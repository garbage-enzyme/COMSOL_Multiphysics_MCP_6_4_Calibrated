"""Canonical candidate proposal records for bounded research campaigns."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .contracts import (
    _bounded_json,
    _identifier,
    _object,
    _optional_text,
    _text,
    normalize_design_space,
)

CANDIDATE_RECORD_SCHEMA_NAME = "comsol_mcp.research_candidate"
CANDIDATE_RECORD_SCHEMA_VERSION = "1.0.0"
MAX_PARENTS = 64
MAX_PREFLIGHT_RESULTS = 256
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STATES = {
    "proposed",
    "preflight_rejected",
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    "dominated",
}
_TERMINAL_STATES = {"preflight_rejected", "completed", "failed", "cancelled", "dominated"}


def _fingerprint(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def _candidate_values(value: object, space: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError("candidate values must be an object with string keys")
    variables = {item["variable_id"]: item for item in space["variables"]}
    if set(value) != set(variables):
        raise ValueError("candidate values must exactly cover the design-space variables")
    digits = space["canonicalization"]["float_digits"]
    normalized: dict[str, Any] = {}
    for variable_id, variable in variables.items():
        raw = value[variable_id]
        kind = variable["kind"]
        if kind in {"categorical", "ordinal"}:
            encoded = _bounded_json(raw, f"candidate.{variable_id}", 4096)
            allowed = variable["allowed_values"] or []
            encoded_identity = json.dumps(
                encoded, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            allowed_identities = {
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                for item in allowed
            }
            if encoded_identity not in allowed_identities:
                raise ValueError(f"candidate.{variable_id} is outside allowed_values")
            normalized[variable_id] = encoded
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"candidate.{variable_id} must be numeric")
        number = float(raw)
        if not math.isfinite(number):
            raise ValueError(f"candidate.{variable_id} must be finite")
        if kind == "integer":
            if not number.is_integer():
                raise ValueError(f"candidate.{variable_id} must be an integer")
            normalized_value: int | float = int(number)
        else:
            normalized_value = round(number, digits)
        if not variable["lower"] <= normalized_value <= variable["upper"]:
            raise ValueError(f"candidate.{variable_id} is outside the frozen bounds")
        normalized[variable_id] = normalized_value
    return {key: normalized[key] for key in sorted(normalized)}


def normalize_candidate_record(value: object, design_space: object) -> dict[str, Any]:
    """Normalize a candidate proposal before geometry or solver side effects."""
    space = normalize_design_space(design_space)
    raw = _object(
        _bounded_json(value, "candidate record", 512 * 1024),
        {
            "schema_name",
            "schema_version",
            "candidate_id",
            "campaign_fingerprint",
            "requested_values",
            "parent_candidate_ids",
            "proposal_reason",
            "hypothesis",
            "preflight_results",
            "predicted_resource_class",
            "requested_fidelity",
            "producer_identity",
            "optimizer_identity",
            "random_seed",
            "lifecycle_state",
            "terminal_reason",
        },
        "candidate record",
    )
    if (
        raw["schema_name"] != CANDIDATE_RECORD_SCHEMA_NAME
        or raw["schema_version"] != CANDIDATE_RECORD_SCHEMA_VERSION
    ):
        raise ValueError("candidate schema identity is unsupported")
    normalized_values = _candidate_values(raw["requested_values"], space)
    parents = raw["parent_candidate_ids"]
    if not isinstance(parents, list) or len(parents) > MAX_PARENTS:
        raise ValueError("parent_candidate_ids must be a bounded list")
    normalized_parents = [
        _identifier(item, f"parent_candidate_ids[{index}]") for index, item in enumerate(parents)
    ]
    if len(normalized_parents) != len(set(normalized_parents)):
        raise ValueError("parent_candidate_ids must be unique")
    preflight = raw["preflight_results"]
    if not isinstance(preflight, list) or len(preflight) > MAX_PREFLIGHT_RESULTS:
        raise ValueError("preflight_results must be a bounded list")
    normalized_preflight: list[dict[str, Any]] = []
    for index, result_value in enumerate(preflight):
        name = f"preflight_results[{index}]"
        result = _object(result_value, {"constraint_id", "passed", "reason_code"}, name)
        if not isinstance(result["passed"], bool):
            raise ValueError(f"{name}.passed must be boolean")
        normalized_preflight.append(
            {
                "constraint_id": _identifier(result["constraint_id"], f"{name}.constraint_id"),
                "passed": result["passed"],
                "reason_code": _identifier(result["reason_code"], f"{name}.reason_code"),
            }
        )
    constraint_ids = [item["constraint_id"] for item in normalized_preflight]
    if len(constraint_ids) != len(set(constraint_ids)):
        raise ValueError("preflight constraint IDs must be unique")
    state = raw["lifecycle_state"]
    if state not in _STATES:
        raise ValueError("lifecycle_state is unsupported")
    terminal_reason = _optional_text(raw["terminal_reason"], "terminal_reason", maximum=1024)
    if (state in _TERMINAL_STATES) != (terminal_reason is not None):
        raise ValueError("terminal states require one reason and nonterminal states require null")
    seed = raw["random_seed"]
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= (1 << 63) - 1:
        raise ValueError("random_seed must be a bounded nonnegative integer")
    campaign_fingerprint = _fingerprint(raw["campaign_fingerprint"], "campaign_fingerprint")
    point_identity = {
        "campaign_fingerprint": campaign_fingerprint,
        "space_fingerprint": space["space_fingerprint"],
        "normalized_values": normalized_values,
    }
    body = {
        "schema_name": CANDIDATE_RECORD_SCHEMA_NAME,
        "schema_version": CANDIDATE_RECORD_SCHEMA_VERSION,
        "candidate_id": _identifier(raw["candidate_id"], "candidate_id"),
        "campaign_fingerprint": campaign_fingerprint,
        "space_fingerprint": space["space_fingerprint"],
        "requested_values": _bounded_json(raw["requested_values"], "requested_values", 32 * 1024),
        "normalized_values": normalized_values,
        "parent_candidate_ids": sorted(normalized_parents),
        "proposal_reason": _text(raw["proposal_reason"], "proposal_reason", maximum=2048),
        "hypothesis": _bounded_json(raw["hypothesis"], "hypothesis", 32 * 1024),
        "preflight_results": sorted(normalized_preflight, key=lambda item: item["constraint_id"]),
        "predicted_resource_class": _identifier(
            raw["predicted_resource_class"], "predicted_resource_class"
        ),
        "requested_fidelity": _identifier(raw["requested_fidelity"], "requested_fidelity"),
        "producer_identity": _fingerprint(raw["producer_identity"], "producer_identity"),
        "optimizer_identity": _fingerprint(raw["optimizer_identity"], "optimizer_identity"),
        "random_seed": seed,
        "lifecycle_state": state,
        "terminal_reason": terminal_reason,
    }
    return {
        **body,
        "candidate_fingerprint": domain_sha256_v2(
            "comsol_mcp.research_candidate_point", point_identity
        ),
        "record_fingerprint": domain_sha256_v2(CANDIDATE_RECORD_SCHEMA_NAME, body),
    }


__all__ = [
    "CANDIDATE_RECORD_SCHEMA_NAME",
    "CANDIDATE_RECORD_SCHEMA_VERSION",
    "normalize_candidate_record",
]
