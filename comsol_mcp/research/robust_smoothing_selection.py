"""Evidence-only comparison of caller-supplied robust soft-min temperatures."""

from __future__ import annotations

import math
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .derivative_support import _bounded_json, _finite, _object, _sha256
from .robust_objectives import (
    aggregate_robust_absolute_contrast_gradient,
    evaluate_robust_absolute_contrast,
    normalize_robust_objective_configuration,
)

ROBUST_SMOOTHING_COMPARISON_SCHEMA_NAME = "comsol_mcp.robust_smoothing_comparison_receipt"
ROBUST_SMOOTHING_COMPARISON_SCHEMA_VERSION = "1.0.0"


def _temperatures(value: object) -> list[float]:
    if not isinstance(value, list) or not 2 <= len(value) <= 16:
        raise ValueError("smoothing candidates must contain between two and sixteen values")
    normalized = sorted(
        _finite(item, f"candidate_temperatures[{index}]", positive=True)
        for index, item in enumerate(value)
    )
    if len(normalized) != len(set(normalized)):
        raise ValueError("smoothing candidates must be unique")
    return normalized


def _norm(values: list[float]) -> float:
    return math.sqrt(sum(item * item for item in values))


def compare_robust_smoothing_candidates(
    base_configuration: object,
    condition_table: object,
    observations: object,
    condition_gradients: object,
    candidate_temperatures: object,
) -> dict[str, Any]:
    """Compare objective and gradient effects without selecting a winner."""
    base = normalize_robust_objective_configuration(base_configuration)
    candidates = _temperatures(candidate_temperatures)
    basis = {
        key: value
        for key, value in base.items()
        if key not in {"worst_case_temperature", "objective_fingerprint"}
    }
    rows = []
    prior_gradient: list[float] | None = None
    for temperature in candidates:
        configuration = {
            key: value
            for key, value in base.items()
            if key not in {"worst_case_temperature", "objective_fingerprint"}
        }
        configuration["worst_case_temperature"] = temperature
        objective = evaluate_robust_absolute_contrast(configuration, condition_table, observations)
        gradient = aggregate_robust_absolute_contrast_gradient(
            configuration,
            condition_table,
            observations,
            condition_gradients,
        )
        weights = [float(pair["smooth_worst_case_weight"]) for pair in objective["pairs"]]
        aggregate_gradient = [float(item) for item in gradient["aggregate_gradient"]]
        entropy = -sum(weight * math.log(weight) for weight in weights if weight > 0.0)
        effective_pairs = 1.0 / sum(weight * weight for weight in weights)
        gradient_delta = (
            None
            if prior_gradient is None
            else _norm(
                [
                    current - previous
                    for current, previous in zip(aggregate_gradient, prior_gradient, strict=True)
                ]
            )
        )
        rows.append(
            {
                "worst_case_temperature": temperature,
                "objective_fingerprint": objective["objective_fingerprint"],
                "objective_receipt_fingerprint": objective["receipt_fingerprint"],
                "gradient_receipt_fingerprint": gradient["receipt_fingerprint"],
                "minimum_smooth_absolute_contrast": objective["minimum_smooth_absolute_contrast"],
                "aggregate_objective": objective["smooth_worst_case_absolute_contrast"],
                "approximation_offset": objective["worst_case_approximation_offset"],
                "maximum_pair_weight": max(weights),
                "effective_pair_count": effective_pairs,
                "weight_entropy": entropy,
                "aggregate_gradient": aggregate_gradient,
                "aggregate_gradient_norm": _norm(aggregate_gradient),
                "gradient_delta_norm_from_previous_candidate": gradient_delta,
            }
        )
        prior_gradient = aggregate_gradient
    body = {
        "schema_name": ROBUST_SMOOTHING_COMPARISON_SCHEMA_NAME,
        "schema_version": ROBUST_SMOOTHING_COMPARISON_SCHEMA_VERSION,
        "comparison_basis_fingerprint": domain_sha256_v2(
            "comsol_mcp.robust_smoothing_comparison_basis", basis
        ),
        "candidate_temperatures": candidates,
        "candidate_count": len(candidates),
        "rows": rows,
        "selection_disposition": "review_required",
        "selected_temperature": None,
        "automatic_selection_used": False,
    }
    body["receipt_fingerprint"] = domain_sha256_v2(ROBUST_SMOOTHING_COMPARISON_SCHEMA_NAME, body)
    return body


def normalize_robust_smoothing_comparison_receipt(value: object) -> dict[str, Any]:
    """Validate one exact comparison receipt and its no-auto-selection boundary."""
    bounded = _bounded_json(value, "robust smoothing comparison receipt", 2 * 1024 * 1024)
    supplied = None
    if isinstance(bounded, dict) and "receipt_fingerprint" in bounded:
        supplied = bounded.pop("receipt_fingerprint")
    raw = _object(
        bounded,
        {
            "schema_name",
            "schema_version",
            "comparison_basis_fingerprint",
            "candidate_temperatures",
            "candidate_count",
            "rows",
            "selection_disposition",
            "selected_temperature",
            "automatic_selection_used",
        },
        "robust smoothing comparison receipt",
    )
    if (
        raw["schema_name"] != ROBUST_SMOOTHING_COMPARISON_SCHEMA_NAME
        or raw["schema_version"] != ROBUST_SMOOTHING_COMPARISON_SCHEMA_VERSION
    ):
        raise ValueError("robust smoothing comparison schema identity is unsupported")
    candidates = _temperatures(raw["candidate_temperatures"])
    if raw["candidate_count"] != len(candidates):
        raise ValueError("robust smoothing comparison candidate count is invalid")
    if (
        raw["selection_disposition"] != "review_required"
        or raw["selected_temperature"] is not None
        or raw["automatic_selection_used"] is not False
    ):
        raise ValueError("robust smoothing comparison cannot select automatically")
    rows = raw["rows"]
    if not isinstance(rows, list) or len(rows) != len(candidates):
        raise ValueError("robust smoothing comparison rows are incomplete")
    normalized_rows = []
    for index, (item, temperature) in enumerate(zip(rows, candidates, strict=True)):
        row = _object(
            item,
            {
                "worst_case_temperature",
                "objective_fingerprint",
                "objective_receipt_fingerprint",
                "gradient_receipt_fingerprint",
                "minimum_smooth_absolute_contrast",
                "aggregate_objective",
                "approximation_offset",
                "maximum_pair_weight",
                "effective_pair_count",
                "weight_entropy",
                "aggregate_gradient",
                "aggregate_gradient_norm",
                "gradient_delta_norm_from_previous_candidate",
            },
            f"robust smoothing comparison rows[{index}]",
        )
        if row["worst_case_temperature"] != temperature:
            raise ValueError("robust smoothing comparison row order changed")
        gradient = row["aggregate_gradient"]
        if not isinstance(gradient, list) or not gradient:
            raise ValueError("robust smoothing comparison gradient must be nonempty")
        normalized_gradient = [
            _finite(item, f"rows[{index}].aggregate_gradient") for item in gradient
        ]
        delta = row["gradient_delta_norm_from_previous_candidate"]
        normalized_delta = (
            None
            if delta is None
            else _finite(delta, f"rows[{index}].gradient_delta_norm")
        )
        if normalized_delta is not None and normalized_delta < 0.0:
            raise ValueError("robust smoothing comparison gradient delta must be nonnegative")
        if (index == 0) != (normalized_delta is None):
            raise ValueError("robust smoothing comparison gradient delta ordering is invalid")
        normalized_rows.append(
            {
                "worst_case_temperature": temperature,
                "objective_fingerprint": _sha256(
                    row["objective_fingerprint"], "objective_fingerprint"
                ),
                "objective_receipt_fingerprint": _sha256(
                    row["objective_receipt_fingerprint"], "objective_receipt_fingerprint"
                ),
                "gradient_receipt_fingerprint": _sha256(
                    row["gradient_receipt_fingerprint"], "gradient_receipt_fingerprint"
                ),
                "minimum_smooth_absolute_contrast": _finite(
                    row["minimum_smooth_absolute_contrast"], "minimum contrast"
                ),
                "aggregate_objective": _finite(row["aggregate_objective"], "aggregate objective"),
                "approximation_offset": _finite(
                    row["approximation_offset"], "approximation offset"
                ),
                "maximum_pair_weight": _finite(
                    row["maximum_pair_weight"], "maximum pair weight", positive=True
                ),
                "effective_pair_count": _finite(
                    row["effective_pair_count"], "effective pair count", positive=True
                ),
                "weight_entropy": _finite(row["weight_entropy"], "weight entropy"),
                "aggregate_gradient": normalized_gradient,
                "aggregate_gradient_norm": _finite(
                    row["aggregate_gradient_norm"], "aggregate gradient norm"
                ),
                "gradient_delta_norm_from_previous_candidate": normalized_delta,
            }
        )
    body = {
        "schema_name": ROBUST_SMOOTHING_COMPARISON_SCHEMA_NAME,
        "schema_version": ROBUST_SMOOTHING_COMPARISON_SCHEMA_VERSION,
        "comparison_basis_fingerprint": _sha256(
            raw["comparison_basis_fingerprint"], "comparison_basis_fingerprint"
        ),
        "candidate_temperatures": candidates,
        "candidate_count": len(candidates),
        "rows": normalized_rows,
        "selection_disposition": "review_required",
        "selected_temperature": None,
        "automatic_selection_used": False,
    }
    expected = domain_sha256_v2(ROBUST_SMOOTHING_COMPARISON_SCHEMA_NAME, body)
    if supplied != expected:
        raise ValueError("robust smoothing comparison receipt fingerprint is invalid")
    return {**body, "receipt_fingerprint": expected}


__all__ = [
    "ROBUST_SMOOTHING_COMPARISON_SCHEMA_NAME",
    "ROBUST_SMOOTHING_COMPARISON_SCHEMA_VERSION",
    "compare_robust_smoothing_candidates",
    "normalize_robust_smoothing_comparison_receipt",
]
