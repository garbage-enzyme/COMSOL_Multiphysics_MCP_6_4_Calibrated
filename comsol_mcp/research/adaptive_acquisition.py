"""Explicitly loaded bounded Gaussian-process expected-improvement acquisition."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from importlib import import_module
from typing import Any

from .contracts import normalize_design_space

MAX_GP_OBSERVATIONS = 256
MAX_GP_CANDIDATES = 4096


def _positive_finite(value: object, name: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or number < 0.0 or (number == 0.0 and not allow_zero):
        kind = "nonnegative" if allow_zero else "positive"
        raise ValueError(f"{name} must be {kind} and finite")
    return number


def _continuous_axes(design_space: object) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    space = normalize_design_space(design_space)
    variables = list(space["variables"])
    if not variables or any(item["kind"] != "continuous" for item in variables):
        raise ValueError(
            "Gaussian-process acquisition currently requires a continuous design space"
        )
    return space, variables


def _point(
    value: object,
    variables: Sequence[Mapping[str, Any]],
    name: str,
) -> tuple[dict[str, float], tuple[float, ...]]:
    expected = {item["variable_id"] for item in variables}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"{name} must exactly cover the continuous design space")
    normalized: dict[str, float] = {}
    scaled: list[float] = []
    for variable in variables:
        variable_id = variable["variable_id"]
        raw = value[variable_id]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"{name}.{variable_id} must be numeric")
        number = float(raw)
        lower = float(variable["lower"])
        upper = float(variable["upper"])
        if not math.isfinite(number) or not lower <= number <= upper:
            raise ValueError(f"{name}.{variable_id} is outside the frozen design space")
        normalized[variable_id] = number
        scaled.append((number - lower) / (upper - lower))
    return {key: normalized[key] for key in sorted(normalized)}, tuple(scaled)


def select_expected_improvement_candidate(
    design_space: object,
    observations: object,
    candidates: object,
    *,
    length_scale: float = 0.25,
    noise: float = 1.0e-10,
    exploration: float = 0.0,
) -> dict[str, Any]:
    """Select one bounded candidate by deterministic GP expected improvement."""
    space, variables = _continuous_axes(design_space)
    length_scale = _positive_finite(length_scale, "length_scale")
    noise = _positive_finite(noise, "noise")
    exploration = _positive_finite(exploration, "exploration", allow_zero=True)
    if not isinstance(observations, Sequence) or isinstance(observations, (str, bytes)):
        raise ValueError("observations must be a sequence")
    if not 2 <= len(observations) <= MAX_GP_OBSERVATIONS:
        raise ValueError(f"observations must contain 2 through {MAX_GP_OBSERVATIONS} points")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise ValueError("candidates must be a sequence")
    if not 1 <= len(candidates) <= MAX_GP_CANDIDATES:
        raise ValueError(f"candidates must contain 1 through {MAX_GP_CANDIDATES} points")

    observed_scaled: list[tuple[float, ...]] = []
    losses: list[float] = []
    for index, item in enumerate(observations):
        if not isinstance(item, Mapping) or set(item) != {"values", "loss"}:
            raise ValueError("each observation must contain exactly values and loss")
        _, scaled = _point(item["values"], variables, f"observations[{index}].values")
        loss = _positive_finite(item["loss"], f"observations[{index}].loss", allow_zero=True)
        observed_scaled.append(scaled)
        losses.append(loss)
    if len(set(observed_scaled)) != len(observed_scaled):
        raise ValueError("observations contain duplicate design points")

    candidate_points: list[dict[str, float]] = []
    candidate_scaled: list[tuple[float, ...]] = []
    for index, item in enumerate(candidates):
        point, scaled = _point(item, variables, f"candidates[{index}]")
        candidate_points.append(point)
        candidate_scaled.append(scaled)
    if len(set(candidate_scaled)) != len(candidate_scaled):
        raise ValueError("candidates contain duplicate design points")
    if set(observed_scaled) & set(candidate_scaled):
        raise ValueError("candidates must exclude observed design points")

    # Keep heavy numerical imports behind the explicit adaptive path.
    np = import_module("numpy")
    scipy_linalg = import_module("scipy.linalg")
    scipy_special = import_module("scipy.special")

    x_train: Any = np.asarray(observed_scaled, dtype=np.float64)
    y_train: Any = np.asarray(losses, dtype=np.float64)
    x_candidates: Any = np.asarray(candidate_scaled, dtype=np.float64)
    y_mean = float(np.mean(y_train))
    y_scale = float(np.std(y_train))
    if not math.isfinite(y_scale) or y_scale <= 1.0e-15:
        y_scale = 1.0
    targets = (y_train - y_mean) / y_scale

    def kernel(left: Any, right: Any) -> Any:
        squared = np.sum((left[:, None, :] - right[None, :, :]) ** 2, axis=2)
        return np.exp(-0.5 * squared / (length_scale * length_scale))

    covariance = kernel(x_train, x_train)
    covariance.flat[:: covariance.shape[0] + 1] += noise
    try:
        factor = scipy_linalg.cho_factor(covariance, lower=True, check_finite=True)
        alpha = scipy_linalg.cho_solve(factor, targets, check_finite=True)
        cross = kernel(x_train, x_candidates)
        posterior_mean = y_mean + y_scale * (cross.T @ alpha)
        solved = scipy_linalg.cho_solve(factor, cross, check_finite=True)
    except (ValueError, ArithmeticError) as exc:
        raise ValueError("Gaussian-process covariance is not solvable") from exc
    posterior_variance = np.maximum(0.0, 1.0 - np.sum(cross * solved, axis=0))
    posterior_std = y_scale * np.sqrt(posterior_variance)
    improvement = float(np.min(y_train)) - posterior_mean - exploration
    expected_improvement = np.zeros_like(improvement)
    positive = posterior_std > 0.0
    z = np.zeros_like(improvement)
    z[positive] = improvement[positive] / posterior_std[positive]
    expected_improvement[positive] = improvement[positive] * scipy_special.ndtr(
        z[positive]
    ) + posterior_std[positive] * np.exp(-0.5 * z[positive] ** 2) / math.sqrt(2.0 * math.pi)
    expected_improvement[~positive] = np.maximum(0.0, improvement[~positive])
    if not (
        np.all(np.isfinite(posterior_mean))
        and np.all(np.isfinite(posterior_std))
        and np.all(np.isfinite(expected_improvement))
    ):
        raise ValueError("Gaussian-process acquisition produced nonfinite evidence")
    selected = int(np.flatnonzero(expected_improvement == np.max(expected_improvement))[0])
    return {
        "selected_index": selected,
        "values": candidate_points[selected],
        "posterior_mean": float(posterior_mean[selected]),
        "posterior_standard_deviation": float(posterior_std[selected]),
        "expected_improvement": float(expected_improvement[selected]),
        "observed_best_loss": float(np.min(y_train)),
        "observation_count": len(observed_scaled),
        "candidate_count": len(candidate_points),
        "space_fingerprint": space["space_fingerprint"],
        "settings": {
            "length_scale": length_scale,
            "noise": noise,
            "exploration": exploration,
        },
    }


__all__ = [
    "MAX_GP_CANDIDATES",
    "MAX_GP_OBSERVATIONS",
    "select_expected_improvement_candidate",
]
