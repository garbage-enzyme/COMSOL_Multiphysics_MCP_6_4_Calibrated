"""Combined alpha7.2 component and directional gradient acceptance."""

from __future__ import annotations

from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .derivative_support import _bounded_json, _finite, _identifier, _object, _sha256

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
        component_error = _finite(selected.get("relative_error"), "relative_error")
        if component_error < 0.0:
            raise ValueError("component relative error must be nonnegative")
        component_errors.append(component_error)
        sign = selected.get("sign_agreement")
        if not isinstance(sign, bool):
            raise ValueError("component gradient sign evidence is invalid")
        component_signs.append(sign)
    cosine = _finite(component.get("cosine_similarity"), "cosine_similarity")
    if not -1.0 <= cosine <= 1.0:
        raise ValueError("component cosine similarity is outside [-1, 1]")
    directional_error = _finite(directional.get("relative_error"), "directional.relative_error")
    if directional_error < 0.0:
        raise ValueError("directional relative error must be nonnegative")
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


def _licensed_receipt(value: object, name: str) -> dict[str, Any]:
    receipt = _bounded_json(value, name, 2 * 1024 * 1024)
    if not isinstance(receipt, dict) or receipt.get("success") is not True:
        raise ValueError(f"{name} must be a successful bounded receipt")
    revision = receipt.get("source_revision")
    if (
        not isinstance(revision, str)
        or len(revision) != 40
        or any(character not in "0123456789abcdef" for character in revision.casefold())
    ):
        raise ValueError(f"{name} source revision is invalid")
    receipt["source_revision"] = revision.casefold()
    receipt["source_sha256"] = _sha256(receipt.get("source_sha256"), f"{name}.source_sha256")
    return receipt


def assess_licensed_gradient_ladder(
    policy: object,
    native_receipt: object,
    finite_difference_receipt: object,
    directional_receipt: object,
    *,
    native_receipt_sha256: object,
    finite_difference_receipt_sha256: object,
    directional_receipt_sha256: object,
) -> dict[str, Any]:
    """Recompute canonical robust acceptance from licensed S4 ladder receipts."""
    normalized_policy = normalize_robust_gradient_policy(policy)
    native = _licensed_receipt(native_receipt, "native gradient receipt")
    finite_difference = _licensed_receipt(finite_difference_receipt, "finite-difference receipt")
    directional = _licensed_receipt(directional_receipt, "directional receipt")
    source_identity = (native["source_revision"], native["source_sha256"])
    if any(
        (receipt["source_revision"], receipt["source_sha256"]) != source_identity
        for receipt in (finite_difference, directional)
    ):
        raise ValueError("licensed gradient ladder receipts bind different sources")
    native_sha = _sha256(native_receipt_sha256, "native_receipt_sha256")
    finite_difference_sha = _sha256(
        finite_difference_receipt_sha256, "finite_difference_receipt_sha256"
    )
    directional_sha = _sha256(directional_receipt_sha256, "directional_receipt_sha256")
    native_rows = native.get("derivatives")
    if not isinstance(native_rows, list) or not native_rows:
        raise ValueError("native gradient receipt has no derivative rows")
    variable_order: list[str] = []
    native_values: list[float] = []
    for index, row in enumerate(native_rows):
        if not isinstance(row, dict):
            raise ValueError("native gradient derivative row is invalid")
        variable_order.append(
            _identifier(row.get("variable_id"), f"native.derivatives[{index}].variable_id")
        )
        native_values.append(
            _finite(row.get("accepted_real"), f"native.derivatives[{index}].accepted_real")
        )
    if len(variable_order) != len(set(variable_order)):
        raise ValueError("native gradient variable order contains duplicates")
    fd_rows = finite_difference.get("derivatives")
    if not isinstance(fd_rows, list) or len(fd_rows) != len(variable_order):
        raise ValueError("finite-difference receipt must cover every native variable")
    component_errors: list[float] = []
    component_signs: list[bool] = []
    step_match = True
    for index, (row, variable_id) in enumerate(zip(fd_rows, variable_order, strict=True)):
        if not isinstance(row, dict) or row.get("variable_id") != variable_id:
            raise ValueError("finite-difference variable order differs from native gradient")
        steps = row.get("steps")
        selected = row.get("selected")
        if not isinstance(steps, list) or not isinstance(selected, dict) or selected not in steps:
            raise ValueError(f"finite-difference derivative row {index} is invalid")
        relative_steps: list[float] = []
        for step in steps:
            if not isinstance(step, dict):
                raise ValueError("finite-difference step is invalid")
            relative_steps.append(
                _finite(step.get("relative_step"), "relative_step", positive=True)
            )
        step_match = step_match and relative_steps == normalized_policy["required_relative_steps"]
        error = _finite(selected.get("relative_error"), "selected.relative_error")
        if error < 0.0:
            raise ValueError("finite-difference relative error must be nonnegative")
        sign = selected.get("sign_agreement")
        if not isinstance(sign, bool):
            raise ValueError("finite-difference sign evidence is invalid")
        component_errors.append(error)
        component_signs.append(sign)
    cosine = _finite(finite_difference.get("cosine_similarity"), "cosine_similarity")
    if not -1.0 <= cosine <= 1.0:
        raise ValueError("finite-difference cosine similarity is outside [-1, 1]")
    if directional.get("variables") != variable_order:
        raise ValueError("directional receipt variable order differs from native gradient")
    directional_error = _finite(directional.get("relative_error"), "directional.relative_error")
    if directional_error < 0.0:
        raise ValueError("directional relative error must be nonnegative")
    directional_sign = directional.get("sign_agreement")
    if not isinstance(directional_sign, bool):
        raise ValueError("directional sign evidence is invalid")
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
    gradient_fingerprint = domain_sha256_v2(
        "comsol_mcp.licensed_robust_gradient",
        {
            "source_revision": source_identity[0],
            "source_sha256": source_identity[1],
            "variable_order": variable_order,
            "native_gradient": native_values,
            "native_receipt_sha256": native_sha,
        },
    )
    component_check_fingerprint = domain_sha256_v2(
        "comsol_mcp.licensed_component_gradient_check",
        {
            "gradient_fingerprint": gradient_fingerprint,
            "receipt_sha256": finite_difference_sha,
            "relative_errors": component_errors,
            "cosine_similarity": cosine,
        },
    )
    directional_check_fingerprint = domain_sha256_v2(
        "comsol_mcp.licensed_directional_gradient_check",
        {
            "gradient_fingerprint": gradient_fingerprint,
            "receipt_sha256": directional_sha,
            "relative_error": directional_error,
            "sign_agreement": directional_sign,
        },
    )
    body = {
        "schema_name": ROBUST_GRADIENT_RECEIPT_SCHEMA_NAME,
        "schema_version": ROBUST_GRADIENT_RECEIPT_SCHEMA_VERSION,
        "policy_fingerprint": normalized_policy["policy_fingerprint"],
        "gradient_fingerprint": gradient_fingerprint,
        "component_check_fingerprint": component_check_fingerprint,
        "directional_check_fingerprint": directional_check_fingerprint,
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
    "assess_licensed_gradient_ladder",
    "assess_robust_gradient_acceptance",
    "normalize_robust_gradient_policy",
]
