"""Append-only hash-chained journal for durable research artifacts."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from comsol_mcp.durable import (
    append_jsonl_record,
    domain_sha256_v2,
    fsync_directory,
    read_complete_jsonl,
)

from .decisions import DECISION_RECORD_SCHEMA_NAME
from .evaluations import EVALUATION_RECORD_SCHEMA_NAME
from .records import CANDIDATE_RECORD_SCHEMA_NAME
from .state import OPTIMIZER_CHECKPOINT_SCHEMA_NAME, PORTFOLIO_SCHEMA_NAME

RESEARCH_JOURNAL_RECORD_SCHEMA_NAME = "comsol_mcp.research_journal_record"
RESEARCH_JOURNAL_RECORD_SCHEMA_VERSION = "1.0.0"
MAX_JOURNAL_BYTES = 256 * 1024 * 1024
_KINDS = {
    "candidate": (CANDIDATE_RECORD_SCHEMA_NAME, "record_fingerprint"),
    "decision": (DECISION_RECORD_SCHEMA_NAME, "decision_fingerprint"),
    "evaluation": (EVALUATION_RECORD_SCHEMA_NAME, "evaluation_fingerprint"),
    "checkpoint": (OPTIMIZER_CHECKPOINT_SCHEMA_NAME, "checkpoint_fingerprint"),
    "portfolio": (PORTFOLIO_SCHEMA_NAME, "portfolio_fingerprint"),
}


def _validate_payload(kind: object, payload: object) -> tuple[str, dict[str, Any]]:
    if kind not in _KINDS:
        raise ValueError("research journal kind is unsupported")
    if not isinstance(payload, dict):
        raise ValueError("research journal payload must be an object")
    schema_name, fingerprint_field = _KINDS[str(kind)]
    if payload.get("schema_name") != schema_name:
        raise ValueError("research journal payload schema does not match its kind")
    fingerprint = payload.get(fingerprint_field)
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise ValueError("research journal payload fingerprint is missing")
    body = {key: value for key, value in payload.items() if key != fingerprint_field}
    if kind == "candidate":
        body.pop("candidate_fingerprint", None)
    if domain_sha256_v2(schema_name, body) != fingerprint:
        raise ValueError("research journal payload fingerprint is invalid")
    return fingerprint, payload


def _validate_records(records: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    previous: str | None = None
    for sequence, record in enumerate(records):
        if not isinstance(record, dict) or set(record) != {
            "schema_name",
            "schema_version",
            "sequence",
            "kind",
            "payload",
            "payload_fingerprint",
            "previous_record_fingerprint",
            "record_fingerprint",
        }:
            raise ValueError("research journal record fields are invalid")
        if (
            record["schema_name"] != RESEARCH_JOURNAL_RECORD_SCHEMA_NAME
            or record["schema_version"] != RESEARCH_JOURNAL_RECORD_SCHEMA_VERSION
        ):
            raise ValueError("research journal record schema is unsupported")
        if record["sequence"] != sequence:
            raise ValueError("research journal sequence is discontinuous")
        if record["previous_record_fingerprint"] != previous:
            raise ValueError("research journal predecessor chain is invalid")
        payload_fingerprint, payload = _validate_payload(record["kind"], record["payload"])
        if record["payload_fingerprint"] != payload_fingerprint:
            raise ValueError("research journal payload identity does not match")
        body = {key: value for key, value in record.items() if key != "record_fingerprint"}
        calculated = domain_sha256_v2(RESEARCH_JOURNAL_RECORD_SCHEMA_NAME, body)
        if record["record_fingerprint"] != calculated:
            raise ValueError("research journal record fingerprint is invalid")
        normalized_record = {**body, "payload": payload, "record_fingerprint": calculated}
        normalized.append(normalized_record)
        previous = calculated
    return normalized


def _truncate_partial_tail(path: Path, complete_byte_count: int) -> None:
    with path.open("r+b") as handle:
        handle.truncate(complete_byte_count)
        handle.flush()
        os.fsync(handle.fileno())
    fsync_directory(path.parent)


def recover_research_journal(
    path: str | Path, *, repair_partial_tail: bool = False
) -> dict[str, Any]:
    """Recover complete records, optionally truncating only an incomplete final record."""
    candidate = Path(path)
    outcome = read_complete_jsonl(
        candidate,
        max_bytes=MAX_JOURNAL_BYTES,
        version_field="schema_version",
        current_version=RESEARCH_JOURNAL_RECORD_SCHEMA_VERSION,
    )
    if outcome["state"] in {"corrupt", "oversized"}:
        raise ValueError(f"research journal is {outcome['state']}")
    records = _validate_records(outcome["records"])
    repaired = False
    if outcome["state"] == "incomplete" and repair_partial_tail:
        _truncate_partial_tail(candidate, outcome["complete_byte_count"])
        repaired = True
    return {
        "state": "current_valid" if repaired else outcome["state"],
        "records": records,
        "record_count": len(records),
        "last_record_fingerprint": None if not records else records[-1]["record_fingerprint"],
        "partial_tail_repaired": repaired,
    }


def append_research_journal_record(
    path: str | Path,
    kind: str,
    payload: object,
    *,
    expected_previous_record_fingerprint: str | None,
) -> dict[str, Any]:
    """Append one fsync'd record only when the caller owns the exact journal tail."""
    recovered = recover_research_journal(path, repair_partial_tail=True)
    previous = recovered["last_record_fingerprint"]
    if expected_previous_record_fingerprint != previous:
        raise ValueError("research journal predecessor is stale")
    payload_fingerprint, normalized_payload = _validate_payload(kind, payload)
    body = {
        "schema_name": RESEARCH_JOURNAL_RECORD_SCHEMA_NAME,
        "schema_version": RESEARCH_JOURNAL_RECORD_SCHEMA_VERSION,
        "sequence": recovered["record_count"],
        "kind": kind,
        "payload": normalized_payload,
        "payload_fingerprint": payload_fingerprint,
        "previous_record_fingerprint": previous,
    }
    record = {
        **body,
        "record_fingerprint": domain_sha256_v2(RESEARCH_JOURNAL_RECORD_SCHEMA_NAME, body),
    }
    append_jsonl_record(path, record)
    return record


__all__ = [
    "RESEARCH_JOURNAL_RECORD_SCHEMA_NAME",
    "RESEARCH_JOURNAL_RECORD_SCHEMA_VERSION",
    "append_research_journal_record",
    "recover_research_journal",
]
