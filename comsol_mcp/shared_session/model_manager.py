"""Runtime for the default-off COMSOL Model Manager feature.

Relationship to the other Model Manager modules
-----------------------------------------------

:mod:`comsol_mcp.shared_session.dbmodel_operations` decides whether one
operation is admissible and what its receipt must record. It is pure and has no
transport. This module is the runtime a public tool calls, and it adds the three
things a pure decision layer cannot own:

1. the feature gate, read through the same ``feature_enabled`` predicate every
   other optional feature uses;
2. durable journalling of every attempt through
   :class:`comsol_mcp.durable.operation_ledger.OperationLedger`; and
3. serialization of the actual Model Manager call behind an injected caller.

Why the transport is injected
-----------------------------

The real Model Manager call needs a live COMSOL session and a licensed host.
Injecting it as a callable keeps this module testable without a solver and — more
usefully — means the gate, the journal write, the collision checks, and the
receipt are all exercised by solver-free tests. A licensed run then only has to
prove the injected callable works, not re-prove the policy.

Frozen behaviours
-----------------

* **The feature is default-off and has one switch.** ``model_manager.enabled``
  controls the whole surface, like the other optional features. Within the
  enabled feature, ``model_manager.upload_enabled`` separately controls the
  upload operation only, so reads stay available with writes refused.
* **A read never requires the upload setting.** ``open``, ``run``, and
  ``download`` work whenever the feature is on, because the write setting exists
  to protect the Model Manager from writes, not to withhold reads.
* **Every attempt is journalled, including a refusal.** A refused operation
  leaves the same durable record a successful one does, because "we tried and
  were refused" is evidence a caller needs.
* **A response is never treated as success.** The injected caller reports the
  outcome; the runtime records what it was told and marks a merely-received reply
  as not-success.
* **An upload is journalled before the write is attempted**, so a crash during
  the write leaves a durable pre-write record rather than an unattributed change.
* **No session, no operation.** Without an attached client the runtime refuses
  rather than reaching for a different attachment path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from comsol_mcp.durable import canonical_sha256_v1
from comsol_mcp.durable.operation_ledger import UNKNOWN, OperationLedger
from comsol_mcp.shared_session.dbmodel_operations import (
    DBMODEL_OPERATIONS,
    DbmodelOperationError,
    DbmodelRequest,
    IdentityRecord,
    build_request,
    capability_document,
    evaluate_destination_collision,
    evaluate_revision_collision,
    normalize_identity,
    operation_receipt,
)

#: The feature gate name, matching :data:`comsol_mcp.tools.catalog.FEATURE_NAMES`.
FEATURE_NAME = "model_manager"

#: The upload setting name within the feature.
UPLOAD_SETTING = "model_manager.upload_enabled"

#: Outcomes an injected dispatch may report.
DISPATCH_OUTCOMES = ("succeeded", "refused", "failed", "unknown")

#: How each dispatch outcome is recorded in the durable ledger.
#:
#: The ledger's vocabulary is about what happened to the attempt, and an
#: unreported outcome is an attempt whose result the environment did not settle,
#: so it is recorded as an interrupted attempt rather than as any terminal
#: disposition. Collapsing it to ``failed`` would assert a failure nobody
#: observed; collapsing it to ``succeeded`` would assert an unobserved success.
LEDGER_OUTCOME = {
    "succeeded": "succeeded",
    "refused": "refused",
    "failed": "failed",
    "unknown": "in_progress",
}


class ModelManagerError(RuntimeError):
    """One stable, bounded refusal with a machine-readable reason code."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class ModelManagerContext:
    """The exact things one operation needs, gathered before dispatch."""

    attached: bool
    session_identity: str | None
    dispatch: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None


def evaluate_gate(*, feature_enabled: bool, upload_enabled: bool) -> dict[str, Any]:
    """Return both gate verdicts without performing any operation."""
    return {
        "feature": FEATURE_NAME,
        "enabled": bool(feature_enabled),
        "default": False,
        "restart_required": True,
        "upload_setting": UPLOAD_SETTING,
        "upload_enabled": bool(upload_enabled),
        "upload_default": False,
        "upload_independent_of_shared_server": True,
    }


def _require_gate(feature_enabled: bool) -> None:
    if not feature_enabled:
        raise ModelManagerError(
            "feature_gate_closed",
            "the COMSOL Model Manager feature is disabled; enable it explicitly "
            "and restart the MCP host",
        )


def _require_session(context: ModelManagerContext) -> None:
    """Require an attached session before any Model Manager access.

    Every operation addresses a model held by a COMSOL session. Without an
    attachment there is nothing to address, so this refuses rather than
    constructing an unexpected client.
    """
    if not context.attached or not context.session_identity:
        raise ModelManagerError(
            "no_attached_session",
            "no COMSOL session is attached; attach to the shared session first",
        )


class ModelManagerRuntime:
    """Gate-checked, journalled Model Manager operations."""

    def __init__(
        self,
        *,
        feature_enabled: bool,
        upload_enabled: bool,
        context: ModelManagerContext,
        runtime_dir: str | Path | None = None,
        ledger: OperationLedger | None = None,
    ) -> None:
        self._feature_enabled = bool(feature_enabled)
        self._upload_enabled = bool(upload_enabled)
        self._context = context
        self._ledger = ledger if ledger is not None else _ledger_for(runtime_dir)

    # -- introspection -----------------------------------------------------

    @property
    def gate(self) -> dict[str, Any]:
        return evaluate_gate(
            feature_enabled=self._feature_enabled, upload_enabled=self._upload_enabled
        )

    def capabilities(self) -> dict[str, Any]:
        """Truthful capability metadata for this runtime's current state."""
        document = capability_document(upload_enabled=self._upload_enabled)
        return {
            **document,
            "gate": self.gate,
            "attached": self._context.attached,
            "session_identity": self._context.session_identity,
            "journalled": self._ledger is not None,
        }

    # -- operations --------------------------------------------------------

    def open(self, *, uri: str) -> dict[str, Any]:
        """Open a model or experiment data item held by the Model Manager."""
        return self._run(operation="open", uri=uri)

    def run(self, *, uri: str, resource_policy: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Run a model with the caller's normal solver/resource policy.

        The policy is passed through unchanged. This layer never supplies,
        defaults, or relaxes a solver or resource setting.
        """
        return self._run(
            operation="run",
            uri=uri,
            extra={"resource_policy": dict(resource_policy or {})},
        )

    def download(
        self,
        *,
        uri: str,
        destination: str,
        destination_exists: bool = False,
        existing_sha256: str | None = None,
        incoming_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Download to a caller-selected local ``.mph`` destination.

        The collision decision is made before dispatch, so a refused write can
        never be reported as an attempted one.
        """
        collision = evaluate_destination_collision(
            destination_exists=destination_exists,
            existing_sha256=existing_sha256,
            incoming_sha256=incoming_sha256,
        )
        return self._run(
            operation="download",
            uri=uri,
            destination=destination,
            pre_dispatch=collision,
        )

    def upload(
        self,
        *,
        uri: str,
        derived_source_identity: Mapping[str, Any] | None,
        expected_revision: str | None = None,
        observed_revision: str | None = None,
    ) -> dict[str, Any]:
        """Save a caller-selected derived copy back to the Model Manager.

        Requires the separate upload setting. Even when it is on, the revision
        check must pass before any write is attempted, and an overwrite is never
        permitted.
        """
        revision = evaluate_revision_collision(
            expected_revision=expected_revision,
            observed_revision=observed_revision,
        )
        return self._run(
            operation="upload",
            uri=uri,
            derived_source_identity=derived_source_identity,
            pre_dispatch=revision,
        )

    # -- internals ---------------------------------------------------------

    def _run(
        self,
        *,
        operation: str,
        uri: str,
        destination: Any = None,
        derived_source_identity: Any = None,
        pre_dispatch: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Order is deliberate: feature gate, then session, then request validity,
        # then the upload setting, then pre-dispatch policy, then journal, then
        # dispatch. A refusal at an earlier step cannot reach a later one.
        try:
            _require_gate(self._feature_enabled)
            _require_session(self._context)
        except ModelManagerError as exc:
            self._journal_raw(operation=operation, subject=uri, reason=exc.reason_code)
            raise

        try:
            request = build_request(
                operation=operation,
                uri=uri,
                destination=destination,
                derived_source_identity=derived_source_identity,
                write_enabled=self._upload_enabled,
            )
        except DbmodelOperationError as exc:
            return self._refuse(request=None, operation=operation, uri=uri, reason=exc.reason_code)

        if pre_dispatch is not None and not pre_dispatch.get("write_allowed", True):
            return self._refuse(
                request=request,
                operation=operation,
                uri=uri,
                reason=str(pre_dispatch.get("reason", "refused")),
            )

        if self._context.dispatch is None:
            return self._refuse(
                request=request, operation=operation, uri=uri, reason="no_dispatch_available"
            )

        # Journalled before dispatch so the attempt is durable even if the process
        # dies during the transport call.
        self._journal(
            request=request,
            outcome="in_progress",
            receipt_sha256=None,
            cleanup_state=UNKNOWN,
            detail={"stage": "pre_dispatch"},
        )

        payload: dict[str, Any] = {
            "uri": request.uri,
            "authority": request.authority,
            "resource": request.resource,
            "declared_sha256": request.declared_sha256,
        }
        if request.destination is not None:
            payload["destination"] = request.destination
        if request.derived_source_identity is not None:
            payload["derived_source_identity"] = request.derived_source_identity.to_document()
        if extra:
            payload.update(extra)

        try:
            reply = self._context.dispatch(request.operation, payload)
        except Exception:
            self._journal(
                request=request,
                outcome="failed",
                receipt_sha256=None,
                cleanup_state=UNKNOWN,
                detail={"stage": "dispatch_raised"},
            )
            raise

        outcome = _reply_outcome(reply)
        resolved = _reply_identity(reply)
        receipt = operation_receipt(
            request,
            outcome=outcome,
            resolved_identity=resolved,
            transport_reply_received=True,
            cleanup=_reply_cleanup(reply),
            detail={"injected_dispatch": True},
        )
        self._journal(
            request=request,
            outcome=LEDGER_OUTCOME[outcome],
            receipt_sha256=receipt["receipt_sha256"],
            artifact_sha256=resolved.sha256,
            cleanup_state=str(receipt["cleanup"].get("state", UNKNOWN)),
            detail={"stage": "dispatch_returned", "dispatch_outcome": outcome},
        )
        return {**receipt, "result": dict(reply) if isinstance(reply, Mapping) else {}}

    def _refuse(
        self,
        *,
        request: DbmodelRequest | None,
        operation: str,
        uri: str,
        reason: str,
    ) -> dict[str, Any]:
        """Record a refusal as durably as a success."""
        subject = uri if isinstance(uri, str) else UNKNOWN
        if request is not None:
            receipt = operation_receipt(
                request,
                outcome="refused",
                transport_reply_received=False,
                detail={"reason": reason},
            )
            self._journal(
                request=request,
                outcome="refused",
                receipt_sha256=receipt["receipt_sha256"],
                cleanup_state=UNKNOWN,
                detail={"reason": reason},
            )
            return {**receipt, "reason": reason}
        body = {
            "schema_name": "comsol_mcp.model_manager_refusal",
            "schema_version": "1.0.0",
            "operation": operation if operation in DBMODEL_OPERATIONS else UNKNOWN,
            "uri": subject,
            "outcome": "refused",
            "reason": reason,
            "transport_attempted": False,
            "success_inferred_from_transport": False,
        }
        self._journal_raw(operation=operation, subject=subject, reason=reason)
        return {**body, "refusal_sha256": canonical_sha256_v1(body)}

    # -- journalling -------------------------------------------------------

    def _journal(
        self,
        *,
        request: DbmodelRequest,
        outcome: str,
        receipt_sha256: str | None,
        cleanup_state: str,
        detail: Mapping[str, Any],
        artifact_sha256: Any = None,
    ) -> None:
        if self._ledger is None:
            return
        self._ledger.append(
            family="dbmodel",
            operation=request.operation,
            subject=request.uri,
            owner=self._context.session_identity or UNKNOWN,
            outcome=outcome,
            receipt_sha256=receipt_sha256,
            artifact_sha256=artifact_sha256,
            cleanup_state=cleanup_state,
            detail=dict(detail),
        )

    def _journal_raw(self, *, operation: str, subject: str, reason: str) -> None:
        if self._ledger is None:
            return
        self._ledger.append(
            family="dbmodel",
            operation=operation if operation in DBMODEL_OPERATIONS else UNKNOWN,
            subject=subject,
            owner=self._context.session_identity or UNKNOWN,
            outcome="refused",
            cleanup_state=UNKNOWN,
            detail={"reason": reason, "stage": "pre_admission"},
        )

    def replay(self) -> dict[str, Any]:
        """Rebuild journalled state, or report that no journal is kept."""
        if self._ledger is None:
            return {
                "journal_state": "not_configured",
                "row_count": 0,
                "skipped_row_count": 0,
                "outcome_counts": {},
                "family_counts": {},
            }
        return self._ledger.replay()

    def recovery_report(self) -> dict[str, Any]:
        """Report journalled recovery state, or state that none is kept."""
        if self._ledger is None:
            return {
                "journal_state": "not_configured",
                "row_count": 0,
                "interrupted_subjects": [],
                "cleanup_state_unknown_subjects": [],
            }
        return self._ledger.recovery_report()


def _ledger_for(runtime_dir: str | Path | None) -> OperationLedger | None:
    if runtime_dir is None:
        return None
    return OperationLedger(Path(runtime_dir) / "model_manager")


def _reply_outcome(reply: Any) -> str:
    """Read the caller's reported outcome, defaulting to ``unknown``.

    A reply that merely arrived is not evidence of success, so an unreported or
    unrecognised outcome is recorded as ``unknown`` rather than promoted.
    """
    if not isinstance(reply, Mapping):
        return "unknown"
    value = reply.get("outcome")
    if isinstance(value, str) and value in DISPATCH_OUTCOMES:
        return value
    return "unknown"


def _reply_identity(reply: Any) -> IdentityRecord:
    if not isinstance(reply, Mapping):
        return IdentityRecord.unknown("dispatch_reply")
    try:
        return normalize_identity(reply.get("identity"), source="dispatch_reply")
    except DbmodelOperationError:
        return IdentityRecord.unknown("dispatch_reply")


def _reply_cleanup(reply: Any) -> dict[str, Any]:
    if not isinstance(reply, Mapping):
        return {"state": UNKNOWN}
    cleanup = reply.get("cleanup")
    return dict(cleanup) if isinstance(cleanup, Mapping) else {"state": UNKNOWN}


__all__ = [
    "DISPATCH_OUTCOMES",
    "FEATURE_NAME",
    "ModelManagerContext",
    "ModelManagerError",
    "ModelManagerRuntime",
    "UPLOAD_SETTING",
    "evaluate_gate",
]
