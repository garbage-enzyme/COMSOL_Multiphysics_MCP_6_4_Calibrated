"""Strict point-audit to durable-spectral-row adapter tests."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import pytest

from src.evidence.contracts import build_physical_evidence
from src.jobs.spectral_audit import build_spectral_audit_point, extract_spectral_audit_result
from src.jobs.spectral_characterization import normalize_spectral_characterization_job_spec


def _spec(tmp_path):
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
                "maximum_stages": 0,
                "points_per_stage": 5,
                "span_shrink_factor": 4.0,
                "minimum_spacing_m": 1e-10,
                "peak_shift_abs_tolerance_m": 1e-9,
                "fit_support_peak_abs_tolerance_m": 1e-9,
                "fit_support_fwhm_abs_tolerance_m": 1e-9,
                "fit_support_quality_factor_abs_tolerance": 1.0,
            },
            "expansion_policy": {
                "maximum_expansions": 0,
                "points_per_expansion": 5,
                "span_multiplier": 1.5,
                "absolute_lower_m": 4e-6,
                "absolute_upper_m": 6e-6,
            },
            "maximum_points": 5,
            "collector": {
                "name": "wave_optics_point_audit",
                "inputs": {
                    "component_tag": "comp1",
                    "physics_tag": "ewfd",
                    "study_tag": "std1",
                    "study_step_tag": "freq",
                    "study_step_property": "plist",
                    "r_expression": "R",
                    "t_expression": "T",
                    "a_expression": "A",
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
                "wall_time_budget_seconds": 50,
                "minimum_next_point_seconds": 10,
                "max_mesh_elements": 1000,
            },
            "cores": 1,
        }
    )


def _result(job, spec, point):
    artifact = job / "point_artifacts" / point["point_fingerprint"]
    inner = artifact / "audit" / "manifest.json"
    inner.parent.mkdir(parents=True, exist_ok=True)
    physical = build_physical_evidence(
        {
            "schema_name": "comsol_mcp.physical_evidence",
            "schema_version": "1.1.0",
            "artifact_type": "wave_optics_point_audit",
            "producer": {"tool": "wave_optics_point_audit", "tool_schema_version": "test"},
            "identity": {
                "config_id": point["point_fingerprint"],
                "config_sha256": "b" * 64,
                "source_sha256": spec["source_model_sha256"],
            },
            "model": {
                "component_tag": "comp1",
                "physics_tag": "ewfd",
                "study_tag": "std1",
                "study_step_tag": "freq",
                "mesh_tag": "mesh1",
                "mesh_element_count": 12,
                "mesh_vertex_count": 8,
            },
            "evidence": {},
            "limitations": [],
        }
    )
    measurement = {
        "wavelength": {
            "requested_m": point["wavelength"]["value"],
            "evaluated_parameter_m": point["wavelength"]["value"],
            "solved_frequency_wavelength_m": point["wavelength"]["value"],
        },
        "power": {"R": 0.1, "T": 0.05, "A": 0.85},
        "mesh": {"element_count": 12, "vertex_count": 8},
        "solve": {"ran": True, "seconds": 0.5, "error": None},
        "measurement_errors": [],
        "integrity_errors": [],
    }
    inner.write_text(
        json.dumps(
            {
                "audit_status": "measurement_complete",
                "measurement": measurement,
                "physical_evidence": physical,
            }
        ),
        encoding="utf-8",
    )
    wrapper = artifact / "matrix_collector.json"
    wrapper.write_text(
        json.dumps(
            {
                "schema_name": "comsol_mcp.validation_matrix_collector",
                "schema_version": "1.0.0",
                "collector": "wave_optics_point_audit",
                "point": {
                    "point_id": point["point_id"],
                    "point_fingerprint": point["point_fingerprint"],
                    "configuration_sha256": point["configuration_sha256"],
                    "wavelength": point["wavelength"],
                    "incidence": point["incidence"],
                    "incidence_application": "not_mutated_by_collector_adapter",
                },
                "source_model_sha256": spec["source_model_sha256"],
                "audit_status": "measurement_complete",
                "inner_manifest": {
                    "relative_path": inner.relative_to(artifact).as_posix(),
                    "sha256": hashlib.sha256(inner.read_bytes()).hexdigest(),
                    "size_bytes": inner.stat().st_size,
                },
            }
        ),
        encoding="utf-8",
    )
    return artifact, {
        "success": True,
        "audit_status": "measurement_complete",
        "artifacts": {"manifest": str(wrapper)},
    }


def test_point_identity_and_complete_audit_projection(tmp_path):
    spec = _spec(tmp_path)
    job = tmp_path / "job"
    point = build_spectral_audit_point(spec, 5e-6)
    artifact, result = _result(job, spec, point)
    projected = extract_spectral_audit_result(
        job_dir=job,
        artifact_dir=artifact,
        spec=spec,
        point=point,
        result=result,
    )

    assert point["wavelength"] == {"value": 5e-6, "unit": "m", "parameter": "wl"}
    assert projected["R"] == 0.1
    assert projected["T"] == 0.05
    assert projected["A"] == 0.85
    assert projected["mesh_element_count"] == 12
    assert projected["mesh_vertex_count"] == 8
    assert projected["audit_artifact"]["physical_evidence_sha256"]


def test_incomplete_audit_is_not_projected(tmp_path):
    spec = _spec(tmp_path)
    job = tmp_path / "job"
    point = build_spectral_audit_point(spec, 5e-6)
    artifact, result = _result(job, spec, point)
    result["audit_status"] = "measurement_partial"
    with pytest.raises(ValueError, match="incomplete"):
        extract_spectral_audit_result(job_dir=job, artifact_dir=artifact, spec=spec, point=point, result=result)


def test_wrapper_and_inner_tampering_fail_closed(tmp_path):
    spec = _spec(tmp_path)
    job = tmp_path / "job"
    point = build_spectral_audit_point(spec, 5e-6)
    artifact, result = _result(job, spec, point)
    wrapper_path = artifact / "matrix_collector.json"
    wrapper = json.loads(wrapper_path.read_text(encoding="utf-8"))
    wrapper["point"]["configuration_sha256"] = "c" * 64
    wrapper_path.write_text(json.dumps(wrapper), encoding="utf-8")
    with pytest.raises(ValueError, match="configuration_sha256"):
        extract_spectral_audit_result(job_dir=job, artifact_dir=artifact, spec=spec, point=point, result=result)

    artifact, result = _result(job, spec, point)
    inner = next((artifact / "audit").glob("manifest.json"))
    document = json.loads(inner.read_text(encoding="utf-8"))
    document["measurement"]["power"]["A"] = 0.5
    inner.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="size|hash"):
        extract_spectral_audit_result(job_dir=job, artifact_dir=artifact, spec=spec, point=point, result=result)


def test_artifact_must_remain_inside_job_directory(tmp_path):
    spec = _spec(tmp_path)
    job = tmp_path / "job"
    point = build_spectral_audit_point(spec, 5e-6)
    artifact, result = _result(job, spec, point)
    outside = tmp_path / "outside"
    outside.mkdir()
    wrapper = artifact / "matrix_collector.json"
    moved = outside / wrapper.name
    moved.write_bytes(wrapper.read_bytes())
    result["artifacts"]["manifest"] = str(moved)
    with pytest.raises(ValueError, match="assigned artifact"):
        extract_spectral_audit_result(job_dir=job, artifact_dir=artifact, spec=spec, point=point, result=result)
