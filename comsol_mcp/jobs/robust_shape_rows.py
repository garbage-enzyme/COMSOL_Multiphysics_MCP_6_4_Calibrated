"""Crash-durable hash-chained rows for robust shape optimization jobs."""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from comsol_mcp.durable import append_jsonl_record, domain_sha256_v2, read_complete_jsonl

from .journal import locked_journal, recover_jsonl_tail

ROBUST_SHAPE_ROW_SCHEMA_NAME = "comsol_mcp.robust_shape_optimization_row"
ROBUST_SHAPE_ROW_SCHEMA_VERSION = "1.0.0"
MAX_ROWS = 100_000
MAX_ROW_BYTES = 256 * 1024
_KINDS = {
    "condition",
    "gradient",
    "iteration",
    "trial",
    "finalist_validation",
    "checkpoint",
    "cleanup",
}
_STATUSES = {"accepted", "rejected", "completed", "failed", "skipped", "cancelled"}


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object with string keys")
    return dict(value)


def _digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value.lower())
    ):
        raise ValueError(f"{name} must be a SHA-256 digest")
    return value.lower()


def _optional_digest(value: object, name: str) -> str | None:
    return None if value is None else _digest(value, name)


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError(f"{name} must be a bounded identifier")
    if not value[0].isalnum() or any(not (item.isalnum() or item in "_.:-") for item in value):
        raise ValueError(f"{name} must be a portable identifier")
    return value


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite")
    number = float(value)
    if number != number or abs(number) == float("inf"):
        raise ValueError(f"{name} must be finite")
    return number


def _optional_finite(value: object, name: str) -> float | None:
    return None if value is None else _finite(value, name)


def _status(value: object, name: str) -> str:
    if value not in _STATUSES:
        raise ValueError(f"{name} is unsupported")
    return str(value)


def _payload(value: object, kind: str) -> dict[str, Any]:
    raw = _mapping(value, f"{kind} payload")
    if kind == "condition":
        fields = {
            "iteration_id",
            "condition_id",
            "condition_order",
            "status",
            "observation_fingerprint",
            "objective_contribution",
            "reason_code",
        }
        if set(raw) != fields:
            raise ValueError("condition payload fields are invalid")
        order = raw["condition_order"]
        if isinstance(order, bool) or not isinstance(order, int) or order < 0:
            raise ValueError("condition_order must be nonnegative")
        status = _status(raw["status"], "condition status")
        if status not in {"completed", "failed", "skipped", "cancelled"}:
            raise ValueError("condition status is unsupported")
        observation = _optional_digest(raw["observation_fingerprint"], "observation_fingerprint")
        contribution = _optional_finite(raw["objective_contribution"], "objective_contribution")
        if status in {"completed", "skipped"} and (observation is None or contribution is None):
            raise ValueError("completed condition rows require durable observation evidence")
        if status in {"failed", "cancelled"} and (
            observation is not None or contribution is not None
        ):
            raise ValueError("failed condition rows must not claim objective evidence")
        return {
            "iteration_id": _identifier(raw["iteration_id"], "iteration_id"),
            "condition_id": _identifier(raw["condition_id"], "condition_id"),
            "condition_order": order,
            "status": status,
            "observation_fingerprint": observation,
            "objective_contribution": contribution,
            "reason_code": _identifier(raw["reason_code"], "reason_code"),
        }
    if kind == "gradient":
        fields = {
            "iteration_id",
            "gradient_fingerprint",
            "acceptance_fingerprint",
            "evidence_state",
        }
        if set(raw) != fields:
            raise ValueError("gradient payload fields are invalid")
        state = raw["evidence_state"]
        if state not in {"native_unchecked", "gradient_validated", "restricted", "rejected"}:
            raise ValueError("gradient evidence_state is unsupported")
        return {
            "iteration_id": _identifier(raw["iteration_id"], "iteration_id"),
            "gradient_fingerprint": _digest(raw["gradient_fingerprint"], "gradient_fingerprint"),
            "acceptance_fingerprint": _digest(
                raw["acceptance_fingerprint"], "acceptance_fingerprint"
            ),
            "evidence_state": state,
        }
    if kind == "iteration":
        fields = {
            "iteration_id",
            "iteration_index",
            "candidate_fingerprint",
            "aggregate_objective",
            "status",
            "robust_objective_fingerprint",
            "fresh_forward_fingerprint",
            "reason_code",
        }
        if set(raw) != fields:
            raise ValueError("iteration payload fields are invalid")
        index = raw["iteration_index"]
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError("iteration_index must be nonnegative")
        return {
            "iteration_id": _identifier(raw["iteration_id"], "iteration_id"),
            "iteration_index": index,
            "candidate_fingerprint": _digest(raw["candidate_fingerprint"], "candidate_fingerprint"),
            "aggregate_objective": _finite(raw["aggregate_objective"], "aggregate_objective"),
            "status": _status(raw["status"], "iteration status"),
            "robust_objective_fingerprint": _digest(
                raw["robust_objective_fingerprint"], "robust_objective_fingerprint"
            ),
            "fresh_forward_fingerprint": _optional_digest(
                raw["fresh_forward_fingerprint"], "fresh_forward_fingerprint"
            ),
            "reason_code": _identifier(raw["reason_code"], "reason_code"),
        }
    if kind == "trial":
        fields = {
            "iteration_id",
            "trial_id",
            "candidate_fingerprint",
            "aggregate_objective",
            "status",
            "reason_code",
        }
        if set(raw) != fields:
            raise ValueError("trial payload fields are invalid")
        return {
            "iteration_id": _identifier(raw["iteration_id"], "iteration_id"),
            "trial_id": _identifier(raw["trial_id"], "trial_id"),
            "candidate_fingerprint": _digest(raw["candidate_fingerprint"], "candidate_fingerprint"),
            "aggregate_objective": _finite(raw["aggregate_objective"], "aggregate_objective"),
            "status": _status(raw["status"], "trial status"),
            "reason_code": _identifier(raw["reason_code"], "reason_code"),
        }
    if kind == "finalist_validation":
        fields = {
            "iteration_id",
            "candidate_fingerprint",
            "policy_fingerprint",
            "receipt_fingerprint",
            "status",
            "reason_codes",
        }
        if set(raw) != fields:
            raise ValueError("finalist validation payload fields are invalid")
        status = raw["status"]
        if status not in {"validated", "rejected"}:
            raise ValueError("finalist validation status is unsupported")
        reasons = raw["reason_codes"]
        if not isinstance(reasons, list) or len(reasons) > 6:
            raise ValueError("finalist validation reason_codes must be a bounded list")
        normalized_reasons = [
            _identifier(item, "finalist validation reason_code") for item in reasons
        ]
        if len(normalized_reasons) != len(set(normalized_reasons)):
            raise ValueError("finalist validation reason_codes must be unique")
        if (status == "validated") != (not normalized_reasons):
            raise ValueError("finalist validation status differs from reason_codes")
        return {
            "iteration_id": _identifier(raw["iteration_id"], "iteration_id"),
            "candidate_fingerprint": _digest(raw["candidate_fingerprint"], "candidate_fingerprint"),
            "policy_fingerprint": _digest(raw["policy_fingerprint"], "policy_fingerprint"),
            "receipt_fingerprint": _digest(raw["receipt_fingerprint"], "receipt_fingerprint"),
            "status": status,
            "reason_codes": normalized_reasons,
        }
    if kind == "checkpoint":
        fields = {
            "iteration_id",
            "checkpoint_fingerprint",
            "completed_condition_count",
            "retained_model_disposition",
        }
        if set(raw) != fields:
            raise ValueError("checkpoint payload fields are invalid")
        count = raw["completed_condition_count"]
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 4096:
            raise ValueError("completed_condition_count is outside the allowed range")
        return {
            "iteration_id": _identifier(raw["iteration_id"], "iteration_id"),
            "checkpoint_fingerprint": _digest(
                raw["checkpoint_fingerprint"], "checkpoint_fingerprint"
            ),
            "completed_condition_count": count,
            "retained_model_disposition": _identifier(
                raw["retained_model_disposition"], "retained_model_disposition"
            ),
        }
    fields = {
        "source_unchanged",
        "client_clear",
        "owned_processes_absent",
        "lease_released",
        "cleanup_fingerprint",
    }
    if set(raw) != fields or any(
        not isinstance(raw[key], bool)
        for key in ("source_unchanged", "client_clear", "owned_processes_absent", "lease_released")
    ):
        raise ValueError("cleanup payload fields are invalid")
    return {
        "source_unchanged": raw["source_unchanged"],
        "client_clear": raw["client_clear"],
        "owned_processes_absent": raw["owned_processes_absent"],
        "lease_released": raw["lease_released"],
        "cleanup_fingerprint": _digest(raw["cleanup_fingerprint"], "cleanup_fingerprint"),
    }


def _normalize_row(
    value: object, *, sequence: int, job_fingerprint: str, previous: str | None
) -> dict[str, Any]:
    raw = _mapping(value, f"robust shape row {sequence}")
    fields = {
        "schema_name",
        "schema_version",
        "sequence",
        "attempt",
        "created_at_epoch",
        "job_fingerprint",
        "kind",
        "payload",
        "previous_row_sha256",
        "row_sha256",
    }
    if set(raw) != fields:
        raise ValueError("robust shape row fields are invalid")
    if (
        raw["schema_name"] != ROBUST_SHAPE_ROW_SCHEMA_NAME
        or raw["schema_version"] != ROBUST_SHAPE_ROW_SCHEMA_VERSION
    ):
        raise ValueError("robust shape row schema is unsupported")
    if isinstance(raw["sequence"], bool) or raw["sequence"] != sequence:
        raise ValueError("robust shape row sequence is not contiguous")
    if (
        isinstance(raw["attempt"], bool)
        or not isinstance(raw["attempt"], int)
        or raw["attempt"] < 1
    ):
        raise ValueError("robust shape row attempt must be positive")
    if raw["job_fingerprint"] != job_fingerprint:
        raise ValueError("robust shape row job identity changed")
    kind = raw["kind"]
    if kind not in _KINDS:
        raise ValueError("robust shape row kind is unsupported")
    if raw["previous_row_sha256"] != previous:
        raise ValueError("robust shape row hash chain is discontinuous")
    expected = domain_sha256_v2(
        ROBUST_SHAPE_ROW_SCHEMA_NAME,
        {key: raw[key] for key in fields if key != "row_sha256"},
    )
    if raw["row_sha256"] != expected:
        raise ValueError("robust shape row hash is invalid")
    return {
        **raw,
        "created_at_epoch": _finite(raw["created_at_epoch"], "created_at_epoch"),
        "payload": _payload(raw["payload"], kind),
        "row_sha256": raw["row_sha256"].lower(),
    }


def _read_rows_unlocked(journal: Path, job: str) -> list[dict[str, Any]]:
    recover_jsonl_tail(journal, max_row_bytes=MAX_ROW_BYTES)
    outcome = read_complete_jsonl(journal, max_bytes=MAX_ROWS * MAX_ROW_BYTES)
    if outcome["state"] in {"corrupt", "oversized"}:
        raise ValueError(f"robust shape journal is {outcome['state']}")
    rows = []
    previous = None
    for sequence, value in enumerate(outcome["records"]):
        row = _normalize_row(value, sequence=sequence, job_fingerprint=job, previous=previous)
        rows.append(row)
        previous = row["row_sha256"]
    return rows


def read_robust_shape_rows(path: str | Path, *, job_fingerprint: str) -> list[dict[str, Any]]:
    """Read complete rows and repair only an incomplete final tail."""
    job = _digest(job_fingerprint, "job_fingerprint")
    with locked_journal(path) as journal:
        return _read_rows_unlocked(journal, job)


def read_robust_shape_rows_readonly(
    path: str | Path, *, job_fingerprint: str
) -> list[dict[str, Any]]:
    """Validate only complete durable rows without locks, repair, or filesystem writes."""
    job = _digest(job_fingerprint, "job_fingerprint")
    outcome = read_complete_jsonl(Path(path), max_bytes=MAX_ROWS * MAX_ROW_BYTES)
    if outcome["state"] != "current_valid":
        raise ValueError(f"robust shape journal is {outcome['state']}")
    rows = []
    previous = None
    for sequence, value in enumerate(outcome["records"]):
        row = _normalize_row(value, sequence=sequence, job_fingerprint=job, previous=previous)
        rows.append(row)
        previous = row["row_sha256"]
    return rows


def append_robust_shape_row(
    path: str | Path,
    *,
    job_fingerprint: str,
    attempt: int,
    kind: str,
    payload: Mapping[str, Any],
    created_at_epoch: float | None = None,
) -> dict[str, Any]:
    """Append one validated, hash-chained, fsync'd robust shape row."""
    job = _digest(job_fingerprint, "job_fingerprint")
    if kind not in _KINDS:
        raise ValueError("robust shape row kind is unsupported")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("attempt must be positive")
    with locked_journal(path) as journal:
        rows = _read_rows_unlocked(journal, job)
        if len(rows) >= MAX_ROWS:
            raise ValueError("robust shape journal exceeds its entry limit")
        body: dict[str, Any] = {
            "schema_name": ROBUST_SHAPE_ROW_SCHEMA_NAME,
            "schema_version": ROBUST_SHAPE_ROW_SCHEMA_VERSION,
            "sequence": len(rows),
            "attempt": attempt,
            "created_at_epoch": float(
                time.time() if created_at_epoch is None else created_at_epoch
            ),
            "job_fingerprint": job,
            "kind": kind,
            "payload": _payload(payload, kind),
            "previous_row_sha256": rows[-1]["row_sha256"] if rows else None,
        }
        row = {**body, "row_sha256": domain_sha256_v2(ROBUST_SHAPE_ROW_SCHEMA_NAME, body)}
        _normalize_row(
            row,
            sequence=body["sequence"],
            job_fingerprint=job,
            previous=body["previous_row_sha256"],
        )
        append_jsonl_record(journal, row)
        return row


__all__ = [
    "ROBUST_SHAPE_ROW_SCHEMA_NAME",
    "ROBUST_SHAPE_ROW_SCHEMA_VERSION",
    "append_robust_shape_row",
    "read_robust_shape_rows",
    "read_robust_shape_rows_readonly",
]
