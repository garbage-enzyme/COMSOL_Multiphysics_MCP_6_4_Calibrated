"""``dbmodel://`` Model Manager operation contract.

Scope and boundaries
--------------------

This module implements the four Model Manager operations on top of the frozen
0.7.5 ``dbmodel://`` source-kind contract:

1. ``open``  - open a model or experiment data item;
2. ``run``   - run a model with the caller's normal solver/resource policy;
3. ``download`` - write a caller-selected local ``.mph`` destination; and
4. ``upload`` - save a caller-selected derived copy back to the manager.

It is deliberately split from the transport. Everything here is a pure decision
about *whether* an operation is admissible and *what* a receipt must record; the
actual COMSOL/MPh call happens elsewhere and only after this layer admits it.
That split is what makes the whole contract testable without a solver, and it is
why a policy bug cannot be hidden behind a licensed run.

Frozen rules
------------

* **Write is a separate, default-off gate.** ``upload`` requires an explicit
  caller setting that is independent of ``shared_server.enabled``. Opening,
  running, and downloading must never turn it on.
* **No overwrite, ever.** An existing destination, a revision mismatch, or an
  identity collision is refused. There is no retry, force, or fallback path.
* **Source immutability.** An upload writes a *derived* copy; the source
  identity is preserved and must be declared.
* **Unknown stays unknown.** A revision or hash the environment did not expose is
  recorded as ``unknown``; it is never inferred from a transport reply.
* **Success is never inferred from transport.** A reply that merely arrived is
  not evidence that the operation succeeded.
* **Permissions are delegated.** The module never invents, stores, or prompts for
  credentials; the configured COMSOL/MPh environment owns authentication.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from comsol_mcp.contracts.surrogate import parse_dbmodel_uri
from comsol_mcp.durable import canonical_sha256_v1

SCHEMA_NAME = "comsol_mcp.dbmodel_operation"
SCHEMA_VERSION = "1.0.0"
RECEIPT_SCHEMA_NAME = "comsol_mcp.dbmodel_operation_receipt"
RECEIPT_SCHEMA_VERSION = "1.0.0"

#: The four admitted operations. Nothing else is expressible, so this surface
#: cannot become a generic shell or cloud API.
DBMODEL_OPERATIONS = ("open", "run", "download", "upload")

#: Operations that only read. These must stay usable with the write gate off.
READ_ONLY_OPERATIONS = ("open", "run", "download")

#: Operations that mutate the Model Manager. Gated separately and default-off.
WRITE_OPERATIONS = ("upload",)

#: The only admitted local destination suffix for a download.
DOWNLOAD_SUFFIX = ".mph"

#: Identity values the environment did not expose are reported with this token
#: rather than guessed.
UNKNOWN = "unknown"


class DbmodelOperationError(ValueError):
    """One stable, bounded refusal with a reason code."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class IdentityRecord:
    """One as-reported identity, preserving absence instead of inferring it."""

    revision: str
    sha256: str
    source: str

    @staticmethod
    def unknown(source: str) -> IdentityRecord:
        return IdentityRecord(revision=UNKNOWN, sha256=UNKNOWN, source=source)

    def to_document(self) -> dict[str, str]:
        return {"revision": self.revision, "sha256": self.sha256, "source": self.source}


@dataclass(frozen=True)
class DbmodelRequest:
    """One parsed, validated Model Manager operation request."""

    operation: str
    uri: str
    authority: str
    resource: str
    declared_sha256: str | None
    destination: str | None = None
    derived_source_identity: IdentityRecord | None = None
    write_enabled: bool = False
    overwrite_existing: bool = False
    request_metadata: Mapping[str, Any] = field(default_factory=dict)


def parse_operation(value: object) -> str:
    """Validate one operation name against the frozen set."""
    if not isinstance(value, str) or value not in DBMODEL_OPERATIONS:
        raise DbmodelOperationError(
            "unsupported_operation",
            f"operation must be one of {', '.join(DBMODEL_OPERATIONS)}",
        )
    return value


def normalize_local_destination(value: object) -> str:
    """Validate a caller-selected local download destination.

    Path *containment* is enforced by the existing path policy during dispatch;
    this layer enforces the shape rules that must hold before any filesystem
    work: an absolute, ASCII, ``.mph``-suffixed destination that is not a device
    path or a traversal attempt.
    """
    if not isinstance(value, str) or not value.strip():
        raise DbmodelOperationError(
            "invalid_destination", "a download requires an explicit local destination"
        )
    if len(value) > 4096:
        raise DbmodelOperationError("invalid_destination", "destination is out of range")
    if not value.isascii():
        raise DbmodelOperationError("invalid_destination", "destination must be ASCII")
    text = value.strip()
    if text.startswith(("\\\\?\\", "\\\\.\\", "//?/", "//./")):
        raise DbmodelOperationError(
            "invalid_destination", "destination must not use a Windows device prefix"
        )
    if ".." in text.replace("\\", "/").split("/"):
        raise DbmodelOperationError("invalid_destination", "destination must not traverse")
    from pathlib import Path

    path = Path(text)
    if not path.is_absolute():
        raise DbmodelOperationError("invalid_destination", "destination must be an absolute path")
    if path.suffix.casefold() != DOWNLOAD_SUFFIX:
        raise DbmodelOperationError(
            "invalid_destination", f"destination must end with {DOWNLOAD_SUFFIX}"
        )
    return text


def _identity_field(value: Mapping[str, Any], key: str) -> str:
    """Read one identity field, separating "not exposed" from "malformed".

    An absent, null, or blank field means the environment did not expose it, and
    is recorded as ``unknown``. Any other non-string is malformed input and is
    refused instead, so a rejected value can never be mistaken for a silently
    recorded unknown.
    """
    if key not in value:
        return UNKNOWN
    raw = value[key]
    if raw is None:
        return UNKNOWN
    if not isinstance(raw, str):
        raise DbmodelOperationError(
            "invalid_identity", f"{key} must be a string when it is present"
        )
    return raw.strip() or UNKNOWN


def normalize_identity(value: object, *, source: str) -> IdentityRecord:
    """Validate one as-reported identity, defaulting honestly to ``unknown``."""
    if value is None:
        return IdentityRecord.unknown(source)
    if not isinstance(value, Mapping):
        raise DbmodelOperationError("invalid_identity", "an identity must be an object")
    revision = _identity_field(value, "revision")
    digest = _identity_field(value, "sha256").lower()
    if digest != UNKNOWN and (
        len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
    ):
        # A malformed hash is not a hash. Refuse rather than record a wrong one.
        raise DbmodelOperationError("invalid_identity", "sha256 must be 64 lowercase hex digits")
    return IdentityRecord(revision=revision.strip(), sha256=digest, source=source)


def build_request(
    *,
    operation: object,
    uri: object,
    destination: object = None,
    derived_source_identity: object = None,
    write_enabled: bool = False,
    overwrite_existing: bool = False,
) -> DbmodelRequest:
    """Parse and admit one operation request, failing closed on every rule.

    Write admission happens here, before any dispatch, so a refusal cannot be
    reached only after a partially performed write.
    """
    admitted = parse_operation(operation)
    if not isinstance(uri, str):
        raise DbmodelOperationError("invalid_uri", "a dbmodel operation requires a URI string")
    try:
        components = parse_dbmodel_uri(uri)
    except ValueError as exc:
        raise DbmodelOperationError("invalid_uri", str(exc)) from exc

    local_destination: str | None = None
    identity: IdentityRecord | None = None

    if admitted in WRITE_OPERATIONS:
        if overwrite_existing:
            # Refused before the gate so an explicit overwrite can never appear
            # to be permitted by enabling writes.
            raise DbmodelOperationError(
                "overwrite_refused", "overwriting an existing Model Manager item is refused"
            )
        if not write_enabled:
            raise DbmodelOperationError(
                "write_gate_disabled",
                "Model Manager upload requires the separate upload gate to be enabled",
            )
        identity = normalize_identity(derived_source_identity, source="derived_copy")
        if identity.revision == UNKNOWN and identity.sha256 == UNKNOWN:
            raise DbmodelOperationError(
                "invalid_identity",
                "an upload requires an explicit derived source identity (revision or sha256)",
            )
    elif derived_source_identity is not None:
        raise DbmodelOperationError(
            "unexpected_identity", "only an upload may declare a derived source identity"
        )

    if admitted == "download":
        local_destination = normalize_local_destination(destination)
    elif destination is not None:
        raise DbmodelOperationError(
            "unexpected_destination", "only a download may declare a local destination"
        )

    return DbmodelRequest(
        operation=admitted,
        uri=uri,
        authority=str(components["authority"]),
        resource=str(components["resource"]),
        declared_sha256=components["sha256"],
        destination=local_destination,
        derived_source_identity=identity,
        write_enabled=bool(write_enabled),
        overwrite_existing=False,
    )


def evaluate_destination_collision(
    *,
    destination_exists: bool,
    existing_sha256: str | None,
    incoming_sha256: str | None,
) -> dict[str, Any]:
    """Decide whether a download destination may be written.

    Returning a decision rather than raising lets a caller receipt record *why*
    a write was refused, which is the difference between a documented refusal and
    an unexplained failure.
    """
    if not destination_exists:
        return {"write_allowed": True, "reason": "destination_absent"}
    if existing_sha256 and incoming_sha256 and existing_sha256 == incoming_sha256:
        # The bytes already match. Refusing is still correct: this surface has no
        # silent-overwrite path, and an identical re-download is a no-op the
        # caller can observe rather than a permitted replacement.
        return {"write_allowed": False, "reason": "identical_content_already_present"}
    return {"write_allowed": False, "reason": "destination_exists"}


def evaluate_revision_collision(
    *,
    expected_revision: str | None,
    observed_revision: str | None,
) -> dict[str, Any]:
    """Decide whether an upload may proceed against the observed revision."""
    if observed_revision in (None, UNKNOWN):
        # Unknown is not a match. Proceeding on an unverified revision would
        # silently risk overwriting a newer item.
        return {"write_allowed": False, "reason": "observed_revision_unknown"}
    if expected_revision in (None, UNKNOWN):
        return {"write_allowed": False, "reason": "expected_revision_unknown"}
    if expected_revision != observed_revision:
        return {"write_allowed": False, "reason": "revision_collision"}
    return {"write_allowed": True, "reason": "revision_matched"}


def operation_receipt(
    request: DbmodelRequest,
    *,
    outcome: str,
    resolved_identity: IdentityRecord | None = None,
    transport_reply_received: bool = False,
    cleanup: Mapping[str, Any] | None = None,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the append-only receipt for one attempted operation.

    The receipt keeps three things separate that are easy to conflate: whether a
    reply arrived, whether the operation succeeded, and whether cleanup is
    verified. ``outcome`` is caller-supplied for that reason.
    """
    resolved = resolved_identity or IdentityRecord.unknown("unresolved")
    body: dict[str, Any] = {
        "schema_name": RECEIPT_SCHEMA_NAME,
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "operation": request.operation,
        # The original URI is recorded verbatim; the parsed parts are derived.
        "uri": request.uri,
        "authority": request.authority,
        "resource": request.resource,
        "declared_sha256": request.declared_sha256 or UNKNOWN,
        "resolved_identity": resolved.to_document(),
        "outcome": outcome,
        "transport_reply_received": bool(transport_reply_received),
        # Stated explicitly so a reader cannot mistake a reply for success.
        "success_inferred_from_transport": False,
        "is_write_operation": request.operation in WRITE_OPERATIONS,
        "write_gate_enabled": request.write_enabled,
        "local_destination": request.destination or UNKNOWN,
        "derived_source_identity": (
            request.derived_source_identity.to_document()
            if request.derived_source_identity
            else UNKNOWN
        ),
        "source_immutable": True,
        "cleanup": dict(cleanup or {"state": UNKNOWN}),
        "authentication": {
            "delegated_to": "configured_comsol_mph_environment",
            "credentials_recorded": False,
        },
        "detail": dict(detail or {}),
    }
    return {**body, "receipt_sha256": canonical_sha256_v1(body)}


def capability_document(*, upload_enabled: bool) -> dict[str, Any]:
    """Truthful capability metadata distinguishing read from write access.

    Reporting a write operation as available while the gate is off would be a
    false capability claim, so the gate state is part of the advertised shape.
    """
    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "source_kind": "dbmodel",
        "uri_syntax": "dbmodel://<authority>/<resource>[?sha256=<hex>]",
        "operations": {
            operation: {
                "available": True,
                "mutates_model_manager": operation in WRITE_OPERATIONS,
                "requires_write_gate": operation in WRITE_OPERATIONS,
            }
            for operation in DBMODEL_OPERATIONS
        },
        "upload_gate": {
            "name": "dbmodel_upload",
            "enabled": bool(upload_enabled),
            "default": False,
            "independent_of_shared_server": True,
        },
        "overwrite_permitted": False,
        "generic_cloud_api": False,
        "authentication": "delegated_to_configured_environment",
    }


__all__ = [
    "DBMODEL_OPERATIONS",
    "DOWNLOAD_SUFFIX",
    "READ_ONLY_OPERATIONS",
    "RECEIPT_SCHEMA_NAME",
    "RECEIPT_SCHEMA_VERSION",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "UNKNOWN",
    "WRITE_OPERATIONS",
    "DbmodelOperationError",
    "DbmodelRequest",
    "IdentityRecord",
    "build_request",
    "capability_document",
    "evaluate_destination_collision",
    "evaluate_revision_collision",
    "normalize_identity",
    "normalize_local_destination",
    "operation_receipt",
    "parse_operation",
]
