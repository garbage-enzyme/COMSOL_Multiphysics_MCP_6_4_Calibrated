"""Solver-free spectral line-shape comparison regression tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from copy import deepcopy

import pytest
from scipy.constants import c
from src.evidence.spectral_characterization import (
    build_spectral_analysis_decision,
    build_spectral_point_bundle,
)
from src.evidence.spectral_model_comparison import (
    build_spectral_model_comparison,
    validate_spectral_model_comparison,
)
from src.server import create_server

from development_kit.tests.mcp_test_support import decode_tool_result

CONFIGURATION_SHA256 = "a" * 64


def _bundle(wavelengths: list[float], absorption: list[float]) -> dict:
    rows = []
    for index, (wavelength, value) in enumerate(zip(wavelengths, absorption)):
        raw = {"index": index, "wavelength": wavelength, "absorption": value}
        rows.append(
            {
                "row_id": f"point-{index:03d}",
                "raw_row_sha256": hashlib.sha256(
                    json.dumps(raw, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                "configuration_sha256": CONFIGURATION_SHA256,
                "requested_wavelength_m": wavelength,
                "evaluated_wavelength_m": wavelength,
                "frequency_wavelength_m": wavelength,
                "R": 0.95 - value,
                "T": 0.05,
                "A": value,
            }
        )
    return build_spectral_point_bundle(
        bundle_id="model-comparison",
        source_model={"relative_identity": "fixtures/source.mph", "sha256": "b" * 64},
        configuration_sha256=CONFIGURATION_SHA256,
        parameter_state={"angle_deg": 0.0},
        wavelength_convention={
            "unit": "m",
            "requested_field": "requested_wavelength_m",
            "evaluated_field": "evaluated_wavelength_m",
            "frequency_derived_field": "frequency_wavelength_m",
            "frequency_relation": "c_const/frequency",
        },
        expressions={"R": "R", "T": "T", "A": "1-R-T"},
        rows=rows,
    )


def _policy() -> dict:
    return {
        "response_quantity": "A",
        "candidate_polarity": "maximum",
        "passivity_abs_tolerance": 1.0e-12,
        "closure_abs_tolerance": 1.0e-12,
        "wavelength_sync_abs_m": 1.0e-15,
        "flat_response_abs_tolerance": 1.0e-12,
        "minimum_point_count": 7,
    }


def _configuration(coordinate: str = "wavelength") -> dict:
    return {
        "coordinate": coordinate,
        "models": ["fano_fit", "lorentzian_fit"],
        "baseline_rule": "declared_response",
        "baseline_response_value": 0.1,
        "fit_support_points": 31,
        "fit_support_sensitivity_points": [21, 31],
        "local_polynomial_degree": None,
        "fit_max_evaluations": 20000,
        "fit_quality_policy": {
            "maximum_relative_rms_residual": 0.2,
            "maximum_covariance_condition": 1.0e20,
            "minimum_parameter_bound_margin_fraction": 0.0,
        },
    }


def _call_tool(name: str, arguments: dict) -> dict:
    server = create_server("spectral-model-comparison-public", profile="core")
    return decode_tool_result(asyncio.run(server.call_tool(name, arguments)))


def test_analytic_lorentzian_prefers_lorentzian_on_identical_support():
    center = 5.0e-6
    half_width = 0.08e-6
    wavelengths = [4.6e-6 + index * 0.025e-6 for index in range(33)]
    absorption = [
        0.1 + 0.8 / (1.0 + ((wavelength - center) / half_width) ** 2) for wavelength in wavelengths
    ]
    bundle = _bundle(wavelengths, absorption)
    decision = build_spectral_analysis_decision(bundle, _policy())

    result = build_spectral_model_comparison(bundle, decision, _configuration())

    assert result["state"] == "compared"
    assert result["ranking"]["entries"][0]["model"] == "lorentzian_fit"
    assert result["ranking"]["mechanism_authority"] is False
    assert result["scientific_mechanism_classified"] is False
    measured = {item["model"]: item for item in result["models"]}
    assert measured["lorentzian_fit"]["peak_wavelength_m"] == pytest.approx(center, abs=1e-12)
    assert measured["lorentzian_fit"]["wavelength_fwhm_m"] == pytest.approx(
        2.0 * half_width, rel=0.02
    )
    assert len(measured["lorentzian_fit"]["support_rows"]) == 31
    assert len(measured["fano_fit"]["support_rows"]) == 31
    assert validate_spectral_model_comparison(result, bundle=bundle, decision=decision) == result


def test_frequency_coordinate_reverses_rows_and_maps_peak_back_to_wavelength():
    center_frequency = c / 5.0e-6
    half_width = center_frequency * 0.008
    wavelengths = [4.65e-6 + index * 0.022e-6 for index in range(33)]
    absorption = [
        0.1 + 0.75 / (1.0 + ((c / wavelength - center_frequency) / half_width) ** 2)
        for wavelength in wavelengths
    ]
    bundle = _bundle(wavelengths, absorption)
    decision = build_spectral_analysis_decision(bundle, _policy())

    result = build_spectral_model_comparison(
        bundle, decision, _configuration(coordinate="frequency")
    )

    lorentzian = next(item for item in result["models"] if item["model"] == "lorentzian_fit")
    assert lorentzian["state"] == "measured"
    assert lorentzian["peak_wavelength_m"] == pytest.approx(5.0e-6, rel=2e-4)
    assert lorentzian["coordinate_fwhm_si"] == pytest.approx(2.0 * half_width, rel=0.03)
    assert lorentzian["wavelength_fwhm_m"] > 0.0
    assert (
        lorentzian["diagnostics"]["fit_window_coordinate_si"][0]
        < (lorentzian["diagnostics"]["fit_window_coordinate_si"][1])
    )


def test_aicc_and_bic_match_independent_calculation():
    center = 5.0e-6
    wavelengths = [4.6e-6 + index * 0.025e-6 for index in range(33)]
    absorption = [0.1 + 0.8 / (1.0 + ((value - center) / 0.08e-6) ** 2) for value in wavelengths]
    bundle = _bundle(wavelengths, absorption)
    decision = build_spectral_analysis_decision(bundle, _policy())
    result = build_spectral_model_comparison(bundle, decision, _configuration())
    lorentzian = next(item for item in result["models"] if item["model"] == "lorentzian_fit")
    rss = lorentzian["diagnostics"]["residual_sum_squares"]
    observations = lorentzian["diagnostics"]["support_point_count"]
    parameters = len(lorentzian["diagnostics"]["parameter_values"])
    stabilized = max(rss, math.nextafter(0.0, 1.0))
    aic = observations * math.log(stabilized / observations) + 2.0 * parameters
    aicc = aic + 2.0 * parameters * (parameters + 1) / (observations - parameters - 1)
    bic = observations * math.log(stabilized / observations) + parameters * math.log(observations)

    assert lorentzian["information_criteria"]["aic"] == pytest.approx(aic)
    assert lorentzian["information_criteria"]["aicc"] == pytest.approx(aicc)
    assert lorentzian["information_criteria"]["bic"] == pytest.approx(bic)


def test_boundary_maximum_is_not_fit_and_public_tool_is_solver_free():
    wavelengths = [4.0e-6 + index * 0.1e-6 for index in range(33)]
    absorption = [0.1 + index * 0.01 for index in range(33)]
    bundle = _bundle(wavelengths, absorption)

    result = _call_tool(
        "spectral_model_compare",
        {
            "analysis_policy": _policy(),
            "comparison_configuration": _configuration(),
            "spectral_bundle": bundle,
        },
    )

    assert result["success"] is True
    assert result["classification"] == "boundary_high"
    assert result["model_comparison"]["state"] == "not_compared"
    assert result["model_comparison"]["models"] == []
    assert result["solver_started"] is False
    assert result["filesystem_modified"] is False


def test_tampering_unknown_fields_and_duplicate_models_fail_closed():
    center = 5.0e-6
    wavelengths = [4.6e-6 + index * 0.025e-6 for index in range(33)]
    absorption = [0.1 + 0.8 / (1.0 + ((value - center) / 0.08e-6) ** 2) for value in wavelengths]
    bundle = _bundle(wavelengths, absorption)
    decision = build_spectral_analysis_decision(bundle, _policy())
    configuration = _configuration()
    configuration["models"] = ["lorentzian_fit", "lorentzian_fit"]
    with pytest.raises(ValueError, match="unique"):
        build_spectral_model_comparison(bundle, decision, configuration)

    valid = build_spectral_model_comparison(bundle, decision, _configuration())
    tampered = deepcopy(valid)
    tampered["ranking"]["mechanism_authority"] = True
    with pytest.raises(ValueError, match="noncanonical|hash"):
        validate_spectral_model_comparison(tampered, bundle=bundle, decision=decision)

    unknown = _configuration()
    unknown["automatic_mechanism"] = True
    with pytest.raises(ValueError, match="fields"):
        build_spectral_model_comparison(bundle, decision, unknown)
