"""Solver-free spectral evidence and characterization regression tests."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import pytest

from src.evidence.spectral_characterization import (
    build_spectral_analysis_decision,
    build_spectral_characterization,
    build_spectral_point_bundle,
    validate_spectral_analysis_decision,
    validate_spectral_characterization,
    validate_spectral_point_bundle,
)


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
