"""Durable started and terminal evaluation records for research candidates."""

from __future__ import annotations

import re
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .contracts import _bounded_json, _identifier, _object, _optional_text, _timestamp

EVALUATION_RECORD_SCHEMA_NAME = "comsol_mcp.research_evaluation"
EVALUATION_RECORD_SCHEMA_VERSION = "1.0.0"
MAX_EVIDENCE = 512
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STATUSES = {"started", "completed", "failed", "infeasible", "cancelled"}
_TERMINAL_STATUSES = _STATUSES - {"started"}


def _fingerprint(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def normalize_evaluation_record(value: object) -> dict[str, Any]:
    """Normalize one evaluation state transition without interpreting its response."""
    raw = _object(
        _bounded_json(value, "evaluation record", 2 * 1024 * 1024),
        {
            "schema_name",
            "schema_version",
            "evaluation_id",
            "campaign_fingerprint",
            "candidate_id",
            "candidate_fingerprint",
            "attempt",
            "status",
            "fidelity",
            "evaluator_identity",
            "started_at",
            "completed_at",
            "response",
            "evidence_fingerprints",
            "failure_reason",
        },
        "evaluation record",
    )
    if (
        raw["schema_name"] != EVALUATION_RECORD_SCHEMA_NAME
        or raw["schema_version"] != EVALUATION_RECORD_SCHEMA_VERSION
    ):
        raise ValueError("evaluation schema identity is unsupported")
    attempt = raw["attempt"]
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 1_000_000:
        raise ValueError("attempt must be a bounded positive integer")
    status = raw["status"]
    if status not in _STATUSES:
        raise ValueError("evaluation status is unsupported")
    completed_at = (
        None if raw["completed_at"] is None else _timestamp(raw["completed_at"], "completed_at")
    )
    failure_reason = _optional_text(raw["failure_reason"], "failure_reason", maximum=2048)
    response = _bounded_json(raw["response"], "response", 1024 * 1024)
    if status == "started" and (
        completed_at is not None or response is not None or failure_reason is not None
    ):
        raise ValueError("started evaluations cannot contain terminal fields")
    if status == "completed" and (
        completed_at is None or response is None or failure_reason is not None
    ):
        raise ValueError("completed evaluations require response and completion time only")
    if status in _TERMINAL_STATUSES - {"completed"} and (
        completed_at is None or failure_reason is None
    ):
        raise ValueError("unsuccessful terminal evaluations require time and failure reason")
    evidence = raw["evidence_fingerprints"]
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE:
        raise ValueError("evidence_fingerprints must be a bounded list")
    normalized_evidence = [
        _fingerprint(item, f"evidence_fingerprints[{index}]") for index, item in enumerate(evidence)
    ]
    if len(normalized_evidence) != len(set(normalized_evidence)):
        raise ValueError("evidence_fingerprints must be unique")
    body = {
        "schema_name": EVALUATION_RECORD_SCHEMA_NAME,
        "schema_version": EVALUATION_RECORD_SCHEMA_VERSION,
        "evaluation_id": _identifier(raw["evaluation_id"], "evaluation_id"),
        "campaign_fingerprint": _fingerprint(raw["campaign_fingerprint"], "campaign_fingerprint"),
        "candidate_id": _identifier(raw["candidate_id"], "candidate_id"),
        "candidate_fingerprint": _fingerprint(
            raw["candidate_fingerprint"], "candidate_fingerprint"
        ),
        "attempt": attempt,
        "status": status,
        "fidelity": _identifier(raw["fidelity"], "fidelity"),
        "evaluator_identity": _fingerprint(raw["evaluator_identity"], "evaluator_identity"),
        "started_at": _timestamp(raw["started_at"], "started_at"),
        "completed_at": completed_at,
        "response": response,
        "evidence_fingerprints": sorted(normalized_evidence),
        "failure_reason": failure_reason,
    }
    return {
        **body,
        "evaluation_fingerprint": domain_sha256_v2(EVALUATION_RECORD_SCHEMA_NAME, body),
    }


__all__ = [
    "EVALUATION_RECORD_SCHEMA_NAME",
    "EVALUATION_RECORD_SCHEMA_VERSION",
    "normalize_evaluation_record",
]
