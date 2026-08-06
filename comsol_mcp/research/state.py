"""Optimizer checkpoint and candidate portfolio contracts."""

from __future__ import annotations

import re
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .contracts import _bounded_json, _identifier, _object, _text, _timestamp

OPTIMIZER_CHECKPOINT_SCHEMA_NAME = "comsol_mcp.research_optimizer_checkpoint"
OPTIMIZER_CHECKPOINT_SCHEMA_VERSION = "1.0.0"
PORTFOLIO_SCHEMA_NAME = "comsol_mcp.research_portfolio"
PORTFOLIO_SCHEMA_VERSION = "1.0.0"
MAX_HISTORY_ITEMS = 4096
MAX_PORTFOLIO_ITEMS = 4096
MAX_EVIDENCE_ITEMS = 512
MAX_RISKS = 256
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DISPOSITIONS = {"proposed", "simulated", "converged", "independently_checked", "rejected"}


def _fingerprint(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def _fingerprint_list(value: object, name: str, maximum: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{name} must be a bounded list")
    normalized = [_fingerprint(item, f"{name}[{index}]") for index, item in enumerate(value)]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{name} must contain unique values")
    return sorted(normalized)


def _identifier_list(value: object, name: str, maximum: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{name} must be a bounded list")
    normalized = [_identifier(item, f"{name}[{index}]") for index, item in enumerate(value)]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{name} must contain unique values")
    return sorted(normalized)


def normalize_optimizer_checkpoint(value: object) -> dict[str, Any]:
    """Normalize backend-neutral optimizer state without importing its backend."""
    raw = _object(
        _bounded_json(value, "optimizer checkpoint", 2 * 1024 * 1024),
        {
            "schema_name",
            "schema_version",
            "campaign_fingerprint",
            "sequence",
            "decision_fingerprint",
            "backend",
            "random_state",
            "optimizer_state",
            "history_fingerprint",
            "candidate_fingerprints",
            "created_at",
        },
        "optimizer checkpoint",
    )
    if (
        raw["schema_name"] != OPTIMIZER_CHECKPOINT_SCHEMA_NAME
        or raw["schema_version"] != OPTIMIZER_CHECKPOINT_SCHEMA_VERSION
    ):
        raise ValueError("optimizer checkpoint schema identity is unsupported")
    sequence = raw["sequence"]
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or not 0 <= sequence <= 1_000_000_000
    ):
        raise ValueError("sequence must be a bounded nonnegative integer")
    backend = _object(raw["backend"], {"name", "version", "identity"}, "backend")
    body = {
        "schema_name": OPTIMIZER_CHECKPOINT_SCHEMA_NAME,
        "schema_version": OPTIMIZER_CHECKPOINT_SCHEMA_VERSION,
        "campaign_fingerprint": _fingerprint(raw["campaign_fingerprint"], "campaign_fingerprint"),
        "sequence": sequence,
        "decision_fingerprint": _fingerprint(raw["decision_fingerprint"], "decision_fingerprint"),
        "backend": {
            "name": _identifier(backend["name"], "backend.name"),
            "version": _text(backend["version"], "backend.version", maximum=128),
            "identity": _fingerprint(backend["identity"], "backend.identity"),
        },
        "random_state": _bounded_json(raw["random_state"], "random_state", 256 * 1024),
        "optimizer_state": _bounded_json(raw["optimizer_state"], "optimizer_state", 1024 * 1024),
        "history_fingerprint": _fingerprint(raw["history_fingerprint"], "history_fingerprint"),
        "candidate_fingerprints": _fingerprint_list(
            raw["candidate_fingerprints"], "candidate_fingerprints", MAX_HISTORY_ITEMS
        ),
        "created_at": _timestamp(raw["created_at"], "created_at"),
    }
    return {
        **body,
        "checkpoint_fingerprint": domain_sha256_v2(OPTIMIZER_CHECKPOINT_SCHEMA_NAME, body),
    }


def normalize_portfolio(value: object) -> dict[str, Any]:
    """Normalize a bounded candidate portfolio without assigning scientific truth."""
    raw = _object(
        _bounded_json(value, "research portfolio", 4 * 1024 * 1024),
        {
            "schema_name",
            "schema_version",
            "campaign_fingerprint",
            "items",
            "selected_candidate_ids",
            "created_at",
        },
        "research portfolio",
    )
    if (
        raw["schema_name"] != PORTFOLIO_SCHEMA_NAME
        or raw["schema_version"] != PORTFOLIO_SCHEMA_VERSION
    ):
        raise ValueError("portfolio schema identity is unsupported")
    items_value = raw["items"]
    if not isinstance(items_value, list) or len(items_value) > MAX_PORTFOLIO_ITEMS:
        raise ValueError("items must be a bounded list")
    items = []
    for index, item_value in enumerate(items_value):
        name = f"items[{index}]"
        item = _object(
            item_value,
            {
                "candidate_id",
                "candidate_fingerprint",
                "disposition",
                "objective_values",
                "evidence_fingerprints",
                "unresolved_risks",
                "strictly_verified",
            },
            name,
        )
        if item["disposition"] not in _DISPOSITIONS:
            raise ValueError(f"{name}.disposition is unsupported")
        if not isinstance(item["strictly_verified"], bool):
            raise ValueError(f"{name}.strictly_verified must be boolean")
        risks = item["unresolved_risks"]
        if not isinstance(risks, list) or len(risks) > MAX_RISKS:
            raise ValueError(f"{name}.unresolved_risks must be a bounded list")
        normalized_risks = [
            _text(risk, f"{name}.unresolved_risks[{risk_index}]", maximum=1024)
            for risk_index, risk in enumerate(risks)
        ]
        if len(normalized_risks) != len(set(normalized_risks)):
            raise ValueError(f"{name}.unresolved_risks must be unique")
        if item["strictly_verified"] and normalized_risks:
            raise ValueError(f"{name} cannot be strictly verified with unresolved risks")
        items.append(
            {
                "candidate_id": _identifier(item["candidate_id"], f"{name}.candidate_id"),
                "candidate_fingerprint": _fingerprint(
                    item["candidate_fingerprint"], f"{name}.candidate_fingerprint"
                ),
                "disposition": item["disposition"],
                "objective_values": _bounded_json(
                    item["objective_values"], f"{name}.objective_values", 64 * 1024
                ),
                "evidence_fingerprints": _fingerprint_list(
                    item["evidence_fingerprints"],
                    f"{name}.evidence_fingerprints",
                    MAX_EVIDENCE_ITEMS,
                ),
                "unresolved_risks": sorted(normalized_risks),
                "strictly_verified": item["strictly_verified"],
            }
        )
    candidate_ids = [item["candidate_id"] for item in items]
    candidate_fingerprints = [item["candidate_fingerprint"] for item in items]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("portfolio candidate IDs must be unique")
    if len(candidate_fingerprints) != len(set(candidate_fingerprints)):
        raise ValueError("portfolio candidate fingerprints must be unique")
    selected = _identifier_list(
        raw["selected_candidate_ids"], "selected_candidate_ids", MAX_PORTFOLIO_ITEMS
    )
    by_id = {item["candidate_id"]: item for item in items}
    if not set(selected) <= set(by_id):
        raise ValueError("selected candidates must exist in the portfolio")
    if any(by_id[candidate_id]["disposition"] == "rejected" for candidate_id in selected):
        raise ValueError("rejected candidates cannot be selected")
    body = {
        "schema_name": PORTFOLIO_SCHEMA_NAME,
        "schema_version": PORTFOLIO_SCHEMA_VERSION,
        "campaign_fingerprint": _fingerprint(raw["campaign_fingerprint"], "campaign_fingerprint"),
        "items": sorted(items, key=lambda item: item["candidate_id"]),
        "selected_candidate_ids": selected,
        "created_at": _timestamp(raw["created_at"], "created_at"),
    }
    return {**body, "portfolio_fingerprint": domain_sha256_v2(PORTFOLIO_SCHEMA_NAME, body)}


__all__ = [
    "OPTIMIZER_CHECKPOINT_SCHEMA_NAME",
    "OPTIMIZER_CHECKPOINT_SCHEMA_VERSION",
    "PORTFOLIO_SCHEMA_NAME",
    "PORTFOLIO_SCHEMA_VERSION",
    "normalize_optimizer_checkpoint",
    "normalize_portfolio",
]
