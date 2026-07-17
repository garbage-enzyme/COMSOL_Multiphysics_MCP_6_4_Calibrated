"""Adaptive spectral stage transition and scientific outcome tests."""

from __future__ import annotations

import hashlib
import json

import pytest

from src.jobs.spectral_characterization import normalize_spectral_characterization_job_spec
from src.jobs.spectral_progress import build_spectral_progress
from src.jobs.spectral_stages import build_initial_spectral_stage


def _spec(tmp_path, *, maximum_points=20, maximum_expansions=1, absolute_upper=7e-6):
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
                "maximum_expansions": maximum_expansions,
                "points_per_expansion": 5,
                "span_multiplier": 1.5,
                "absolute_lower_m": 3e-6,
                "absolute_upper_m": absolute_upper,
            },
            "maximum_points": maximum_points,
            "collector": {
                "name": "wave_optics_point_audit",
                "inputs": {
                    "component_tag": "comp1",
                    "physics_tag": "ewfd",
                    "study_tag": "std1",
                    "study_step_tag": "freq",
                    "study_step_property": "plist",
                    "r_expression": "R_expr",
                    "t_expression": "T_expr",
                    "a_expression": "A_expr",
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
                "wall_time_budget_seconds": maximum_points * 10,
                "minimum_next_point_seconds": 10,
                "max_mesh_elements": 1000,
            },
            "cores": 1,
        }
    )


def _rows(spec, plan, values):
    rows = []
    for index, (point, wavelength, absorption) in enumerate(
        zip(plan["requested_points"], plan["requested_wavelengths_m"], values), 1
    ):
        raw = {
            "point": point["point_fingerprint"],
            "wavelength": wavelength,
            "absorption": absorption,
        }
        rows.append(
            {
                "sequence": index,
                "stage_index": plan["stage_index"],
                "stage_kind": plan["stage_kind"],
                "point_id": point["point_id"],
                "point_fingerprint": point["point_fingerprint"],
                "row_sha256": hashlib.sha256(
                    json.dumps(raw, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                "configuration_sha256": spec["configuration_sha256"],
                "requested_wavelength_m": wavelength,
                "evaluated_wavelength_m": wavelength,
                "frequency_wavelength_m": wavelength,
                "R": 0.95 - absorption,
                "T": 0.05,
                "A": absorption,
            }
        )
    return rows


def test_initial_and_pending_stage_actions_are_explicit(tmp_path):
    spec = _spec(tmp_path)
    initial = build_spectral_progress(spec, [], [])
    assert initial["action"] == "schedule_next_stage"
    plan = initial["next_stage_plan"]
    pending = build_spectral_progress(spec, [plan], _rows(spec, plan, [0.1, 0.2])[:2])
    assert pending["action"] == "solve_current_stage"
    assert len(pending["pending_points"]) == 3
    assert pending["analysis"] is None


def test_interior_candidate_schedules_refinement_then_accepts_own_peak(tmp_path):
    spec = _spec(tmp_path)
    initial = build_initial_spectral_stage(spec)
    initial_rows = _rows(spec, initial, [0.1, 0.3, 0.9, 0.3, 0.1])
    progress = build_spectral_progress(spec, [initial], initial_rows)
    assert progress["action"] == "schedule_next_stage"
    refinement = progress["next_stage_plan"]
    assert refinement["stage_kind"] == "refinement"

    refinement_values = [
        0.1 + 0.8 / (1.0 + ((wavelength - 5e-6) / 0.15e-6) ** 2)
        for wavelength in refinement["requested_wavelengths_m"]
    ]
    refined_rows = _rows(spec, refinement, refinement_values)
    for offset, row in enumerate(refined_rows, len(initial_rows) + 1):
        row["sequence"] = offset
    completed = build_spectral_progress(
        spec, [initial, refinement], initial_rows + refined_rows
    )
    assert completed["action"] == "complete"
    assert completed["scientific_disposition"] == "accepted"
    candidate = completed["analysis"]["characterization"]["candidate"]
    assert candidate["peak"]["wavelength_m"] == 5e-6
    assert candidate["fwhm"]["state"] == "bracketed"
    assert candidate["quality_factor"]["value"] > 0


def test_boundary_high_schedules_bounded_expansion(tmp_path):
    spec = _spec(tmp_path)
    initial = build_initial_spectral_stage(spec)
    rows = _rows(spec, initial, [0.1, 0.2, 0.3, 0.5, 0.9])
    progress = build_spectral_progress(spec, [initial], rows)

    assert progress["action"] == "schedule_next_stage"
    expansion = progress["next_stage_plan"]
    assert expansion["stage_kind"] == "window_expansion"
    assert expansion["window"]["upper_m"] == 7e-6
    assert all(point["point_fingerprint"] not in {item["point_fingerprint"] for item in initial["requested_points"]} for point in expansion["requested_points"])


def test_boundary_at_absolute_bound_completes_unresolved_without_execution_failure(tmp_path):
    spec = _spec(tmp_path, absolute_upper=6e-6)
    initial = build_initial_spectral_stage(spec)
    progress = build_spectral_progress(
        spec, [initial], _rows(spec, initial, [0.1, 0.2, 0.3, 0.5, 0.9])
    )
    assert progress["action"] == "complete"
    assert progress["scientific_disposition"] == "unresolved_at_declared_cap"
    assert progress["declared_cap_reached"] is True
    assert "bound" in progress["reason_code"]


def test_point_cap_blocks_expansion_without_partial_implicit_grid(tmp_path):
    spec = _spec(tmp_path, maximum_points=5)
    initial = build_initial_spectral_stage(spec)
    progress = build_spectral_progress(
        spec, [initial], _rows(spec, initial, [0.1, 0.2, 0.3, 0.5, 0.9])
    )
    assert progress["action"] == "complete"
    assert progress["reason_code"] == "window_expansion_point_cap_reached"
    assert progress["next_stage_plan"] is None


@pytest.mark.parametrize(
    "values,reason",
    [
        ([0.2, 0.2, 0.2, 0.2, 0.2], "classification_flat"),
        ([0.1, 0.8, 0.1, 0.7, 0.1], "classification_multi_candidate"),
        ([0.1, 0.2, 0.3, 0.4, 0.4], "classification_no_candidate"),
    ],
)
def test_normal_scientific_nonacceptance_is_not_an_execution_error(tmp_path, values, reason):
    spec = _spec(tmp_path)
    initial = build_initial_spectral_stage(spec)
    progress = build_spectral_progress(spec, [initial], _rows(spec, initial, values))
    assert progress["action"] == "complete"
    assert progress["scientific_disposition"] == "residual"
    assert progress["reason_code"] == reason


def test_unplanned_or_wrong_stage_row_fails_closed(tmp_path):
    spec = _spec(tmp_path)
    initial = build_initial_spectral_stage(spec)
    rows = _rows(spec, initial, [0.1, 0.3, 0.9, 0.3, 0.1])
    rows[0]["stage_index"] = 2
    with pytest.raises(ValueError, match="stage identity"):
        build_spectral_progress(spec, [initial], rows)


def test_rehashed_adaptive_stage_with_changed_targets_fails_replay(tmp_path):
    spec = _spec(tmp_path)
    initial = build_initial_spectral_stage(spec)
    rows = _rows(spec, initial, [0.1, 0.3, 0.9, 0.3, 0.1])
    progress = build_spectral_progress(spec, [initial], rows)
    tampered = dict(progress["next_stage_plan"])
    tampered["planning_reason"] = "attacker_selected_targets"
    body = {key: value for key, value in tampered.items() if key != "stage_sha256"}
    tampered["stage_sha256"] = hashlib.sha256(
        json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()

    with pytest.raises(ValueError, match="deterministic evidence replay"):
        build_spectral_progress(spec, [initial, tampered], rows)
