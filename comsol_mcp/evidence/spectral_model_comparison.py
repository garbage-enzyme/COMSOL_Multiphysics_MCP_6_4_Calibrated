"""Bounded solver-free comparison of scalar spectral line-shape models."""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any, Mapping

import numpy as np

from comsol_mcp.durable import canonical_sha256_v1

from .spectral_characterization import (
    _fit_candidate,
    _normalize_measurement_configuration,
    validate_spectral_analysis_decision,
    validate_spectral_point_bundle,
)

SPECTRAL_MODEL_COMPARISON_SCHEMA = "comsol_mcp.spectral_model_comparison"
SPECTRAL_MODEL_COMPARISON_VERSION = "1.0.0"
_COORDINATES = {"wavelength", "frequency", "angular_frequency", "energy"}
_MODELS = {"local_polynomial_fit", "lorentzian_fit", "fano_fit"}
_C_EXACT_M_PER_S: float = 299_792_458.0
_H_EXACT_J_S: float = 6.626_070_15e-34


def _coordinate(wavelength_m: float, coordinate: str) -> float:
    if coordinate == "wavelength":
        return wavelength_m
    if coordinate == "frequency":
        return _C_EXACT_M_PER_S / wavelength_m
    if coordinate == "angular_frequency":
        return 2.0 * math.pi * _C_EXACT_M_PER_S / wavelength_m
    if coordinate == "energy":
        return _H_EXACT_J_S * _C_EXACT_M_PER_S / wavelength_m
    raise ValueError("unsupported fitting coordinate")


def _wavelength(coordinate_value: float, coordinate: str) -> float:
    if coordinate == "wavelength":
        return coordinate_value
    if coordinate == "frequency":
        return _C_EXACT_M_PER_S / coordinate_value
    if coordinate == "angular_frequency":
        return 2.0 * math.pi * _C_EXACT_M_PER_S / coordinate_value
    if coordinate == "energy":
        return _H_EXACT_J_S * _C_EXACT_M_PER_S / coordinate_value
    raise ValueError("unsupported fitting coordinate")


def _normalize_configuration(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("comparison_configuration must be an object")
    item = dict(value)
    expected = {
        "coordinate",
        "models",
        "baseline_rule",
        "baseline_response_value",
        "fit_support_points",
        "fit_support_sensitivity_points",
        "local_polynomial_degree",
        "fit_max_evaluations",
        "fit_quality_policy",
    }
    if set(item) != expected:
        raise ValueError("comparison_configuration fields are invalid")
    coordinate = item["coordinate"]
    if coordinate not in _COORDINATES:
        raise ValueError("comparison coordinate is unsupported")
    models = item["models"]
    if not isinstance(models, list) or not 2 <= len(models) <= len(_MODELS):
        raise ValueError("comparison models must contain 2..3 entries")
    if len(models) != len(set(models)) or any(model not in _MODELS for model in models):
        raise ValueError("comparison models must be unique supported fit methods")
    normalized_models = sorted(models)
    degree = item["local_polynomial_degree"]
    if "local_polynomial_fit" not in normalized_models and degree is not None:
        raise ValueError(
            "local_polynomial_degree is only valid when local_polynomial_fit is compared"
        )
    normalized = {
        "coordinate": coordinate,
        "models": normalized_models,
        "baseline_rule": item["baseline_rule"],
        "baseline_response_value": item["baseline_response_value"],
        "fit_support_points": item["fit_support_points"],
        "fit_support_sensitivity_points": item["fit_support_sensitivity_points"],
        "local_polynomial_degree": degree,
        "fit_max_evaluations": item["fit_max_evaluations"],
        "fit_quality_policy": item["fit_quality_policy"],
    }
    for model in normalized_models:
        _normalize_measurement_configuration(
            {
                "peak_method": model,
                "baseline_rule": normalized["baseline_rule"],
                "baseline_response_value": normalized["baseline_response_value"],
                "fwhm_definition": "half_prominence",
                "fit_support_points": normalized["fit_support_points"],
                "fit_support_sensitivity_points": normalized["fit_support_sensitivity_points"],
                "local_polynomial_degree": (
                    normalized["local_polynomial_degree"]
                    if model == "local_polynomial_fit"
                    else None
                ),
                "fit_max_evaluations": normalized["fit_max_evaluations"],
                "fit_quality_policy": normalized["fit_quality_policy"],
            }
        )
    return normalized


def _information_criteria(*, rss: float, observations: int, parameters: int) -> dict[str, Any]:
    if not math.isfinite(rss) or rss < 0.0:
        raise ValueError("fit RSS must be finite and nonnegative")
    stabilized = max(rss, float(np.finfo(float).tiny))
    aic = observations * math.log(stabilized / observations) + 2.0 * parameters
    bic = observations * math.log(stabilized / observations) + parameters * math.log(observations)
    aicc = (
        None
        if observations <= parameters + 1
        else aic + (2.0 * parameters * (parameters + 1)) / (observations - parameters - 1)
    )
    return {"aic": aic, "aicc": aicc, "bic": bic}


def build_spectral_model_comparison(
    bundle: Mapping[str, Any],
    decision: Mapping[str, Any],
    comparison_configuration: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare declared scalar line-shape models on one identical support."""
    normalized_bundle = validate_spectral_point_bundle(bundle)
    normalized_decision = validate_spectral_analysis_decision(decision, bundle=normalized_bundle)
    configuration = _normalize_configuration(comparison_configuration)
    rows = normalized_bundle["rows"]
    response_name = normalized_decision["analysis_policy"]["response_quantity"]
    polarity = normalized_decision["analysis_policy"]["candidate_polarity"]
    evidence_rows = [
        {"row_id": row["row_id"], "raw_row_sha256": row["raw_row_sha256"]} for row in rows
    ]
    body: dict[str, Any] = {
        "schema_name": SPECTRAL_MODEL_COMPARISON_SCHEMA,
        "schema_version": SPECTRAL_MODEL_COMPARISON_VERSION,
        "bundle_sha256": normalized_bundle["bundle_sha256"],
        "decision_sha256": normalized_decision["decision_sha256"],
        "configuration_sha256": normalized_bundle["configuration_sha256"],
        "comparison_configuration": configuration,
        "comparison_configuration_sha256": canonical_sha256_v1(configuration),
        "state": "not_compared",
        "reason_code": f"classification_{normalized_decision['classification']}",
        "models": [],
        "ranking": None,
        "evidence_rows": evidence_rows,
        "scientific_mechanism_classified": False,
        "solver_started": False,
        "filesystem_modified": False,
    }
    if normalized_decision["classification"] != "interior_candidate":
        return {**body, "comparison_sha256": canonical_sha256_v1(body)}

    candidate_id = normalized_decision["candidate_row_ids"][0]
    original_candidate_index = next(
        index for index, row in enumerate(rows) if row["row_id"] == candidate_id
    )
    measured: list[float] = [float(row[response_name]) for row in rows]
    oriented_original = measured if polarity == "maximum" else [-value for value in measured]
    if configuration["baseline_rule"] == "local_prominence":
        baseline = max(
            min(oriented_original[: original_candidate_index + 1]),
            min(oriented_original[original_candidate_index:]),
        )
    elif configuration["baseline_rule"] == "window_endpoints_mean":
        baseline = (oriented_original[0] + oriented_original[-1]) / 2.0
    else:
        declared = configuration["baseline_response_value"]
        if declared is None:
            raise RuntimeError("validated declared baseline is missing")
        baseline = declared if polarity == "maximum" else -declared

    coordinate_values = [
        _coordinate(row["requested_wavelength_m"], configuration["coordinate"]) for row in rows
    ]
    order = sorted(range(len(rows)), key=lambda index: coordinate_values[index])
    ordered_coordinates = [coordinate_values[index] for index in order]
    ordered_oriented = [oriented_original[index] for index in order]
    candidate_index = order.index(original_candidate_index)
    model_results = []
    for model in configuration["models"]:
        try:
            fitted = _fit_candidate(
                method=model,
                wavelengths=ordered_coordinates,
                oriented=ordered_oriented,
                candidate_index=candidate_index,
                support_count=configuration["fit_support_points"],
                baseline=baseline,
                polynomial_degree=(
                    configuration["local_polynomial_degree"]
                    if model == "local_polynomial_fit"
                    else None
                ),
                max_evaluations=configuration["fit_max_evaluations"],
                fit_quality_policy=configuration["fit_quality_policy"],
            )
            peak_coordinate = fitted["peak_wavelength_m"]
            peak_wavelength = _wavelength(peak_coordinate, configuration["coordinate"])
            crossing_coordinates = [fitted["left_crossing_m"], fitted["right_crossing_m"]]
            crossing_wavelengths = sorted(
                _wavelength(value, configuration["coordinate"])
                for value in crossing_coordinates
                if value is not None
            )
            wavelength_width = (
                None
                if len(crossing_wavelengths) != 2
                else crossing_wavelengths[1] - crossing_wavelengths[0]
            )
            diagnostics = deepcopy(fitted["diagnostics"])
            diagnostics["fit_window_coordinate_si"] = diagnostics.pop("fit_window_m")
            diagnostics["coordinate_origin_si"] = diagnostics.pop("coordinate_origin_m")
            diagnostics["coordinate_scale_si"] = diagnostics.pop("coordinate_scale_m")
            parameter_count = len(diagnostics["parameter_values"])
            observation_count = diagnostics["support_point_count"]
            criteria = _information_criteria(
                rss=diagnostics["residual_sum_squares"],
                observations=observation_count,
                parameters=parameter_count,
            )
            sensitivity = []
            for support_count in configuration["fit_support_sensitivity_points"]:
                try:
                    measured_fit = _fit_candidate(
                        method=model,
                        wavelengths=ordered_coordinates,
                        oriented=ordered_oriented,
                        candidate_index=candidate_index,
                        support_count=support_count,
                        baseline=baseline,
                        polynomial_degree=(
                            configuration["local_polynomial_degree"]
                            if model == "local_polynomial_fit"
                            else None
                        ),
                        max_evaluations=configuration["fit_max_evaluations"],
                        fit_quality_policy=configuration["fit_quality_policy"],
                    )
                    sensitivity_peak = _wavelength(
                        measured_fit["peak_wavelength_m"], configuration["coordinate"]
                    )
                    sensitivity_crossings = sorted(
                        _wavelength(value, configuration["coordinate"])
                        for value in (
                            measured_fit["left_crossing_m"],
                            measured_fit["right_crossing_m"],
                        )
                        if value is not None
                    )
                    sensitivity_width = (
                        None
                        if len(sensitivity_crossings) != 2
                        else sensitivity_crossings[1] - sensitivity_crossings[0]
                    )
                    sensitivity.append(
                        {
                            "support_point_count": support_count,
                            "state": "measured",
                            "peak_wavelength_m": sensitivity_peak,
                            "wavelength_fwhm_m": sensitivity_width,
                        }
                    )
                except (RuntimeError, TypeError, ValueError, np.linalg.LinAlgError) as exc:
                    sensitivity.append(
                        {
                            "support_point_count": support_count,
                            "state": "fit_failed",
                            "failure_reason": str(exc)[:2048],
                        }
                    )
            model_results.append(
                {
                    "model": model,
                    "state": "measured",
                    "reason_code": "fit_passed_declared_quality_policy",
                    "peak_coordinate_si": peak_coordinate,
                    "peak_wavelength_m": peak_wavelength,
                    "coordinate_fwhm_si": fitted["fwhm_m"],
                    "wavelength_fwhm_m": wavelength_width,
                    "coordinate_quality_factor": fitted["quality_factor"],
                    "wavelength_quality_factor": (
                        None
                        if wavelength_width is None or wavelength_width <= 0.0
                        else peak_wavelength / wavelength_width
                    ),
                    "support_rows": [
                        evidence_rows[order[index]] for index in fitted["support_indices"]
                    ],
                    "diagnostics": diagnostics,
                    "fit_support_sensitivity": sensitivity,
                    "information_criteria": criteria,
                }
            )
        except (RuntimeError, TypeError, ValueError, np.linalg.LinAlgError) as exc:
            model_results.append(
                {
                    "model": model,
                    "state": "fit_failed",
                    "reason_code": "fit_failed_declared_quality_policy",
                    "failure_reason": str(exc)[:2048],
                }
            )

    successful = [item for item in model_results if item["state"] == "measured"]
    criterion = (
        "aicc"
        if all(item["information_criteria"]["aicc"] is not None for item in successful)
        else "bic"
    )
    if successful:
        best = min(item["information_criteria"][criterion] for item in successful)
        exponentials = [
            math.exp(-0.5 * (item["information_criteria"][criterion] - best)) for item in successful
        ]
        total = sum(exponentials)
        ranked = []
        for item, weight in zip(successful, exponentials):
            value = item["information_criteria"][criterion]
            ranked.append(
                {
                    "model": item["model"],
                    "criterion": criterion,
                    "value": value,
                    "delta": value - best,
                    "descriptive_weight": weight / total,
                }
            )
        ranked.sort(key=lambda item: (item["value"], item["model"]))
        ranking = {
            "state": "descriptive_model_support",
            "criterion": criterion,
            "entries": ranked,
            "mechanism_authority": False,
        }
        state = "compared"
        reason_code = (
            "models_compared_with_aicc"
            if criterion == "aicc"
            else "models_compared_with_bic_aicc_undefined"
        )
    else:
        ranking = {
            "state": "all_models_failed",
            "criterion": None,
            "entries": [],
            "mechanism_authority": False,
        }
        state = "not_compared"
        reason_code = "all_models_failed"
    body.update(
        {
            "state": state,
            "reason_code": reason_code,
            "models": model_results,
            "ranking": ranking,
        }
    )
    return {**body, "comparison_sha256": canonical_sha256_v1(body)}


def validate_spectral_model_comparison(
    value: Mapping[str, Any],
    *,
    bundle: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> dict[str, Any]:
    """Rebuild one comparison and reject noncanonical or tampered output."""
    if not isinstance(value, Mapping):
        raise ValueError("spectral model comparison must be an object")
    item = dict(value)
    rebuilt = build_spectral_model_comparison(
        bundle, decision, item.get("comparison_configuration", {})
    )
    if item != rebuilt:
        raise ValueError("spectral model comparison is noncanonical or its hash does not match")
    return deepcopy(rebuilt)


__all__ = [
    "SPECTRAL_MODEL_COMPARISON_SCHEMA",
    "SPECTRAL_MODEL_COMPARISON_VERSION",
    "build_spectral_model_comparison",
    "validate_spectral_model_comparison",
]
