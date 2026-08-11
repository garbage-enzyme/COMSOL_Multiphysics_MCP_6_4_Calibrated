"""Combined alpha7.2 component and directional gradient acceptance."""

from __future__ import annotations

from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .derivative_support import _bounded_json, _finite, _object

ROBUST_GRADIENT_POLICY_SCHEMA_NAME = "comsol_mcp.robust_gradient_acceptance_policy"
ROBUST_GRADIENT_POLICY_SCHEMA_VERSION = "1.0.0"
ROBUST_GRADIENT_RECEIPT_SCHEMA_NAME = "comsol_mcp.robust_gradient_acceptance_receipt"
ROBUST_GRADIENT_RECEIPT_SCHEMA_VERSION = "1.0.0"


def normalize_robust_gradient_policy(value: object) -> dict[str, Any]:
    """Normalize caller-owned independent gradient acceptance thresholds."""
    bounded = _bounded_json(value, "robust gradient policy", 64 * 1024)
    supplied = None
    if isinstance(bounded, dict) and "policy_fingerprint" in bounded:
        supplied = bounded.pop("policy_fingerprint")
    raw = _object(
        bounded,
        {
            "schema_name",
            "schema_version",
            "component_relative_error_limit",
            "directional_relative_error_limit",
            "cosine_floor",
            "require_sign",
            "required_relative_steps",
        },
        "robust gradient policy",
    )
    if (
        raw["schema_name"] != ROBUST_GRADIENT_POLICY_SCHEMA_NAME
        or raw["schema_version"] != ROBUST_GRADIENT_POLICY_SCHEMA_VERSION
    ):
        raise ValueError("robust gradient policy schema identity is unsupported")
    component_limit = _finite(
        raw["component_relative_error_limit"], "component_relative_error_limit"
    )
    directional_limit = _finite(
        raw["directional_relative_error_limit"], "directional_relative_error_limit"
    )
    cosine_floor = _finite(raw["cosine_floor"], "cosine_floor")
    if not 0.0 <= component_limit <= 1.0 or not 0.0 <= directional_limit <= 1.0:
        raise ValueError("robust gradient relative-error limits are outside the allowed range")
    if not -1.0 <= cosine_floor <= 1.0:
        raise ValueError("robust gradient cosine floor is outside the allowed range")
    if not isinstance(raw["require_sign"], bool):
        raise ValueError("robust gradient require_sign must be boolean")
    steps = raw["required_relative_steps"]
    if not isinstance(steps, list) or len(steps) != 3:
        raise ValueError("robust gradient policy requires exactly three relative steps")
    normalized_steps = [_finite(item, "required_relative_steps", positive=True) for item in steps]
    if normalized_steps != sorted(set(normalized_steps), reverse=True):
        raise ValueError("robust gradient relative steps must be unique and descending")
    body = {
        "schema_name": ROBUST_GRADIENT_POLICY_SCHEMA_NAME,
        "schema_version": ROBUST_GRADIENT_POLICY_SCHEMA_VERSION,
        "component_relative_error_limit": component_limit,
        "directional_relative_error_limit": directional_limit,
        "cosine_floor": cosine_floor,
        "require_sign": raw["require_sign"],
        "required_relative_steps": normalized_steps,
    }
    body["policy_fingerprint"] = domain_sha256_v2(ROBUST_GRADIENT_POLICY_SCHEMA_NAME, body)
    if supplied is not None and supplied != body["policy_fingerprint"]:
        raise ValueError("robust gradient policy fingerprint is invalid")
    return body


def _verified_receipt(value: object, *, directional: bool) -> dict[str, Any]:
    name = "directional gradient receipt" if directional else "component gradient receipt"
    bounded = _bounded_json(value, name, 2 * 1024 * 1024)
    if not isinstance(bounded, dict):
        raise ValueError(f"{name} must be an object")
    fingerprint_field = "directional_check_fingerprint" if directional else "check_fingerprint"
    fingerprint = bounded.pop(fingerprint_field, None)
    if not isinstance(fingerprint, str):
        raise ValueError(f"{name} fingerprint is missing")
    domain = "comsol_mcp.directional_gradient_check" if directional else "comsol_mcp.gradient_check"
    if fingerprint != domain_sha256_v2(domain, bounded):
        raise ValueError(f"{name} fingerprint is invalid")
    return {**bounded, fingerprint_field: fingerprint}


def assess_robust_gradient_acceptance(
    policy: object, component_receipt: object, directional_receipt: object
) -> dict[str, Any]:
    """Bind component, cosine, sign, step, and directional checks into one result."""
    normalized_policy = normalize_robust_gradient_policy(policy)
    component = _verified_receipt(component_receipt, directional=False)
    directional = _verified_receipt(directional_receipt, directional=True)
    if component.get("schema_name") != "comsol_mcp.gradient_check":
        raise ValueError("component gradient receipt schema is unsupported")
    if directional.get("schema_name") != "comsol_mcp.gradient_check":
        raise ValueError("directional gradient receipt schema is unsupported")
    if component.get("gradient_fingerprint") != directional.get("gradient_fingerprint"):
        raise ValueError("component and directional receipts bind different gradients")
    rows = component.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("component gradient receipt has no variable rows")
    required_steps = normalized_policy["required_relative_steps"]
    step_match = True
    component_errors = []
    component_signs = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("steps"), list):
            raise ValueError("component gradient row structure is invalid")
        row_steps = [item.get("relative_step") for item in row["steps"]]
        step_match = step_match and row_steps == required_steps
        selected = row.get("selected")
        if not isinstance(selected, dict):
            raise ValueError("component gradient row selection is invalid")
        component_errors.append(_finite(selected.get("relative_error"), "relative_error"))
        sign = selected.get("sign_agreement")
        if not isinstance(sign, bool):
            raise ValueError("component gradient sign evidence is invalid")
        component_signs.append(sign)
    cosine = _finite(component.get("cosine_similarity"), "cosine_similarity")
    directional_error = _finite(directional.get("relative_error"), "directional.relative_error")
    directional_sign = directional.get("sign_agreement")
    if not isinstance(directional_sign, bool):
        raise ValueError("directional gradient sign evidence is invalid")
    checks = {
        "three_step_policy_matches": step_match,
        "component_relative_errors_within_limit": all(
            item <= normalized_policy["component_relative_error_limit"] for item in component_errors
        ),
        "cosine_above_floor": cosine >= normalized_policy["cosine_floor"],
        "component_signs_agree": all(component_signs),
        "directional_relative_error_within_limit": (
            directional_error <= normalized_policy["directional_relative_error_limit"]
        ),
        "directional_sign_agrees": directional_sign,
    }
    sign_checks = checks["component_signs_agree"] and checks["directional_sign_agrees"]
    passed = all(
        value
        for key, value in checks.items()
        if key not in {"component_signs_agree", "directional_sign_agrees"}
    ) and (sign_checks or not normalized_policy["require_sign"])
    body = {
        "schema_name": ROBUST_GRADIENT_RECEIPT_SCHEMA_NAME,
        "schema_version": ROBUST_GRADIENT_RECEIPT_SCHEMA_VERSION,
        "policy_fingerprint": normalized_policy["policy_fingerprint"],
        "gradient_fingerprint": component["gradient_fingerprint"],
        "component_check_fingerprint": component["check_fingerprint"],
        "directional_check_fingerprint": directional["directional_check_fingerprint"],
        "component_relative_errors": component_errors,
        "cosine_similarity": cosine,
        "directional_relative_error": directional_error,
        "checks": checks,
        "passed": passed,
    }
    return {
        **body,
        "receipt_fingerprint": domain_sha256_v2(ROBUST_GRADIENT_RECEIPT_SCHEMA_NAME, body),
    }


__all__ = [
    "ROBUST_GRADIENT_POLICY_SCHEMA_NAME",
    "ROBUST_GRADIENT_POLICY_SCHEMA_VERSION",
    "ROBUST_GRADIENT_RECEIPT_SCHEMA_NAME",
    "ROBUST_GRADIENT_RECEIPT_SCHEMA_VERSION",
    "assess_robust_gradient_acceptance",
    "normalize_robust_gradient_policy",
]
