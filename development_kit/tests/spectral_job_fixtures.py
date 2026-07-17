"""Sanitized fake point-audit artifacts for durable spectral job tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.evidence.contracts import build_physical_evidence
from src.jobs.spectral_characterization import normalize_spectral_characterization_job_spec


def spectral_job_spec(tmp_path: Path, *, maximum_points: int = 20) -> dict:
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


def write_fake_point_audit(
    artifact_dir: Path,
    spec: dict,
    point: dict,
    *,
    absorption: float,
) -> dict:
    inner = artifact_dir / "audit" / "manifest.json"
    inner.parent.mkdir(parents=True, exist_ok=True)
    physical = build_physical_evidence(
        {
            "schema_name": "comsol_mcp.physical_evidence",
            "schema_version": "1.1.0",
            "artifact_type": "wave_optics_point_audit",
            "producer": {"tool": "wave_optics_point_audit", "tool_schema_version": "fake"},
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
    wavelength = point["wavelength"]["value"]
    inner.write_text(
        json.dumps(
            {
                "audit_status": "measurement_complete",
                "measurement": {
                    "wavelength": {
                        "requested_m": wavelength,
                        "evaluated_parameter_m": wavelength,
                        "solved_frequency_wavelength_m": wavelength,
                    },
                    "power": {"R": 0.95 - absorption, "T": 0.05, "A": absorption},
                    "mesh": {"element_count": 12, "vertex_count": 8},
                    "solve": {"ran": True, "seconds": 0.25, "error": None},
                    "measurement_errors": [],
                    "integrity_errors": [],
                },
                "physical_evidence": physical,
            }
        ),
        encoding="utf-8",
    )
    wrapper = artifact_dir / "matrix_collector.json"
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
                    "relative_path": inner.relative_to(artifact_dir).as_posix(),
                    "sha256": hashlib.sha256(inner.read_bytes()).hexdigest(),
                    "size_bytes": inner.stat().st_size,
                },
            }
        ),
        encoding="utf-8",
    )
    return {
        "success": True,
        "audit_status": "measurement_complete",
        "artifacts": {"manifest": str(wrapper)},
    }
