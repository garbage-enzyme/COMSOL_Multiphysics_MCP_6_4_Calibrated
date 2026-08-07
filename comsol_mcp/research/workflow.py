"""Passive, cited, reviewable literature workflow capsules."""

from __future__ import annotations

import re
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .contracts import _bounded_json, _identifier, _object, _optional_text, _text, _timestamp

WORKFLOW_CAPSULE_SCHEMA_NAME = "comsol_mcp.workflow_capsule"
WORKFLOW_CAPSULE_SCHEMA_VERSION = "1.0.0"
MAX_CLAIMS = 256
MAX_AMBIGUITIES = 128
MAX_PRIORS = 128
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROVENANCE = {"stated", "inferred", "unavailable"}
_REVIEW_STATES = {"draft", "accepted", "rejected"}


def _optional_sha256(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 or null")
    return value


def _bounded_list(value: object, name: str, maximum: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{name} must be a bounded list")
    return value


def normalize_workflow_capsule(value: object) -> dict[str, Any]:
    """Normalize literature input as passive data without executing its contents."""
    raw = _object(
        _bounded_json(value, "workflow capsule", 512 * 1024),
        {
            "schema_name",
            "schema_version",
            "capsule_id",
            "source",
            "claims",
            "ambiguities",
            "template_mapping",
            "exploration_priors",
            "review",
            "baseline_receipt",
        },
        "workflow capsule",
    )
    if (
        raw["schema_name"] != WORKFLOW_CAPSULE_SCHEMA_NAME
        or raw["schema_version"] != WORKFLOW_CAPSULE_SCHEMA_VERSION
    ):
        raise ValueError("workflow capsule schema identity is unsupported")
    source = _object(
        raw["source"],
        {"citation", "doi_or_url", "accessed_at", "local_source_sha256"},
        "source",
    )
    normalized_source = {
        "citation": _text(source["citation"], "source.citation", maximum=2048),
        "doi_or_url": _optional_text(source["doi_or_url"], "source.doi_or_url", maximum=2048),
        "accessed_at": _timestamp(source["accessed_at"], "source.accessed_at"),
        "local_source_sha256": _optional_sha256(
            source["local_source_sha256"], "source.local_source_sha256"
        ),
    }
    claims = []
    for index, claim_value in enumerate(_bounded_list(raw["claims"], "claims", MAX_CLAIMS)):
        name = f"claims[{index}]"
        claim = _object(
            claim_value,
            {"claim_id", "topic", "value", "provenance", "locator"},
            name,
        )
        provenance = claim["provenance"]
        if provenance not in _PROVENANCE:
            raise ValueError(f"{name}.provenance is unsupported")
        if provenance == "unavailable" and claim["value"] is not None:
            raise ValueError(f"{name}.value must be null when unavailable")
        claims.append(
            {
                "claim_id": _identifier(claim["claim_id"], f"{name}.claim_id"),
                "topic": _identifier(claim["topic"], f"{name}.topic"),
                "value": _bounded_json(claim["value"], f"{name}.value", 32 * 1024),
                "provenance": provenance,
                "locator": _optional_text(claim["locator"], f"{name}.locator", maximum=512),
            }
        )
    claim_ids = [item["claim_id"] for item in claims]
    if len(claim_ids) != len(set(claim_ids)):
        raise ValueError("claim_id values must be unique")
    ambiguities = []
    for index, ambiguity_value in enumerate(
        _bounded_list(raw["ambiguities"], "ambiguities", MAX_AMBIGUITIES)
    ):
        name = f"ambiguities[{index}]"
        ambiguity = _object(
            ambiguity_value,
            {"ambiguity_id", "description"},
            name,
        )
        ambiguities.append(
            {
                "ambiguity_id": _identifier(ambiguity["ambiguity_id"], f"{name}.ambiguity_id"),
                "description": _text(ambiguity["description"], f"{name}.description", maximum=2048),
            }
        )
    ambiguity_ids = [item["ambiguity_id"] for item in ambiguities]
    if len(ambiguity_ids) != len(set(ambiguity_ids)):
        raise ValueError("ambiguity_id values must be unique")
    mapping = _object(
        raw["template_mapping"],
        {"structure_family", "adapter_id", "assumptions"},
        "template_mapping",
    )
    assumptions = [
        _identifier(item, f"template_mapping.assumptions[{index}]")
        for index, item in enumerate(
            _bounded_list(mapping["assumptions"], "template_mapping.assumptions", MAX_AMBIGUITIES)
        )
    ]
    if len(assumptions) != len(set(assumptions)) or not set(assumptions) <= set(ambiguity_ids):
        raise ValueError("template assumptions must uniquely reference declared ambiguities")
    priors = []
    for index, prior_value in enumerate(
        _bounded_list(raw["exploration_priors"], "exploration_priors", MAX_PRIORS)
    ):
        name = f"exploration_priors[{index}]"
        prior = _object(prior_value, {"prior_id", "variable_id", "value", "rationale"}, name)
        priors.append(
            {
                "prior_id": _identifier(prior["prior_id"], f"{name}.prior_id"),
                "variable_id": _identifier(prior["variable_id"], f"{name}.variable_id"),
                "value": _bounded_json(prior["value"], f"{name}.value", 16 * 1024),
                "rationale": _text(prior["rationale"], f"{name}.rationale", maximum=1024),
            }
        )
    prior_ids = [item["prior_id"] for item in priors]
    if len(prior_ids) != len(set(prior_ids)):
        raise ValueError("prior_id values must be unique")
    review = _object(
        raw["review"],
        {"status", "reviewer", "reviewed_at", "accepted_ambiguity_ids"},
        "review",
    )
    if review["status"] not in _REVIEW_STATES:
        raise ValueError("review.status is unsupported")
    accepted_ids = [
        _identifier(item, f"review.accepted_ambiguity_ids[{index}]")
        for index, item in enumerate(
            _bounded_list(
                review["accepted_ambiguity_ids"],
                "review.accepted_ambiguity_ids",
                MAX_AMBIGUITIES,
            )
        )
    ]
    if len(accepted_ids) != len(set(accepted_ids)) or not set(accepted_ids) <= set(ambiguity_ids):
        raise ValueError("accepted ambiguity IDs must uniquely reference declared ambiguities")
    reviewer = _optional_text(review["reviewer"], "review.reviewer", maximum=256)
    reviewed_at = (
        None
        if review["reviewed_at"] is None
        else _timestamp(review["reviewed_at"], "review.reviewed_at")
    )
    if review["status"] == "accepted" and (
        reviewer is None or reviewed_at is None or set(accepted_ids) != set(ambiguity_ids)
    ):
        raise ValueError("accepted capsules require reviewer, time, and all ambiguity decisions")
    receipt = raw["baseline_receipt"]
    normalized_receipt = None
    if receipt is not None:
        receipt_raw = _object(
            receipt,
            {"schema_name", "receipt_sha256", "source_identity_sha256"},
            "baseline_receipt",
        )
        normalized_receipt = {
            "schema_name": _identifier(receipt_raw["schema_name"], "baseline_receipt.schema_name"),
            "receipt_sha256": _optional_sha256(
                receipt_raw["receipt_sha256"], "baseline_receipt.receipt_sha256"
            ),
            "source_identity_sha256": _optional_sha256(
                receipt_raw["source_identity_sha256"],
                "baseline_receipt.source_identity_sha256",
            ),
        }
        if None in {
            normalized_receipt["receipt_sha256"],
            normalized_receipt["source_identity_sha256"],
        }:
            raise ValueError("baseline receipt hashes must be present")
    body = {
        "schema_name": WORKFLOW_CAPSULE_SCHEMA_NAME,
        "schema_version": WORKFLOW_CAPSULE_SCHEMA_VERSION,
        "capsule_id": _identifier(raw["capsule_id"], "capsule_id"),
        "source": normalized_source,
        "claims": sorted(claims, key=lambda item: item["claim_id"]),
        "ambiguities": sorted(ambiguities, key=lambda item: item["ambiguity_id"]),
        "template_mapping": {
            "structure_family": _identifier(
                mapping["structure_family"], "template_mapping.structure_family"
            ),
            "adapter_id": _identifier(mapping["adapter_id"], "template_mapping.adapter_id"),
            "assumptions": sorted(assumptions),
        },
        "exploration_priors": sorted(priors, key=lambda item: item["prior_id"]),
        "review": {
            "status": review["status"],
            "reviewer": reviewer,
            "reviewed_at": reviewed_at,
            "accepted_ambiguity_ids": sorted(accepted_ids),
        },
        "baseline_receipt": normalized_receipt,
        "exploration_ready": review["status"] == "accepted" and normalized_receipt is not None,
    }
    return {
        **body,
        "workflow_fingerprint": domain_sha256_v2(WORKFLOW_CAPSULE_SCHEMA_NAME, body),
    }


__all__ = [
    "WORKFLOW_CAPSULE_SCHEMA_NAME",
    "WORKFLOW_CAPSULE_SCHEMA_VERSION",
    "normalize_workflow_capsule",
]
