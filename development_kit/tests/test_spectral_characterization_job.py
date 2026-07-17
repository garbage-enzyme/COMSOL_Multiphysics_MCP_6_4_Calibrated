"""Solver-free durable spectral-characterization specification tests."""

from __future__ import annotations

from copy import deepcopy

import pytest

from src.jobs.spectral_characterization import (
    MAX_REFINEMENT_STAGES,
    current_spectral_driver_identity,
    normalize_spectral_characterization_job_spec,
    validate_spectral_driver_identity,
)


def _raw_spec(tmp_path) -> dict:
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"immutable model")
    return {
        "job_type": "spectral_characterization",
        "source_model_path": str(source),
        "source_model_relative_identity": "fixtures/passive_spectrum.mph",
        "configuration_sha256": "a" * 64,
        "parameter_state": {"incidence": {"theta_degrees": 0.0}, "mesh": "declared"},
        "wavelength_parameter": "wl",
        "initial_grid": {"lower_m": 4.0e-6, "upper_m": 6.0e-6, "point_count": 9},
        "refinement_policy": {
            "maximum_stages": 2,
            "points_per_stage": 7,
            "span_shrink_factor": 4.0,
            "minimum_spacing_m": 1.0e-10,
            "peak_shift_abs_tolerance_m": 1.0e-9,
            "fit_support_peak_abs_tolerance_m": 1.0e-9,
            "fit_support_fwhm_abs_tolerance_m": 1.0e-9,
            "fit_support_quality_factor_abs_tolerance": 1.0,
        },
        "expansion_policy": {
            "maximum_expansions": 2,
            "points_per_expansion": 5,
            "span_multiplier": 1.5,
            "absolute_lower_m": 3.0e-6,
            "absolute_upper_m": 7.0e-6,
        },
        "maximum_points": 40,
        "collector": {
            "name": "wave_optics_point_audit",
            "inputs": {
                "component_tag": "comp1",
                "physics_tag": "ewfd",
                "study_tag": "std1",
                "study_step_tag": "freq",
                "study_step_property": "plist",
                "r_expression": "ewfd.Rtotal",
                "t_expression": "ewfd.Ttotal",
                "a_expression": "ewfd.Atotal",
                "top_air_domain_ids": [1],
                "top_air_coordinate_range": {"x": [-1.0, 1.0], "y": [-1.0, 1.0], "z": [-1.0, 1.0]},
            },
        },
        "analysis_policy": {
            "response_quantity": "A",
            "candidate_polarity": "maximum",
            "passivity_abs_tolerance": 1.0e-9,
            "closure_abs_tolerance": 1.0e-9,
            "wavelength_sync_abs_m": 1.0e-12,
            "flat_response_abs_tolerance": 1.0e-8,
            "minimum_point_count": 5,
        },
        "measurement_configuration": {
            "peak_method": "quadratic_interpolation",
            "baseline_rule": "local_prominence",
            "baseline_response_value": None,
            "fwhm_definition": "half_prominence",
            "fit_support_points": None,
            "fit_support_sensitivity_points": [],
            "local_polynomial_degree": None,
            "fit_max_evaluations": None,
        },
        "resource_policy": {
            "wall_time_budget_seconds": 4000,
            "minimum_next_point_seconds": 100,
            "max_mesh_elements": 100_000,
        },
        "cores": 2,
    }

def test_spec_is_canonical_hash_bound_and_solver_free(tmp_path):
    raw = _raw_spec(tmp_path)
    first = normalize_spectral_characterization_job_spec(raw)
    second = normalize_spectral_characterization_job_spec(deepcopy(raw))

    assert first == second
    assert first["source_model_sha256"]
    assert first["parameter_state_sha256"]
    assert first["spec_fingerprint"]
    assert first["resource_policy"]["host_defaults_applied"] is False
    assert first["analysis_policy"] == raw["analysis_policy"]
    assert first["driver_identity"] == current_spectral_driver_identity()
    assert validate_spectral_driver_identity(first) == first["driver_identity"]
    assert first["measurement_configuration"]["peak_method"] == "quadratic_interpolation"


def test_configuration_and_collector_identity_change_the_fingerprint(tmp_path):
    raw = _raw_spec(tmp_path)
    baseline = normalize_spectral_characterization_job_spec(raw)
    changed_configuration = deepcopy(raw)
    changed_configuration["configuration_sha256"] = "b" * 64
    changed_collector = deepcopy(raw)
    changed_collector["collector"]["inputs"]["physics_tag"] = "ewfd2"

    assert normalize_spectral_characterization_job_spec(changed_configuration)["spec_fingerprint"] != baseline["spec_fingerprint"]
    assert normalize_spectral_characterization_job_spec(changed_collector)["spec_fingerprint"] != baseline["spec_fingerprint"]


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda spec: spec.__setitem__("automatic_tolerance", 1.0), "unsupported"),
        (lambda spec: spec["initial_grid"].__setitem__("lower_m", float("nan")), "finite"),
        (lambda spec: spec["initial_grid"].__setitem__("point_count", 2), "3 to"),
        (lambda spec: spec["refinement_policy"].__setitem__("maximum_stages", MAX_REFINEMENT_STAGES + 1), "0 to"),
        (lambda spec: spec["refinement_policy"].__setitem__("points_per_stage", 6), "odd"),
        (lambda spec: spec["refinement_policy"].__setitem__("peak_shift_abs_tolerance_m", -1.0), "nonnegative"),
        (lambda spec: spec["expansion_policy"].__setitem__("absolute_lower_m", 5.0e-6), "contain"),
        (lambda spec: spec.__setitem__("maximum_points", 8), "initial grid"),
        (lambda spec: spec["collector"]["inputs"].__setitem__("wavelength_value", 5.0e-6), "locked"),
        (lambda spec: spec["collector"]["inputs"].__setitem__("output_path", "outside.json"), "unsupported"),
        (lambda spec: spec["collector"]["inputs"].pop("r_expression"), "missing"),
        (lambda spec: spec["collector"]["inputs"].pop("top_air_coordinate_range"), "missing"),
        (lambda spec: spec.__setitem__("source_model_relative_identity", "../private.mph"), "relative path"),
        (lambda spec: spec["analysis_policy"].__setitem__("hidden", 1.0), "fields"),
    ],
)
def test_invalid_or_hidden_policy_inputs_fail_closed(tmp_path, mutation, match):
    raw = _raw_spec(tmp_path)
    mutation(raw)
    with pytest.raises(ValueError, match=match):
        normalize_spectral_characterization_job_spec(raw)


def test_point_cap_must_fit_the_declared_wall_budget(tmp_path):
    raw = _raw_spec(tmp_path)
    raw["resource_policy"]["wall_time_budget_seconds"] = 3999
    with pytest.raises(ValueError, match="wall-time budget"):
        normalize_spectral_characterization_job_spec(raw)


def test_source_bytes_are_part_of_the_immutable_identity(tmp_path):
    raw = _raw_spec(tmp_path)
    before = normalize_spectral_characterization_job_spec(raw)
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"changed model")
    after = normalize_spectral_characterization_job_spec(raw)

    assert before["source_model_sha256"] != after["source_model_sha256"]
    assert before["spec_fingerprint"] != after["spec_fingerprint"]


def test_changed_driver_identity_cannot_resume_existing_rows(tmp_path):
    spec = normalize_spectral_characterization_job_spec(_raw_spec(tmp_path))
    spec["driver_identity"]["package_content_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="driver identity"):
        validate_spectral_driver_identity(spec)
