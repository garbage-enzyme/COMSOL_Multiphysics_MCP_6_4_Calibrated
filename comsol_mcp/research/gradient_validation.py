"""Policy-explicit, solver-free comparison of native and finite-difference gradients."""

from __future__ import annotations

import math
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .derivative_support import _finite, _identifier, _object
from .gradient_contracts import normalize_gradient_record

GRADIENT_CHECK_SCHEMA_NAME = "comsol_mcp.gradient_check"
GRADIENT_CHECK_SCHEMA_VERSION = "1.0.0"


def _vector(value: object, name: str, count: int) -> list[float]:
    if not isinstance(value, list) or len(value) != count:
        raise ValueError(f"{name} must match the gradient variable count")
    return [_finite(item, f"{name}[{i}]") for i, item in enumerate(value)]


def _norm(values: list[float]) -> float:
    return math.sqrt(sum(item * item for item in values))


def _cosine(left: list[float], right: list[float]) -> float | None:
    denominator = _norm(left) * _norm(right)
    if denominator == 0.0:
        return None
    return sum(a * b for a, b in zip(left, right, strict=True)) / denominator


def _relative_error(native: float, finite_difference: float, floor: float) -> float:
    return abs(native - finite_difference) / max(abs(native), abs(finite_difference), floor)


def compare_gradient(
    native_record: object,
    support: object,
    finite_difference: object,
    policy: object,
) -> dict[str, Any]:
    """Compare a native gradient with declared finite-difference evidence.

    ``finite_difference`` contains one row per canonical variable and step,
    with central or one-sided values already obtained by the caller. This
    function never solves, changes evidence, or chooses scientific thresholds.
    """
    gradient = normalize_gradient_record(native_record, support)
    raw_policy = _object(
        policy,
        {"relative_error_limit", "absolute_error_floor", "cosine_floor", "require_sign"},
        "gradient_check_policy",
    )
    relative_limit = _finite(raw_policy["relative_error_limit"], "relative_error_limit")
    absolute_floor = _finite(
        raw_policy["absolute_error_floor"], "absolute_error_floor", positive=True
    )
    cosine_floor = _finite(raw_policy["cosine_floor"], "cosine_floor")
    if not 0.0 <= relative_limit <= 1.0 or not -1.0 <= cosine_floor <= 1.0:
        raise ValueError("gradient check limits are outside the allowed range")
    if not isinstance(raw_policy["require_sign"], bool):
        raise ValueError("require_sign must be boolean")
    variable_order = gradient["variable_order"]
    count = len(variable_order)
    if not isinstance(finite_difference, list) or len(finite_difference) != count:
        raise ValueError("finite_difference must cover every canonical variable")
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(finite_difference):
        name = f"finite_difference[{index}]"
        raw = _object(item, {"variable_id", "steps"}, name)
        variable_id = _identifier(raw["variable_id"], f"{name}.variable_id")
        if variable_id != variable_order[index]:
            raise ValueError("finite-difference variable order differs from gradient row")
        steps = raw["steps"]
        if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
            raise ValueError(f"{name}.steps must be a bounded nonempty list")
        step_rows = []
        for step_index, step_value in enumerate(steps):
            step = _object(
                step_value,
                {
                    "relative_step",
                    "mode",
                    "plus_objective",
                    "minus_objective",
                    "base_objective",
                },
                f"{name}.steps[{step_index}]",
            )
            relative_step = _finite(step["relative_step"], "relative_step", positive=True)
            mode = step["mode"]
            if mode not in {"central", "forward", "backward"}:
                raise ValueError("finite-difference mode is unsupported")
            plus_value = (
                None
                if step["plus_objective"] is None
                else _finite(step["plus_objective"], "plus_objective")
            )
            minus_value = (
                None
                if step["minus_objective"] is None
                else _finite(step["minus_objective"], "minus_objective")
            )
            base_value = (
                None
                if step["base_objective"] is None
                else _finite(step["base_objective"], "base_objective")
            )
            if mode == "central" and (plus_value is None or minus_value is None):
                raise ValueError("central finite differences require both neighboring objectives")
            if mode == "forward" and (plus_value is None or base_value is None):
                raise ValueError("forward finite differences require plus and base objectives")
            if mode == "backward" and (minus_value is None or base_value is None):
                raise ValueError("backward finite differences require minus and base objectives")
            if mode == "central":
                fd_value = (plus_value - minus_value) / (2.0 * relative_step)
            elif mode == "forward":
                fd_value = (plus_value - base_value) / relative_step
            else:
                fd_value = (base_value - minus_value) / relative_step
            step_rows.append(
                {
                    "relative_step": relative_step,
                    "mode": mode,
                    "finite_difference_value": fd_value,
                    "plus_objective": plus_value,
                    "minus_objective": minus_value,
                    "base_objective": base_value,
                    "absolute_error": abs(gradient["native_gradient"][index] - fd_value),
                    "relative_error": _relative_error(
                        gradient["native_gradient"][index], fd_value, absolute_floor
                    ),
                    "sign_agreement": (
                        gradient["native_gradient"][index] == 0.0
                        or fd_value == 0.0
                        or math.copysign(1.0, gradient["native_gradient"][index])
                        == math.copysign(1.0, fd_value)
                    ),
                }
            )
        best = min(step_rows, key=lambda row: (row["relative_error"], row["relative_step"]))
        rows.append({"variable_id": variable_id, "steps": step_rows, "selected": best})
    native = list(gradient["native_gradient"])
    selected = [row["selected"]["finite_difference_value"] for row in rows]
    cosine = _cosine(native, selected)
    checks = {
        "all_relative_errors_within_limit": all(
            row["selected"]["relative_error"] <= relative_limit for row in rows
        ),
        "cosine_above_floor": cosine is not None and cosine >= cosine_floor,
        "signs_agree": all(row["selected"]["sign_agreement"] for row in rows),
        "step_sensitivity_bounded": all(
            max(item["relative_error"] for item in row["steps"])
            - min(item["relative_error"] for item in row["steps"])
            <= relative_limit
            for row in rows
        ),
    }
    passed = all(checks.values()) and (checks["signs_agree"] or not raw_policy["require_sign"])
    body = {
        "schema_name": GRADIENT_CHECK_SCHEMA_NAME,
        "schema_version": GRADIENT_CHECK_SCHEMA_VERSION,
        "gradient_fingerprint": gradient["gradient_fingerprint"],
        "variable_order": variable_order,
        "native_gradient": native,
        "finite_difference_gradient": selected,
        "cosine_similarity": cosine,
        "checks": checks,
        "passed": passed,
        "policy": {
            "relative_error_limit": relative_limit,
            "absolute_error_floor": absolute_floor,
            "cosine_floor": cosine_floor,
            "require_sign": raw_policy["require_sign"],
        },
        "rows": rows,
    }
    body["check_fingerprint"] = domain_sha256_v2(GRADIENT_CHECK_SCHEMA_NAME, body)
    return body


def compare_directional_gradient(
    native_record: object,
    support: object,
    direction: object,
    *,
    step: object,
    plus_objective: object | None,
    minus_objective: object | None,
    policy: object,
    base_objective: object | None = None,
) -> dict[str, Any]:
    """Compare one caller-supplied directional derivative with native output."""
    gradient = normalize_gradient_record(native_record, support)
    vector = _vector(direction, "direction", len(gradient["variable_order"]))
    direction_norm = _norm(vector)
    if direction_norm == 0.0:
        raise ValueError("direction must be nonzero")
    vector = [item / direction_norm for item in vector]
    step_value = _finite(step, "step", positive=True)
    if plus_objective is not None and minus_objective is not None and base_objective is not None:
        raise ValueError("central directional evidence must not include a base objective")
    if plus_objective is not None and minus_objective is not None:
        observed = (
            _finite(plus_objective, "plus_objective") - _finite(minus_objective, "minus_objective")
        ) / (2.0 * step_value)
        mode = "central"
    elif plus_objective is not None and base_objective is not None:
        observed = (
            _finite(plus_objective, "plus_objective") - _finite(base_objective, "base_objective")
        ) / step_value
        mode = "forward"
    elif minus_objective is not None and base_objective is not None:
        observed = (
            _finite(base_objective, "base_objective") - _finite(minus_objective, "minus_objective")
        ) / step_value
        mode = "backward"
    else:
        raise ValueError("central or one-sided directional objective values are required")
    predicted = sum(a * b for a, b in zip(gradient["native_gradient"], vector, strict=True))
    raw_policy = _object(
        policy,
        {"relative_error_limit", "absolute_error_floor", "cosine_floor", "require_sign"},
        "gradient_check_policy",
    )
    relative_error = abs(predicted - observed) / max(
        abs(predicted),
        abs(observed),
        _finite(raw_policy["absolute_error_floor"], "absolute_error_floor", positive=True),
    )
    sign_agreement = (
        predicted == 0.0
        or observed == 0.0
        or math.copysign(1.0, predicted) == math.copysign(1.0, observed)
    )
    relative_limit = _finite(raw_policy["relative_error_limit"], "relative_error_limit")
    if not 0.0 <= relative_limit <= 1.0:
        raise ValueError("relative_error_limit is outside the allowed range")
    passed = relative_error <= relative_limit and (sign_agreement or not raw_policy["require_sign"])
    body = {
        "schema_name": GRADIENT_CHECK_SCHEMA_NAME,
        "schema_version": GRADIENT_CHECK_SCHEMA_VERSION,
        "gradient_fingerprint": gradient["gradient_fingerprint"],
        "mode": mode,
        "direction": vector,
        "step": step_value,
        "predicted": predicted,
        "observed": observed,
        "absolute_error": abs(predicted - observed),
        "relative_error": relative_error,
        "sign_agreement": sign_agreement,
        "passed": passed,
    }
    body["directional_check_fingerprint"] = domain_sha256_v2(
        "comsol_mcp.directional_gradient_check", body
    )
    return body


__all__ = [
    "GRADIENT_CHECK_SCHEMA_NAME",
    "GRADIENT_CHECK_SCHEMA_VERSION",
    "compare_directional_gradient",
    "compare_gradient",
]
