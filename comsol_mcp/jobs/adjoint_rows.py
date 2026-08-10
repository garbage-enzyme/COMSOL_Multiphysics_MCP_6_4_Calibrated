"""Crash-durable hash-chained rows for bounded adjoint optimization jobs."""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from comsol_mcp.durable import append_jsonl_record, domain_sha256_v2, read_complete_jsonl

from .journal import locked_journal, recover_jsonl_tail

ADJOINT_ROW_SCHEMA_NAME = "comsol_mcp.adjoint_optimization_row"
ADJOINT_ROW_SCHEMA_VERSION = "1.0.0"
MAX_ADJOINT_ROWS = 4096
MAX_ADJOINT_ROW_BYTES = 256 * 1024
_KINDS = {"iteration", "gradient", "trial"}
_STATUSES = {"accepted", "rejected", "failed", "cancelled"}


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
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value.lower()


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


def _payload(value: object, kind: str) -> dict[str, Any]:
    raw = _mapping(value, f"{kind} payload")
    if kind == "iteration":
        fields = {
            "iteration_id",
            "iteration_index",
            "candidate_fingerprint",
            "objective_value",
            "status",
            "gradient_fingerprint",
            "forward_fingerprint",
            "reason_code",
        }
        if set(raw) != fields:
            raise ValueError("iteration payload fields are invalid")
        index = raw["iteration_index"]
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError("iteration_index must be nonnegative")
        status = raw["status"]
        if status not in _STATUSES:
            raise ValueError("iteration status is unsupported")
        gradient = (
            None
            if raw["gradient_fingerprint"] is None
            else _digest(raw["gradient_fingerprint"], "gradient_fingerprint")
        )
        forward = (
            None
            if raw["forward_fingerprint"] is None
            else _digest(raw["forward_fingerprint"], "forward_fingerprint")
        )
        return {
            "iteration_id": _identifier(raw["iteration_id"], "iteration_id"),
            "iteration_index": index,
            "candidate_fingerprint": _digest(raw["candidate_fingerprint"], "candidate_fingerprint"),
            "objective_value": _finite(raw["objective_value"], "objective_value"),
            "status": status,
            "gradient_fingerprint": gradient,
            "forward_fingerprint": forward,
            "reason_code": _identifier(raw["reason_code"], "reason_code"),
        }
    if kind == "gradient":
        fields = {"iteration_id", "gradient_fingerprint", "check_fingerprint", "evidence_state"}
        if set(raw) != fields:
            raise ValueError("gradient payload fields are invalid")
        state = raw["evidence_state"]
        if state not in {"native_unchecked", "gradient_validated", "restricted", "rejected"}:
            raise ValueError("gradient evidence_state is unsupported")
        return {
            "iteration_id": _identifier(raw["iteration_id"], "iteration_id"),
            "gradient_fingerprint": _digest(raw["gradient_fingerprint"], "gradient_fingerprint"),
            "check_fingerprint": _digest(raw["check_fingerprint"], "check_fingerprint"),
            "evidence_state": state,
        }
    fields = {
        "iteration_id",
        "trial_id",
        "candidate_fingerprint",
        "objective_value",
        "status",
        "reason_code",
    }
    if set(raw) != fields:
        raise ValueError("trial payload fields are invalid")
    status = raw["status"]
    if status not in _STATUSES:
        raise ValueError("trial status is unsupported")
    return {
        "iteration_id": _identifier(raw["iteration_id"], "iteration_id"),
        "trial_id": _identifier(raw["trial_id"], "trial_id"),
        "candidate_fingerprint": _digest(raw["candidate_fingerprint"], "candidate_fingerprint"),
        "objective_value": _finite(raw["objective_value"], "objective_value"),
        "status": status,
        "reason_code": _identifier(raw["reason_code"], "reason_code"),
    }


def _normalize_row(
    value: object, *, sequence: int, job_fingerprint: str, previous: str | None
) -> dict[str, Any]:
    raw = _mapping(value, f"adjoint row {sequence}")
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
        raise ValueError("adjoint row fields are invalid")
    if (
        raw["schema_name"] != ADJOINT_ROW_SCHEMA_NAME
        or raw["schema_version"] != ADJOINT_ROW_SCHEMA_VERSION
    ):
        raise ValueError("adjoint row schema is unsupported")
    if raw["sequence"] != sequence or isinstance(raw["sequence"], bool):
        raise ValueError("adjoint row sequence is not contiguous")
    if (
        isinstance(raw["attempt"], bool)
        or not isinstance(raw["attempt"], int)
        or raw["attempt"] < 1
    ):
        raise ValueError("adjoint row attempt must be positive")
    created = _finite(raw["created_at_epoch"], "created_at_epoch")
    if raw["job_fingerprint"] != job_fingerprint:
        raise ValueError("adjoint row job identity changed")
    kind = raw["kind"]
    if kind not in _KINDS:
        raise ValueError("adjoint row kind is unsupported")
    payload = _payload(raw["payload"], kind)
    if raw["previous_row_sha256"] != previous:
        raise ValueError("adjoint row hash chain is discontinuous")
    expected = domain_sha256_v2(
        ADJOINT_ROW_SCHEMA_NAME,
        {key: raw[key] for key in fields if key != "row_sha256"},
    )
    if raw["row_sha256"] != expected:
        raise ValueError("adjoint row hash is invalid")
    return {
        **raw,
        "created_at_epoch": created,
        "payload": payload,
        "row_sha256": raw["row_sha256"].lower(),
    }


def _read_adjoint_rows_unlocked(journal: Path, job: str) -> list[dict[str, Any]]:
    recover_jsonl_tail(journal, max_row_bytes=MAX_ADJOINT_ROW_BYTES)
    outcome = read_complete_jsonl(journal, max_bytes=MAX_ADJOINT_ROWS * MAX_ADJOINT_ROW_BYTES)
    if outcome["state"] in {"corrupt", "oversized"}:
        raise ValueError(f"adjoint journal is {outcome['state']}")
    rows: list[dict[str, Any]] = []
    previous = None
    for sequence, value in enumerate(outcome["records"]):
        row = _normalize_row(value, sequence=sequence, job_fingerprint=job, previous=previous)
        rows.append(row)
        previous = row["row_sha256"]
    return rows


def read_adjoint_rows(path: str | Path, *, job_fingerprint: str) -> list[dict[str, Any]]:
    """Read and validate all complete rows, repairing only a partial final tail."""
    job = _digest(job_fingerprint, "job_fingerprint")
    with locked_journal(path) as journal:
        return _read_adjoint_rows_unlocked(journal, job)


def append_adjoint_row(
    path: str | Path,
    *,
    job_fingerprint: str,
    attempt: int,
    kind: str,
    payload: Mapping[str, Any],
    created_at_epoch: float | None = None,
) -> dict[str, Any]:
    """Append one fsync'd iteration, gradient, or trial row."""
    job = _digest(job_fingerprint, "job_fingerprint")
    if kind not in _KINDS:
        raise ValueError("adjoint row kind is unsupported")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("attempt must be positive")
    with locked_journal(path) as journal:
        rows = _read_adjoint_rows_unlocked(journal, job)
        if len(rows) >= MAX_ADJOINT_ROWS:
            raise ValueError("adjoint journal exceeds its entry limit")
        normalized_payload = _payload(payload, kind)
        body = {
            "schema_name": ADJOINT_ROW_SCHEMA_NAME,
            "schema_version": ADJOINT_ROW_SCHEMA_VERSION,
            "sequence": len(rows),
            "attempt": attempt,
            "created_at_epoch": float(
                time.time() if created_at_epoch is None else created_at_epoch
            ),
            "job_fingerprint": job,
            "kind": kind,
            "payload": normalized_payload,
            "previous_row_sha256": rows[-1]["row_sha256"] if rows else None,
        }
        row = {**body, "row_sha256": domain_sha256_v2(ADJOINT_ROW_SCHEMA_NAME, body)}
        _normalize_row(
            row,
            sequence=body["sequence"],
            job_fingerprint=job,
            previous=body["previous_row_sha256"],
        )
        append_jsonl_record(journal, row)
        return row


__all__ = [
    "ADJOINT_ROW_SCHEMA_NAME",
    "ADJOINT_ROW_SCHEMA_VERSION",
    "append_adjoint_row",
    "read_adjoint_rows",
]
