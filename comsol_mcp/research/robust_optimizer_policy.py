"""Manual robust native optimizer selection and evidence policy."""

from __future__ import annotations

from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .derivative_support import _bounded_json, _identifier, _object, _sha256

ROBUST_OPTIMIZER_POLICY_SCHEMA_NAME = "comsol_mcp.robust_optimizer_policy"
ROBUST_OPTIMIZER_POLICY_SCHEMA_VERSION = "1.0.0"

_METHODS = {"gcmma", "mma"}
_SUPPORT_STATES = {"validated", "structural_only", "restricted", "rejected"}


def _optional_sha256(value: object, name: str) -> str | None:
    return None if value is None else _sha256(value, name)


def normalize_robust_optimizer_policy(value: object) -> dict[str, Any]:
    """Normalize a method-bound policy with no automatic fallback."""
    bounded = _bounded_json(value, "robust optimizer policy", 128 * 1024)
    supplied = None
    if isinstance(bounded, dict) and "policy_fingerprint" in bounded:
        supplied = bounded.pop("policy_fingerprint")
    derived = None
    if isinstance(bounded, dict) and {
        "execution_allowed",
        "selection_disposition",
        "run_identity_mode",
    }.issubset(bounded):
        derived = (
            bounded.pop("execution_allowed"),
            bounded.pop("selection_disposition"),
            bounded.pop("run_identity_mode"),
        )
    raw = _object(
        bounded,
        {
            "schema_name",
            "schema_version",
            "policy_id",
            "allowed_methods",
            "selected_method",
            "automatic_fallback",
            "method_evidence",
        },
        "robust optimizer policy",
    )
    if (
        raw["schema_name"] != ROBUST_OPTIMIZER_POLICY_SCHEMA_NAME
        or raw["schema_version"] != ROBUST_OPTIMIZER_POLICY_SCHEMA_VERSION
    ):
        raise ValueError("robust optimizer policy schema identity is unsupported")
    allowed = raw["allowed_methods"]
    if not isinstance(allowed, list) or not allowed:
        raise ValueError("allowed_methods must be a nonempty list")
    normalized_allowed = sorted({_identifier(item, "allowed_methods") for item in allowed})
    if len(normalized_allowed) != len(allowed) or not set(normalized_allowed).issubset(_METHODS):
        raise ValueError("allowed_methods must contain unique reviewed GCMMA/MMA methods")
    selected = _identifier(raw["selected_method"], "selected_method")
    if selected not in normalized_allowed:
        raise ValueError("selected_method must be explicitly allowed")
    if raw["automatic_fallback"] is not False:
        raise ValueError("robust optimizer automatic fallback is forbidden")
    evidence = raw["method_evidence"]
    if not isinstance(evidence, list) or len(evidence) != len(normalized_allowed):
        raise ValueError("method_evidence must exactly cover allowed_methods")
    normalized_evidence = []
    for index, item in enumerate(evidence):
        name = f"method_evidence[{index}]"
        entry = _object(item, {"method", "support_state", "evidence_sha256"}, name)
        method = _identifier(entry["method"], f"{name}.method")
        state = entry["support_state"]
        if state not in _SUPPORT_STATES:
            raise ValueError(f"{name}.support_state is unsupported")
        evidence_sha256 = _optional_sha256(entry["evidence_sha256"], f"{name}.evidence_sha256")
        if state == "validated" and evidence_sha256 is None:
            raise ValueError("validated optimizer methods require evidence_sha256")
        normalized_evidence.append(
            {"method": method, "support_state": state, "evidence_sha256": evidence_sha256}
        )
    normalized_evidence.sort(key=lambda item: item["method"])
    if [item["method"] for item in normalized_evidence] != normalized_allowed:
        raise ValueError("method_evidence must exactly cover allowed_methods")
    selected_evidence = next(item for item in normalized_evidence if item["method"] == selected)
    execution_allowed = selected_evidence["support_state"] == "validated"
    disposition = "validated_manual_selection" if execution_allowed else "validation_required"
    expected_derived = (execution_allowed, disposition, "optimizer_method_bound")
    if derived is not None and derived != expected_derived:
        raise ValueError("robust optimizer derived selection fields are invalid")
    body = {
        "schema_name": ROBUST_OPTIMIZER_POLICY_SCHEMA_NAME,
        "schema_version": ROBUST_OPTIMIZER_POLICY_SCHEMA_VERSION,
        "policy_id": _identifier(raw["policy_id"], "policy_id"),
        "allowed_methods": normalized_allowed,
        "selected_method": selected,
        "automatic_fallback": False,
        "method_evidence": normalized_evidence,
        "execution_allowed": execution_allowed,
        "selection_disposition": disposition,
        "run_identity_mode": "optimizer_method_bound",
    }
    body["policy_fingerprint"] = domain_sha256_v2(ROBUST_OPTIMIZER_POLICY_SCHEMA_NAME, body)
    if supplied is not None and supplied != body["policy_fingerprint"]:
        raise ValueError("robust optimizer policy fingerprint is invalid")
    return body


__all__ = [
    "ROBUST_OPTIMIZER_POLICY_SCHEMA_NAME",
    "ROBUST_OPTIMIZER_POLICY_SCHEMA_VERSION",
    "normalize_robust_optimizer_policy",
]
