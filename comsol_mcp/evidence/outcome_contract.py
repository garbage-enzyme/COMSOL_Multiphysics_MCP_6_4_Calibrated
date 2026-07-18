"""Solver-free contract separating execution, evidence, and scientific outcomes."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .contracts import canonical_json_bytes, canonical_sha256


OUTCOME_SCHEMA_NAME = "comsol_mcp.execution_evidence_outcome"
OUTCOME_SCHEMA_VERSION = "1.0.0"

EXECUTION_STATES = frozenset({"completed", "failed", "interrupted", "cancelled"})
EVIDENCE_COMPLETENESS_STATES = frozenset({"complete", "incomplete", "invalid"})
SCIENTIFIC_DISPOSITIONS = frozenset(
    {
        "accepted",
        "residual",
        "unresolved_at_declared_cap",
        "invalid_evidence",
        "not_evaluated",
    }
)
NEXT_ELIGIBLE_ACTIONS = frozenset(
    {
        "none",
        "review_missing_evidence",
        "repair_evidence_chain",
        "revise_scientific_policy",
        "declare_larger_cap",
        "retry_execution",
        "resume_execution",
        "complete_cleanup_verification",
    }
)

_OUTCOME_FIELDS = {
    "schema_name",
    "schema_version",
    "subject_id",
    "execution",
    "evidence",
    "scientific",
    "outcome_sha256",
}
_EXECUTION_FIELDS = {
    "state",
    "reason_code",
    "completed_requested_work",
    "cleanup",
}
_CLEANUP_FIELDS = {
    "processes_absent",
    "descendants_absent",
    "port_closed",
    "lease_absent",
    "verified",
}
_EVIDENCE_FIELDS = {
    "state",
    "missing_evidence",
    "raw_artifact_ids",
    "diagnostic_artifact_ids",
}
_SCIENTIFIC_FIELDS = {
    "disposition",
    "reason_code",
    "declared_cap_reached",
    "next_eligible_action",
}
_IDENTIFIER_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-/"
)
_MAX_IDENTIFIER_LENGTH = 192
_MAX_LIST_ITEMS = 512


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _reject_unknown(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    unknown = sorted(set(value) - fields)
    if unknown:
        raise ValueError(f"{label} contains unknown fields: {unknown}")


def _identifier(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_IDENTIFIER_LENGTH
        or value[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        or any(character not in _IDENTIFIER_CHARS for character in value)
    ):
        raise ValueError(f"{label} must be a bounded identifier")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def _identifier_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) > _MAX_LIST_ITEMS:
        raise ValueError(f"{label} must be a bounded identifier list")
    normalized = [_identifier(item, f"{label}[{index}]") for index, item in enumerate(value)]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{label} must not contain duplicates")
    if normalized != sorted(normalized):
        raise ValueError(f"{label} must be sorted")
    return normalized


def _validate_execution(value: Any) -> dict[str, Any]:
    execution = _mapping(value, "execution")
    _reject_unknown(execution, _EXECUTION_FIELDS, "execution")
    if set(execution) != _EXECUTION_FIELDS:
        raise ValueError("execution fields are incomplete")
    state = execution["state"]
    if state not in EXECUTION_STATES:
        raise ValueError(f"execution.state must be one of {sorted(EXECUTION_STATES)}")
    _identifier(execution["reason_code"], "execution.reason_code")
    completed_requested_work = _boolean(
        execution["completed_requested_work"], "execution.completed_requested_work"
    )
    cleanup = _mapping(execution["cleanup"], "execution.cleanup")
    _reject_unknown(cleanup, _CLEANUP_FIELDS, "execution.cleanup")
    if set(cleanup) != _CLEANUP_FIELDS:
        raise ValueError("execution.cleanup fields are incomplete")
    for field in sorted(_CLEANUP_FIELDS):
        _boolean(cleanup[field], f"execution.cleanup.{field}")
    measured_verified = all(
        cleanup[field]
        for field in (
            "processes_absent",
            "descendants_absent",
            "port_closed",
            "lease_absent",
        )
    )
    if cleanup["verified"] != measured_verified:
        raise ValueError("execution.cleanup.verified must equal all cleanup proofs")
    if state == "completed" and not completed_requested_work:
        raise ValueError("completed execution must complete all requested work")
    if state != "completed" and completed_requested_work:
        raise ValueError("non-completed execution cannot claim all requested work completed")
    if state in {"completed", "cancelled"} and not cleanup["verified"]:
        raise ValueError(f"{state} execution requires verified cleanup")
    return execution


def _validate_evidence(value: Any, execution_state: str) -> dict[str, Any]:
    evidence = _mapping(value, "evidence")
    _reject_unknown(evidence, _EVIDENCE_FIELDS, "evidence")
    if set(evidence) != _EVIDENCE_FIELDS:
        raise ValueError("evidence fields are incomplete")
    state = evidence["state"]
    if state not in EVIDENCE_COMPLETENESS_STATES:
        raise ValueError(
            f"evidence.state must be one of {sorted(EVIDENCE_COMPLETENESS_STATES)}"
        )
    missing = _identifier_list(evidence["missing_evidence"], "evidence.missing_evidence")
    raw_ids = _identifier_list(evidence["raw_artifact_ids"], "evidence.raw_artifact_ids")
    diagnostic_ids = _identifier_list(
        evidence["diagnostic_artifact_ids"], "evidence.diagnostic_artifact_ids"
    )
    if not set(diagnostic_ids) <= set(raw_ids):
        raise ValueError("diagnostic artifact IDs must reference raw artifacts")
    if state == "complete" and missing:
        raise ValueError("complete evidence cannot list missing evidence")
    if state != "complete" and not missing:
        raise ValueError("non-complete evidence must list missing evidence")
    if state == "complete" and not raw_ids:
        raise ValueError("complete evidence requires at least one raw artifact")
    if execution_state != "completed" and diagnostic_ids != raw_ids:
        raise ValueError("raw artifacts from non-completed execution must remain diagnostic")
    return evidence


def _validate_scientific(
    value: Any,
    *,
    execution_state: str,
    evidence_state: str,
) -> dict[str, Any]:
    scientific = _mapping(value, "scientific")
    _reject_unknown(scientific, _SCIENTIFIC_FIELDS, "scientific")
    if set(scientific) != _SCIENTIFIC_FIELDS:
        raise ValueError("scientific fields are incomplete")
    disposition = scientific["disposition"]
    if disposition not in SCIENTIFIC_DISPOSITIONS:
        raise ValueError(
            f"scientific.disposition must be one of {sorted(SCIENTIFIC_DISPOSITIONS)}"
        )
    _identifier(scientific["reason_code"], "scientific.reason_code")
    cap_reached = _boolean(
        scientific["declared_cap_reached"], "scientific.declared_cap_reached"
    )
    next_action = scientific["next_eligible_action"]
    if next_action not in NEXT_ELIGIBLE_ACTIONS:
        raise ValueError(
            "scientific.next_eligible_action must be one of "
            f"{sorted(NEXT_ELIGIBLE_ACTIONS)}"
        )
    if disposition == "accepted":
        if execution_state != "completed" or evidence_state != "complete":
            raise ValueError("accepted disposition requires completed execution and complete evidence")
        if cap_reached or next_action != "none":
            raise ValueError("accepted disposition cannot reach a cap or propose another action")
    else:
        if next_action == "none":
            raise ValueError("non-accepted disposition requires a next eligible action")
    if disposition in {"residual", "unresolved_at_declared_cap"} and (
        execution_state != "completed" or evidence_state != "complete"
    ):
        raise ValueError(
            f"{disposition} requires completed execution and complete evidence"
        )
    if disposition == "unresolved_at_declared_cap" and (
        not cap_reached or next_action != "declare_larger_cap"
    ):
        raise ValueError(
            "unresolved_at_declared_cap requires a reached cap and caller-declared expansion"
        )
    if disposition == "invalid_evidence" and evidence_state == "complete":
        raise ValueError("invalid_evidence disposition requires non-complete evidence")
    return scientific


def validate_outcome_contract(value: Any, *, verify_hash: bool = True) -> dict[str, Any]:
    """Validate and detach one execution/evidence/scientific outcome."""
    outcome = _mapping(value, "outcome")
    _reject_unknown(outcome, _OUTCOME_FIELDS, "outcome")
    if set(outcome) != _OUTCOME_FIELDS:
        raise ValueError("outcome fields are incomplete")
    if outcome["schema_name"] != OUTCOME_SCHEMA_NAME:
        raise ValueError("outcome.schema_name is unsupported")
    if outcome["schema_version"] != OUTCOME_SCHEMA_VERSION:
        raise ValueError("outcome.schema_version is unsupported")
    _identifier(outcome["subject_id"], "outcome.subject_id")
    execution = _validate_execution(outcome["execution"])
    evidence = _validate_evidence(outcome["evidence"], execution["state"])
    _validate_scientific(
        outcome["scientific"],
        execution_state=execution["state"],
        evidence_state=evidence["state"],
    )
    supplied_hash = outcome["outcome_sha256"]
    if not isinstance(supplied_hash, str) or len(supplied_hash) != 64:
        raise ValueError("outcome.outcome_sha256 must be a SHA-256 digest")
    without_hash = dict(outcome)
    without_hash.pop("outcome_sha256")
    if verify_hash and supplied_hash != canonical_sha256(without_hash):
        raise ValueError("outcome.outcome_sha256 does not match the canonical payload")
    canonical_json_bytes(outcome)
    return deepcopy(outcome)


def build_outcome_contract(value: Mapping[str, Any]) -> dict[str, Any]:
    """Add a canonical hash without mutating referenced raw evidence or caller policy."""
    outcome = deepcopy(dict(value))
    if "outcome_sha256" in outcome:
        raise ValueError("build_outcome_contract computes outcome_sha256")
    outcome["outcome_sha256"] = canonical_sha256(outcome)
    return validate_outcome_contract(outcome)


def execution_from_terminal_job_state(value: Mapping[str, Any]) -> dict[str, Any]:
    """Translate a reconciled durable job state without inventing cleanup proof."""
    state = _mapping(dict(value), "job_state")
    status = state.get("status")
    if status not in EXECUTION_STATES:
        raise ValueError("job state must be reconciled to a terminal execution state")

    if status == "cancelled":
        cancel = _mapping(state.get("cancel"), "job_state.cancel")
        verification = _mapping(
            cancel.get("verification"), "job_state.cancel.verification"
        )
        solver = _mapping(
            verification.get("solver"), "job_state.cancel.verification.solver"
        )
        processes_absent = verification.get("absent") is True
        cleanup = {
            "processes_absent": processes_absent,
            "descendants_absent": processes_absent,
            "port_closed": solver.get("recorded_port_closed") is True,
            "lease_absent": solver.get("lease_state") in {"absent", "recovered"},
            "verified": False,
        }
        cleanup["verified"] = all(
            cleanup[field]
            for field in (
                "processes_absent",
                "descendants_absent",
                "port_closed",
                "lease_absent",
            )
        )
        execution = {
            "state": "cancelled",
            "reason_code": "verified_user_cancellation",
            "completed_requested_work": False,
            "cleanup": cleanup,
        }
    else:
        supplied_cleanup = state.get("cleanup_verification")
        if supplied_cleanup is None:
            cleanup = {
                "processes_absent": status == "interrupted",
                "descendants_absent": False,
                "port_closed": False,
                "lease_absent": False,
                "verified": False,
            }
        else:
            cleanup = deepcopy(
                _mapping(supplied_cleanup, "job_state.cleanup_verification")
            )
        reason_code = {
            "completed": "requested_work_completed",
            "failed": "execution_failed",
            "interrupted": "worker_process_lost",
        }[status]
        execution = {
            "state": status,
            "reason_code": reason_code,
            "completed_requested_work": status == "completed",
            "cleanup": cleanup,
        }
    return deepcopy(_validate_execution(execution))


__all__ = [
    "EVIDENCE_COMPLETENESS_STATES",
    "EXECUTION_STATES",
    "NEXT_ELIGIBLE_ACTIONS",
    "OUTCOME_SCHEMA_NAME",
    "OUTCOME_SCHEMA_VERSION",
    "SCIENTIFIC_DISPOSITIONS",
    "build_outcome_contract",
    "execution_from_terminal_job_state",
    "validate_outcome_contract",
]
