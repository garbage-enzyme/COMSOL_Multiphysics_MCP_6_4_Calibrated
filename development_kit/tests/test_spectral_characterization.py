"""Solver-free spectral evidence and characterization regression tests."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
from mcp.server.fastmcp import FastMCP

from src.evidence.spectral_characterization import (
    build_spectral_analysis_decision,
    build_spectral_characterization,
    build_spectral_point_bundle,
    validate_spectral_analysis_decision,
    validate_spectral_characterization,
    validate_spectral_point_bundle,
)
from src.tools.spectral_characterization import register_spectral_characterization_tools


CONFIGURATION_SHA256 = "a" * 64


def _row(index: int, wavelength: float, absorption: float) -> dict:
    transmission = 0.05
    reflection = 1.0 - transmission - absorption
    raw = {
        "index": index,
        "wavelength": wavelength,
        "R": reflection,
        "T": transmission,
        "A": absorption,
    }
    return {
        "row_id": f"point-{index:03d}",
        "raw_row_sha256": hashlib.sha256(
            json.dumps(raw, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "configuration_sha256": CONFIGURATION_SHA256,
        "requested_wavelength_m": wavelength,
        "evaluated_wavelength_m": wavelength,
        "frequency_wavelength_m": wavelength,
        "R": reflection,
        "T": transmission,
        "A": absorption,
    }


def _bundle(values: list[float], wavelengths: list[float] | None = None):
    wavelengths = wavelengths or [4.0e-6 + index * 0.1e-6 for index in range(len(values))]
    return build_spectral_point_bundle(
        bundle_id="bounded-spectrum",
        source_model={"relative_identity": "fixtures/source.mph", "sha256": "b" * 64},
        configuration_sha256=CONFIGURATION_SHA256,
        parameter_state={"angle_deg": 0.0, "polarization": "declared"},
        wavelength_convention={
            "unit": "m",
            "requested_field": "requested_wavelength_m",
            "evaluated_field": "evaluated_wavelength_m",
            "frequency_derived_field": "frequency_wavelength_m",
            "frequency_relation": "c_const/frequency",
        },
        expressions={"R": "R_expr", "T": "T_expr", "A": "1-R_expr-T_expr"},
        rows=[_row(index, wavelength, value) for index, (wavelength, value) in enumerate(zip(wavelengths, values))],
    )


def _policy(**overrides):
    value = {
        "response_quantity": "A",
        "candidate_polarity": "maximum",
        "passivity_abs_tolerance": 1.0e-12,
        "closure_abs_tolerance": 1.0e-12,
        "wavelength_sync_abs_m": 1.0e-15,
        "flat_response_abs_tolerance": 1.0e-12,
        "minimum_point_count": 5,
    }
    value.update(overrides)
    return value


def test_bundle_is_deterministic_hash_bound_and_unknown_fields_fail_closed():
    bundle = _bundle([0.1, 0.2, 0.8, 0.2, 0.1])

    assert validate_spectral_point_bundle(bundle) == bundle
    assert bundle["parameter_state_sha256"]
    assert bundle["bundle_sha256"]
    assert [row["raw_row_sha256"] for row in bundle["rows"]]

    tampered = deepcopy(bundle)
    tampered["rows"][2]["A"] = 0.7
    with pytest.raises(ValueError, match="noncanonical|hash"):
        validate_spectral_point_bundle(tampered)

    unknown = deepcopy(bundle)
    unknown["paper_target"] = 0.99
    with pytest.raises(ValueError, match="fields"):
        validate_spectral_point_bundle(unknown)


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda rows: rows.__setitem__(1, {**rows[1], "requested_wavelength_m": rows[0]["requested_wavelength_m"]}), "sorted and unique"),
        (lambda rows: rows.__setitem__(2, {**rows[2], "A": float("nan")}), "finite"),
        (lambda rows: rows.__setitem__(2, {**rows[2], "configuration_sha256": "c" * 64}), "does not match"),
        (lambda rows: rows.__setitem__(2, {**rows[2], "raw_row_sha256": rows[1]["raw_row_sha256"]}), "raw row hashes"),
    ],
)
def test_duplicate_nonfinite_and_identity_mismatch_rows_fail_closed(mutation, match):
    bundle = _bundle([0.1, 0.2, 0.8, 0.2, 0.1])
    rows = deepcopy(bundle["rows"])
    mutation(rows)
    with pytest.raises(ValueError, match=match):
        build_spectral_point_bundle(
            bundle_id=bundle["bundle_id"],
            source_model=bundle["source_model"],
            configuration_sha256=bundle["configuration_sha256"],
            parameter_state=bundle["parameter_state"],
            wavelength_convention=bundle["wavelength_convention"],
            expressions=bundle["expressions"],
            rows=rows,
        )


@pytest.mark.parametrize(
    "values,policy_changes,expected",
    [
        ([0.8, 0.4, 0.2, 0.1, 0.05], {}, "boundary_high"),
        ([0.05, 0.1, 0.2, 0.4, 0.8], {}, "boundary_high"),
        ([0.1, 0.8, 0.1, 0.7, 0.1], {}, "multi_candidate"),
        ([0.2, 0.2, 0.2, 0.2, 0.2], {}, "flat"),
        ([0.1, 0.8, 0.1], {}, "under_sampled"),
        ([0.1, 0.2, 0.8, 0.2, 0.1], {}, "interior_candidate"),
        ([0.1, 0.2, 0.3, 0.4, 0.4], {}, "no_candidate"),
        ([0.9, 0.8, 0.2, 0.8, 0.9], {"candidate_polarity": "minimum"}, "interior_candidate"),
    ],
)
def test_candidate_classification_is_explicit(values, policy_changes, expected):
    bundle = _bundle(values)
    decision = build_spectral_analysis_decision(bundle, _policy(**policy_changes))

    assert decision["classification"] == expected
    assert decision["bundle_sha256"] == bundle["bundle_sha256"]
    assert len(decision["evidence_rows"]) == len(values)
    assert validate_spectral_analysis_decision(decision, bundle=bundle) == decision


def test_passivity_closure_and_wavelength_sync_fail_under_caller_policy():
    bundle = _bundle([0.1, 0.2, 0.8, 0.2, 0.1])
    rows = deepcopy(bundle["rows"])
    rows[1]["A"] = 1.1
    rows[1]["R"] = -0.15
    rows[2]["R"] += 0.01
    rows[3]["evaluated_wavelength_m"] += 2.0e-12
    invalid = build_spectral_point_bundle(
        bundle_id=bundle["bundle_id"],
        source_model=bundle["source_model"],
        configuration_sha256=bundle["configuration_sha256"],
        parameter_state=bundle["parameter_state"],
        wavelength_convention=bundle["wavelength_convention"],
        expressions=bundle["expressions"],
        rows=rows,
    )

    decision = build_spectral_analysis_decision(invalid, _policy())

    assert decision["classification"] == "invalid_evidence"
    assert decision["invalid_row_ids"] == ["point-001", "point-002", "point-003"]
    assert decision["row_checks"][1]["passivity_passed"] is False
    assert decision["row_checks"][2]["closure_passed"] is False
    assert decision["row_checks"][3]["wavelength_sync_passed"] is False


def test_irregular_grid_is_valid_and_policy_tolerances_change_only_the_decision():
    wavelengths = [4.0e-6, 4.03e-6, 4.11e-6, 4.28e-6, 4.5e-6]
    bundle = _bundle([0.1, 0.3, 0.9, 0.4, 0.1], wavelengths)
    strict = build_spectral_analysis_decision(bundle, _policy())
    flat = build_spectral_analysis_decision(
        bundle, _policy(flat_response_abs_tolerance=1.0)
    )

    assert strict["classification"] == "interior_candidate"
    assert flat["classification"] == "flat"
    assert strict["bundle_sha256"] == flat["bundle_sha256"]
    assert strict["analysis_policy_sha256"] != flat["analysis_policy_sha256"]


def _measurement(**overrides):
    value = {
        "peak_method": "measured_grid",
        "baseline_rule": "local_prominence",
        "baseline_response_value": None,
        "fwhm_definition": "half_prominence",
        "fit_support_points": None,
        "fit_support_sensitivity_points": [],
        "local_polynomial_degree": None,
        "fit_max_evaluations": None,
    }
    value.update(overrides)
    return value


def _characterize(values, *, measurement=None, wavelengths=None):
    bundle = _bundle(values, wavelengths)
    decision = build_spectral_analysis_decision(bundle, _policy())
    result = build_spectral_characterization(
        bundle, decision, measurement or _measurement()
    )
    return bundle, decision, result


def test_measured_grid_peak_half_prominence_and_quality_factor_are_hash_bound():
    wavelengths = [4.8e-6, 4.9e-6, 5.0e-6, 5.1e-6, 5.2e-6]
    bundle, decision, result = _characterize(
        [0.1, 0.5, 0.9, 0.5, 0.1], wavelengths=wavelengths
    )

    candidate = result["candidate"]
    assert result["measurement_state"] == "measured"
    assert candidate["peak"]["wavelength_m"] == 5.0e-6
    assert candidate["peak"]["response_value"] == 0.9
    assert candidate["fwhm"]["state"] == "bracketed"
    assert candidate["fwhm"]["value_m"] == pytest.approx(0.2e-6)
    assert candidate["quality_factor"]["value"] == pytest.approx(25.0)
    assert result["evidence_binding"]["rows"] == [
        {"row_id": row["row_id"], "raw_row_sha256": row["raw_row_sha256"]}
        for row in bundle["rows"]
    ]
    assert validate_spectral_characterization(
        result, bundle=bundle, decision=decision
    ) == result


def test_quadratic_interpolation_recovers_an_off_grid_peak_on_irregular_points():
    center = 5.035e-6
    wavelengths = [4.8e-6, 4.95e-6, 5.0e-6, 5.12e-6, 5.3e-6]
    values = [0.9 - ((wavelength - center) / 0.4e-6) ** 2 for wavelength in wavelengths]
    _bundle_value, _decision, result = _characterize(
        values,
        wavelengths=wavelengths,
        measurement=_measurement(peak_method="quadratic_interpolation"),
    )

    peak = result["candidate"]["peak"]
    assert peak["wavelength_m"] == pytest.approx(center, abs=1.0e-15)
    assert peak["response_value"] == pytest.approx(0.9, abs=1.0e-12)
    assert len(peak["support_rows"]) == 3
    assert peak["diagnostics"]["residual_sum_squares"] < 1.0e-24


def test_missing_half_prominence_crossing_is_explicit_and_quality_factor_is_absent():
    _bundle_value, _decision, result = _characterize(
        [0.7, 0.8, 0.9, 0.8, 0.7],
        measurement=_measurement(
            baseline_rule="declared_response", baseline_response_value=0.0
        ),
    )

    candidate = result["candidate"]
    assert candidate["fwhm"]["state"] == "unbracketed"
    assert candidate["fwhm"]["missing_sides"] == ["left", "right"]
    assert candidate["quality_factor"] == {
        "state": "not_computed",
        "reason_code": "fwhm_unbracketed",
        "value": None,
    }


@pytest.mark.parametrize(
    "values,expected_reason",
    [
        ([0.9, 0.5, 0.2, 0.1, 0.05], "classification_boundary_high"),
        ([0.1, 0.8, 0.1, 0.7, 0.1], "classification_multi_candidate"),
        ([0.2, 0.2, 0.2, 0.2, 0.2], "classification_flat"),
    ],
)
def test_nonaccepted_classifications_never_produce_candidate_measurements(values, expected_reason):
    _bundle_value, _decision, result = _characterize(values)

    assert result["measurement_state"] == "not_measured"
    assert result["reason_code"] == expected_reason
    assert result["candidate"] is None


def test_characterization_hash_tampering_and_unknown_configuration_fail_closed():
    bundle, decision, result = _characterize([0.1, 0.5, 0.9, 0.5, 0.1])
    tampered = deepcopy(result)
    tampered["candidate"]["peak"]["wavelength_m"] += 1.0e-9
    with pytest.raises(ValueError, match="noncanonical|hash"):
        validate_spectral_characterization(tampered, bundle=bundle, decision=decision)

    configuration = _measurement()
    configuration["silent_fit_preference"] = "lorentzian"
    with pytest.raises(ValueError, match="fields"):
        build_spectral_characterization(bundle, decision, configuration)


def _fit_measurement(method: str, support: int, sensitivity: list[int], **overrides):
    value = _measurement(
        peak_method=method,
        baseline_rule="declared_response",
        baseline_response_value=0.1,
        fit_support_points=support,
        fit_support_sensitivity_points=sensitivity,
        local_polynomial_degree=2 if method == "local_polynomial_fit" else None,
        fit_max_evaluations=20000,
    )
    value.update(overrides)
    return value


def test_local_polynomial_fit_records_window_residual_covariance_and_conditioning():
    center = 5.013e-6
    wavelengths = [4.8e-6 + index * 0.025e-6 for index in range(17)]
    values = [0.9 - 0.7 * ((wavelength - center) / 0.2e-6) ** 2 for wavelength in wavelengths]
    bundle, decision, result = _characterize(
        values,
        wavelengths=wavelengths,
        measurement=_fit_measurement("local_polynomial_fit", 9, [5, 7, 9]),
    )

    peak = result["candidate"]["peak"]
    diagnostics = peak["diagnostics"]
    assert peak["wavelength_m"] == pytest.approx(center, abs=1.0e-15)
    assert peak["response_value"] == pytest.approx(0.9, abs=1.0e-12)
    assert diagnostics["support_point_count"] == 9
    assert diagnostics["fit_window_m"] == [wavelengths[5], wavelengths[13]]
    assert diagnostics["residual_sum_squares"] < 1.0e-24
    assert diagnostics["covariance_kind"] == "unscaled_design"
    assert diagnostics["covariance"] is not None
    assert diagnostics["covariance_condition"] is not None
    assert diagnostics["design_matrix_condition"] is not None
    assert validate_spectral_characterization(
        result, bundle=bundle, decision=decision
    ) == result


def test_analytic_lorentzian_fit_recovers_peak_fwhm_and_quality_factor():
    center = 5.012e-6
    half_width = 0.04e-6
    wavelengths = [4.8e-6 + index * 0.01e-6 for index in range(45)]
    values = [
        0.1 + 0.8 / (1.0 + ((wavelength - center) / half_width) ** 2)
        for wavelength in wavelengths
    ]
    _bundle_value, _decision, result = _characterize(
        values,
        wavelengths=wavelengths,
        measurement=_fit_measurement("lorentzian_fit", 31, [15, 21, 31]),
    )

    candidate = result["candidate"]
    assert candidate["peak"]["wavelength_m"] == pytest.approx(center, abs=2.0e-12)
    assert candidate["peak"]["response_value"] == pytest.approx(0.9, abs=1.0e-6)
    assert candidate["fwhm"]["value_m"] == pytest.approx(2.0 * half_width, rel=2.0e-4)
    assert candidate["quality_factor"]["value"] == pytest.approx(
        center / (2.0 * half_width), rel=2.0e-4
    )
    assert candidate["peak"]["diagnostics"]["covariance_kind"] == "curve_fit_estimate"


def test_analytic_fano_fit_recovers_peak_and_declared_half_prominence_width():
    center = 5.0e-6
    half_width = 0.04e-6
    asymmetry = 2.0
    wavelengths = [4.7e-6 + index * 0.01e-6 for index in range(61)]

    def fano(wavelength):
        epsilon = (wavelength - center) / half_width
        return 0.1 + 0.12 * (asymmetry + epsilon) ** 2 / (1.0 + epsilon**2)

    values = [fano(wavelength) for wavelength in wavelengths]
    _bundle_value, _decision, result = _characterize(
        values,
        wavelengths=wavelengths,
        measurement=_fit_measurement("fano_fit", 41, [21, 31, 41]),
    )

    expected_peak = center + half_width / asymmetry
    expected_width = (10.0 / 3.0) * half_width
    candidate = result["candidate"]
    assert candidate["peak"]["wavelength_m"] == pytest.approx(expected_peak, abs=2.0e-12)
    assert candidate["peak"]["response_value"] == pytest.approx(0.7, abs=1.0e-6)
    assert candidate["fwhm"]["value_m"] == pytest.approx(expected_width, rel=5.0e-4)
    assert candidate["quality_factor"]["value"] == pytest.approx(
        expected_peak / expected_width, rel=5.0e-4
    )


def test_fit_support_sensitivity_preserves_each_measurement_and_reports_spans():
    center = 5.015e-6
    wavelengths = [4.8e-6 + index * 0.01e-6 for index in range(45)]
    values = [
        0.1 + 0.8 / (1.0 + ((wavelength - center) / 0.045e-6) ** 2)
        for wavelength in wavelengths
    ]
    _bundle_value, _decision, result = _characterize(
        values,
        wavelengths=wavelengths,
        measurement=_fit_measurement("lorentzian_fit", 31, [11, 21, 31]),
    )

    sensitivity = result["candidate"]["fit_support_sensitivity"]
    assert sensitivity["state"] == "measured_not_classified"
    assert [item["support_point_count"] for item in sensitivity["measurements"]] == [11, 21, 31]
    assert all(item["state"] == "measured" for item in sensitivity["measurements"])
    assert all(item["support_rows"] for item in sensitivity["measurements"])
    assert sensitivity["spans"]["peak_wavelength_m"] is not None
    assert sensitivity["spans"]["fwhm_m"] is not None
    assert sensitivity["policy_authority"] is False


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"fit_support_points": 6}, "odd"),
        ({"fit_support_sensitivity_points": [7, 7]}, "unique"),
        ({"fit_max_evaluations": 20}, "100..100000"),
        ({"local_polynomial_degree": 3}, "only valid"),
    ],
)
def test_invalid_fit_configuration_fails_closed(changes, match):
    bundle = _bundle([0.1, 0.4, 0.9, 0.4, 0.1, 0.05, 0.02])
    decision = build_spectral_analysis_decision(bundle, _policy())
    configuration = _fit_measurement("lorentzian_fit", 7, [])
    configuration.update(changes)
    with pytest.raises(ValueError, match=match):
        build_spectral_characterization(bundle, decision, configuration)


def _bundle_spec(bundle):
    return {
        key: deepcopy(bundle[key])
        for key in (
            "bundle_id",
            "source_model",
            "configuration_sha256",
            "parameter_state",
            "wavelength_convention",
            "expressions",
            "rows",
        )
    }


def test_public_tool_returns_three_separate_hash_bound_artifacts():
    bundle = _bundle([0.1, 0.5, 0.9, 0.5, 0.1])
    server = FastMCP("spectral-characterization-test")
    register_spectral_characterization_tools(server)
    tool = server._tool_manager._tools["spectral_characterize"]

    result = tool.fn(
        spectral_bundle=bundle,
        analysis_policy=_policy(),
        measurement_configuration=_measurement(),
    )

    assert result["success"] is True
    assert result["classification"] == "interior_candidate"
    assert result["artifact_separation"] == {
        "raw_measurements": "raw_bundle",
        "policy_decisions": "analysis_decision",
        "derived_measurements": "candidate_measurements",
    }
    assert result["analysis_decision"]["bundle_sha256"] == result["raw_bundle"]["bundle_sha256"]
    assert result["candidate_measurements"]["decision_sha256"] == result["analysis_decision"]["decision_sha256"]
    assert result["solver_started"] is False
    assert result["filesystem_modified"] is False


def test_public_tool_classifies_nonfinite_rows_without_serializing_invalid_numbers():
    bundle = _bundle([0.1, 0.5, 0.9, 0.5, 0.1])
    spec = _bundle_spec(bundle)
    spec["rows"][2]["A"] = float("nan")
    server = FastMCP("spectral-nonfinite-test")
    register_spectral_characterization_tools(server)

    result = server._tool_manager._tools["spectral_characterize"].fn(
        bundle_spec=spec,
        analysis_policy=_policy(),
        measurement_configuration=_measurement(),
    )

    assert result["success"] is False
    assert result["classification"] == "non_finite"
    assert result["invalid_rows"] == [
        {
            "row_id": "point-002",
            "raw_row_sha256": bundle["rows"][2]["raw_row_sha256"],
        }
    ]
    assert result["raw_bundle"] is None
    assert "NaN" not in json.dumps(result)


def test_existing_durable_bundle_bytes_and_timestamp_remain_unchanged(tmp_path):
    bundle = _bundle([0.1, 0.5, 0.9, 0.5, 0.1])
    path = tmp_path / "durable_spectrum.json"
    path.write_text(json.dumps(bundle, sort_keys=True), encoding="utf-8")
    before_bytes = path.read_bytes()
    before_stat = path.stat()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    server = FastMCP("spectral-immutable-input-test")
    register_spectral_characterization_tools(server)

    result = server._tool_manager._tools["spectral_characterize"].fn(
        spectral_bundle=loaded,
        analysis_policy=_policy(),
        measurement_configuration=_measurement(),
    )

    after_stat = path.stat()
    assert result["success"] is True
    assert path.read_bytes() == before_bytes
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert after_stat.st_size == before_stat.st_size


def test_public_tool_requires_exactly_one_input_form_and_import_is_solver_free():
    server = FastMCP("spectral-input-form-test")
    register_spectral_characterization_tools(server)
    tool = server._tool_manager._tools["spectral_characterize"]
    rejected = tool.fn(
        analysis_policy=_policy(), measurement_configuration=_measurement()
    )

    assert rejected["success"] is False
    assert rejected["classification"] == "invalid_input"
    assert "exactly one" in rejected["error"]

    code = """
import mph
mph.Client = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('Client called'))
from mcp.server.fastmcp import FastMCP
from src.tools.spectral_characterization import register_spectral_characterization_tools
server = FastMCP('solver-free-spectral-subprocess')
register_spectral_characterization_tools(server)
result = server._tool_manager._tools['spectral_characterize'].fn(
    analysis_policy={}, measurement_configuration={}
)
assert result['success'] is False
assert result['solver_started'] is False
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
