"""Canonical hash-chained decisions for adaptive research campaigns."""

from __future__ import annotations

import math
import re
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .contracts import _bounded_json, _identifier, _object, _text, _timestamp

DECISION_RECORD_SCHEMA_NAME = "comsol_mcp.research_decision"
DECISION_RECORD_SCHEMA_VERSION = "1.0.0"
MAX_CANDIDATES_PER_DECISION = 256
MAX_EVIDENCE_PER_DECISION = 512
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACTIONS = {"propose", "evaluate", "promote", "reject", "pause", "resume", "stop"}
_STOP_REASONS = {
    "success",
    "infeasible",
    "stagnation",
    "budget_exhausted",
    "safety",
    "evidence_failure",
    "user_stop",
}


def _fingerprint(value: object, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def _bounded_integer(value: object, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be a bounded nonnegative integer")
    return value


def _fingerprint_list(value: object, name: str, maximum: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{name} must be a bounded list")
    normalized = [str(_fingerprint(item, f"{name}[{index}]")) for index, item in enumerate(value)]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{name} must contain unique values")
    return sorted(normalized)


def normalize_decision_record(value: object) -> dict[str, Any]:
    """Normalize one immutable adaptive decision and its chain predecessor."""
    raw = _object(
        _bounded_json(value, "decision record", 512 * 1024),
        {
            "schema_name",
            "schema_version",
            "decision_id",
            "campaign_fingerprint",
            "sequence",
            "previous_decision_fingerprint",
            "action",
            "candidate_ids",
            "reason_code",
            "rationale",
            "stop_reason",
            "budget",
            "evidence_fingerprints",
            "optimizer_checkpoint_fingerprint",
            "producer_identity",
            "created_at",
        },
        "decision record",
    )
    if (
        raw["schema_name"] != DECISION_RECORD_SCHEMA_NAME
        or raw["schema_version"] != DECISION_RECORD_SCHEMA_VERSION
    ):
        raise ValueError("decision schema identity is unsupported")
    sequence = _bounded_integer(raw["sequence"], "sequence", 1_000_000_000)
    previous = _fingerprint(
        raw["previous_decision_fingerprint"],
        "previous_decision_fingerprint",
        optional=True,
    )
    if (sequence == 0) != (previous is None):
        raise ValueError("only decision sequence zero may omit its predecessor")
    action = raw["action"]
    if action not in _ACTIONS:
        raise ValueError("action is unsupported")
    candidate_ids = raw["candidate_ids"]
    if not isinstance(candidate_ids, list) or len(candidate_ids) > MAX_CANDIDATES_PER_DECISION:
        raise ValueError("candidate_ids must be a bounded list")
    normalized_candidate_ids = [
        _identifier(item, f"candidate_ids[{index}]") for index, item in enumerate(candidate_ids)
    ]
    if len(normalized_candidate_ids) != len(set(normalized_candidate_ids)):
        raise ValueError("candidate_ids must be unique")
    if action in {"evaluate", "promote", "reject"} and not normalized_candidate_ids:
        raise ValueError(f"{action} decisions require at least one candidate")
    stop_reason = raw["stop_reason"]
    if action == "stop":
        if stop_reason not in _STOP_REASONS:
            raise ValueError("stop decisions require a supported stop_reason")
    elif stop_reason is not None:
        raise ValueError("non-stop decisions require stop_reason=null")
    budget = _object(
        raw["budget"],
        {
            "started_fem_evaluations",
            "max_fem_evaluations",
            "elapsed_wall_time_seconds",
            "max_wall_time_seconds",
        },
        "budget",
    )
    maximum_evaluations = _bounded_integer(
        budget["max_fem_evaluations"], "budget.max_fem_evaluations", 4096
    )
    if maximum_evaluations == 0:
        raise ValueError("budget.max_fem_evaluations must be positive")
    started_evaluations = _bounded_integer(
        budget["started_fem_evaluations"], "budget.started_fem_evaluations", maximum_evaluations
    )
    maximum_wall = _bounded_integer(
        budget["max_wall_time_seconds"], "budget.max_wall_time_seconds", 30 * 24 * 3600
    )
    if maximum_wall == 0:
        raise ValueError("budget.max_wall_time_seconds must be positive")
    elapsed = budget["elapsed_wall_time_seconds"]
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
        raise ValueError("budget.elapsed_wall_time_seconds must be finite and nonnegative")
    elapsed_value = float(elapsed)
    if not math.isfinite(elapsed_value) or elapsed_value < 0.0:
        raise ValueError("budget.elapsed_wall_time_seconds must be finite and nonnegative")
    normalized_budget = {
        "started_fem_evaluations": started_evaluations,
        "max_fem_evaluations": maximum_evaluations,
        "remaining_fem_evaluations": maximum_evaluations - started_evaluations,
        "elapsed_wall_time_seconds": elapsed_value,
        "max_wall_time_seconds": maximum_wall,
        "point_budget_exhausted": started_evaluations >= maximum_evaluations,
        "wall_budget_exhausted": elapsed_value >= maximum_wall,
    }
    body = {
        "schema_name": DECISION_RECORD_SCHEMA_NAME,
        "schema_version": DECISION_RECORD_SCHEMA_VERSION,
        "decision_id": _identifier(raw["decision_id"], "decision_id"),
        "campaign_fingerprint": _fingerprint(raw["campaign_fingerprint"], "campaign_fingerprint"),
        "sequence": sequence,
        "previous_decision_fingerprint": previous,
        "action": action,
        "candidate_ids": sorted(normalized_candidate_ids),
        "reason_code": _identifier(raw["reason_code"], "reason_code"),
        "rationale": _text(raw["rationale"], "rationale", maximum=4096),
        "stop_reason": stop_reason,
        "budget": normalized_budget,
        "evidence_fingerprints": _fingerprint_list(
            raw["evidence_fingerprints"],
            "evidence_fingerprints",
            MAX_EVIDENCE_PER_DECISION,
        ),
        "optimizer_checkpoint_fingerprint": _fingerprint(
            raw["optimizer_checkpoint_fingerprint"],
            "optimizer_checkpoint_fingerprint",
            optional=True,
        ),
        "producer_identity": _fingerprint(raw["producer_identity"], "producer_identity"),
        "created_at": _timestamp(raw["created_at"], "created_at"),
    }
    return {
        **body,
        "decision_fingerprint": domain_sha256_v2(DECISION_RECORD_SCHEMA_NAME, body),
    }


__all__ = [
    "DECISION_RECORD_SCHEMA_NAME",
    "DECISION_RECORD_SCHEMA_VERSION",
    "normalize_decision_record",
]
