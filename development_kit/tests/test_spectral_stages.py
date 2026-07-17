"""Immutable spectral stage planning and replay tests."""

from __future__ import annotations

from copy import deepcopy
import json
import math

import pytest

from src.jobs.spectral_characterization import normalize_spectral_characterization_job_spec
from src.jobs.spectral_stages import (
    build_initial_spectral_stage,
    build_spectral_stage_plan,
    inclusive_wavelength_grid,
    read_spectral_stage_plans,
    validate_spectral_stage_plan,
    write_spectral_stage_plan,
)


def _spec(tmp_path) -> dict:
    source = tmp_path / "source.mph"
    source.write_bytes(b"model")
    return normalize_spectral_characterization_job_spec(
        {
            "job_type": "spectral_characterization",
            "source_model_path": str(source),
            "source_model_relative_identity": "fixtures/source.mph",
            "configuration_sha256": "a" * 64,
            "parameter_state": {"mesh": "declared"},
            "wavelength_parameter": "wl",
            "initial_grid": {"lower_m": 4e-6, "upper_m": 6e-6, "point_count": 5},
            "refinement_policy": {
                "maximum_stages": 1,
                "points_per_stage": 5,
                "span_shrink_factor": 4.0,
                "minimum_spacing_m": 1e-10,
                "peak_shift_abs_tolerance_m": 1e-9,
                "fit_support_peak_abs_tolerance_m": 1e-9,
                "fit_support_fwhm_abs_tolerance_m": 1e-9,
                "fit_support_quality_factor_abs_tolerance": 1.0,
            },
            "expansion_policy": {
                "maximum_expansions": 1,
                "points_per_expansion": 5,
                "span_multiplier": 1.5,
                "absolute_lower_m": 3e-6,
                "absolute_upper_m": 7e-6,
            },
            "maximum_points": 10,
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
                "passivity_abs_tolerance": 1e-9,
                "closure_abs_tolerance": 1e-9,
                "wavelength_sync_abs_m": 1e-12,
                "flat_response_abs_tolerance": 1e-8,
                "minimum_point_count": 5,
            },
            "measurement_configuration": {
                "peak_method": "measured_grid",
                "baseline_rule": "local_prominence",
                "baseline_response_value": None,
                "fwhm_definition": "half_prominence",
                "fit_support_points": None,
                "fit_support_sensitivity_points": [],
                "local_polynomial_degree": None,
                "fit_max_evaluations": None,
            },
            "resource_policy": {
                "wall_time_budget_seconds": 100,
                "minimum_next_point_seconds": 10,
                "max_mesh_elements": 1000,
            },
            "cores": 1,
        }
    )


def test_initial_grid_and_stage_are_deterministic_and_inclusive(tmp_path):
    spec = _spec(tmp_path)
    plan = build_initial_spectral_stage(spec)

    assert plan["requested_wavelengths_m"] == [4e-6, 4.5e-6, 5e-6, 5.5e-6, 6e-6]
    assert plan["stage_index"] == 0
    assert plan["stage_kind"] == "initial_locator"
    assert plan["previous_stage_sha256"] is None
    assert plan["evidence_row_sha256"] is None
    assert validate_spectral_stage_plan(
        plan, spec, expected_index=0, previous_stage_sha256=None
    ) == plan


def test_stage_is_atomically_frozen_and_exact_replay_is_idempotent(tmp_path):
    spec = _spec(tmp_path)
    job = tmp_path / "job"
    plan = build_initial_spectral_stage(spec)

    assert write_spectral_stage_plan(job, spec, plan) == plan
    before = (job / "stage_plans" / "000.json").read_bytes()
    assert read_spectral_stage_plans(job, spec) == [plan]
    assert (job / "stage_plans" / "000.json").read_bytes() == before


def test_tampering_and_noncontiguous_files_fail_closed(tmp_path):
    spec = _spec(tmp_path)
    job = tmp_path / "job"
    plan = write_spectral_stage_plan(job, spec, build_initial_spectral_stage(spec))
    path = job / "stage_plans" / "000.json"
    tampered = deepcopy(plan)
    tampered["requested_wavelengths_m"][2] = 5.1e-6
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="noncanonical|hash"):
        read_spectral_stage_plans(job, spec)

    path.unlink()
    (job / "stage_plans" / "001.json").write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(ValueError, match="filenames"):
        read_spectral_stage_plans(job, spec)


def test_later_stage_must_chain_and_cannot_repeat_an_exact_point(tmp_path):
    spec = _spec(tmp_path)
    job = tmp_path / "job"
    initial = write_spectral_stage_plan(job, spec, build_initial_spectral_stage(spec))
    duplicate = build_spectral_stage_plan(
        spec,
        stage_index=1,
        stage_kind="refinement",
        planning_reason="measured_candidate_refinement",
        window_lower_m=4.5e-6,
        window_upper_m=5.5e-6,
        requested_wavelengths_m=[5e-6],
        previous_stage_sha256=initial["stage_sha256"],
        evidence_row_sha256="b" * 64,
    )
    with pytest.raises(ValueError, match="duplicate"):
        write_spectral_stage_plan(job, spec, duplicate)


def test_float_precision_collapse_and_out_of_window_targets_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="strictly increasing"):
        inclusive_wavelength_grid(1.0, math.nextafter(1.0, 2.0), 4)
    spec = _spec(tmp_path)
    with pytest.raises(ValueError, match="inside"):
        build_spectral_stage_plan(
            spec,
            stage_index=0,
            stage_kind="initial_locator",
            planning_reason="invalid",
            window_lower_m=4e-6,
            window_upper_m=6e-6,
            requested_wavelengths_m=[3e-6, 5e-6],
            previous_stage_sha256=None,
            evidence_row_sha256=None,
        )


def test_stage_targets_deduplicate_one_ulp_center_variants(tmp_path):
    spec = _spec(tmp_path)
    initial = build_initial_spectral_stage(spec)
    center = initial["requested_points"][2]["point_fingerprint"]
    variant = build_spectral_stage_plan(
        spec,
        stage_index=1,
        stage_kind="refinement",
        planning_reason="precision_regression",
        window_lower_m=4.9e-6,
        window_upper_m=5.1e-6,
        requested_wavelengths_m=[4.999999999999999e-6],
        previous_stage_sha256=initial["stage_sha256"],
        evidence_row_sha256="b" * 64,
    )
    assert variant["requested_points"][0]["point_fingerprint"] == center
    assert variant["requested_wavelengths_m"] == [5e-6]
