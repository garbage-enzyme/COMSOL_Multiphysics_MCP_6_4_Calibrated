"""Durable raw spectral row integrity and exact-resume tests."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest
from src.evidence.contracts import build_physical_evidence
from src.jobs.spectral_characterization import normalize_spectral_characterization_job_spec
from src.jobs.spectral_rows import (
    MAX_SPECTRAL_ROW_BYTES,
    append_spectral_row,
    completed_spectral_point_fingerprints,
    read_spectral_rows,
    spectral_point_identity,
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
                    "top_air_coordinate_range": {
                        "x": [-1.0, 1.0],
                        "y": [-1.0, 1.0],
                        "z": [-1.0, 1.0],
                    },
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


def _artifact(root: Path, spec: dict, wavelength: float) -> dict:
    point = spectral_point_identity(spec, wavelength)
    physical = build_physical_evidence(
        {
            "schema_name": "comsol_mcp.physical_evidence",
            "schema_version": "1.1.0",
            "artifact_type": "wave_optics_point_audit",
            "producer": {"tool": "wave_optics_point_audit", "tool_schema_version": "test"},
            "identity": {
                "config_id": point["point_fingerprint"],
                "config_sha256": spec["configuration_sha256"],
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
    inner = root / f"point-{wavelength:.9e}" / "manifest.json"
    inner.parent.mkdir(parents=True, exist_ok=True)
    inner.write_text(
        json.dumps(
            {
                "audit_status": "measurement_complete",
                "physical_evidence": physical,
            }
        ),
        encoding="utf-8",
    )
    wrapper = inner.parent / "matrix_collector.json"
    wrapper.write_text(
        json.dumps(
            {
                "schema_name": "comsol_mcp.validation_matrix_collector",
                "schema_version": "1.0.0",
                "collector": "wave_optics_point_audit",
                "point": {
                    "point_id": point["point_id"],
                    "point_fingerprint": point["point_fingerprint"],
                    "configuration_sha256": spec["configuration_sha256"],
                    "wavelength": {
                        "value": wavelength,
                        "unit": "m",
                        "parameter": spec["wavelength_parameter"],
                    },
                    "incidence": None,
                    "incidence_application": "not_mutated_by_collector_adapter",
                },
                "source_model_sha256": spec["source_model_sha256"],
                "audit_status": "measurement_complete",
                "inner_manifest": {
                    "relative_path": inner.relative_to(inner.parent).as_posix(),
                    "sha256": hashlib.sha256(inner.read_bytes()).hexdigest(),
                    "size_bytes": inner.stat().st_size,
                },
            }
        ),
        encoding="utf-8",
    )
    return {
        "wrapper_relative_path": wrapper.relative_to(root).as_posix(),
        "wrapper_sha256": hashlib.sha256(wrapper.read_bytes()).hexdigest(),
        "wrapper_size_bytes": wrapper.stat().st_size,
        "inner_relative_path": inner.relative_to(root).as_posix(),
        "inner_sha256": hashlib.sha256(inner.read_bytes()).hexdigest(),
        "inner_size_bytes": inner.stat().st_size,
        "physical_evidence_sha256": physical["contract_sha256"],
        "audit_status": "measurement_complete",
    }


def _append(
    path: Path,
    root: Path,
    spec: dict,
    wavelength: float,
    absorption: float,
    *,
    reflectance: float | None = None,
):
    return append_spectral_row(
        path,
        spec,
        attempt=1,
        stage_index=0,
        stage_kind="initial_locator",
        requested_wavelength_m=wavelength,
        evaluated_wavelength_m=wavelength,
        frequency_wavelength_m=wavelength,
        R=0.95 - absorption if reflectance is None else reflectance,
        T=0.05,
        A=absorption,
        mesh_element_count=12,
        mesh_vertex_count=8,
        solve_seconds=0.2,
        audit_artifact=_artifact(root, spec, wavelength),
        artifact_root=root,
        created_at_epoch=1000.0 + wavelength,
    )


def test_rows_are_hash_chained_artifact_verified_and_resumable(tmp_path):
    spec = _spec(tmp_path)
    root = tmp_path / "job"
    journal = root / "spectral_rows.jsonl"
    first = _append(journal, root, spec, 4e-6, 0.1)
    second = _append(journal, root, spec, 5e-6, 0.8)

    rows = read_spectral_rows(journal, spec, artifact_root=root)
    assert rows == [first, second]
    assert second["previous_row_sha256"] == first["row_sha256"]
    assert completed_spectral_point_fingerprints(journal, spec, artifact_root=root) == {
        first["point_fingerprint"],
        second["point_fingerprint"],
    }
    assert first["point_id"] == spectral_point_identity(spec, 4e-6)["point_id"]


def test_exact_complete_wavelength_cannot_be_appended_twice(tmp_path):
    spec = _spec(tmp_path)
    root = tmp_path / "job"
    journal = root / "spectral_rows.jsonl"
    _append(journal, root, spec, 4e-6, 0.1)
    with pytest.raises(ValueError, match="already exists"):
        _append(journal, root, spec, 4e-6, 0.1)


@pytest.mark.parametrize(
    "absorption",
    [float("inf"), pytest.param(10**10_000, id="huge-integer")],
)
def test_nonfinite_equivalent_rows_share_the_validation_error_boundary(tmp_path, absorption):
    spec = _spec(tmp_path)
    root = tmp_path / "job"

    with pytest.raises(ValueError, match="finite"):
        _append(
            root / "spectral_rows.jsonl",
            root,
            spec,
            4e-6,
            absorption,
            reflectance=0.0,
        )


def test_row_and_artifact_tampering_fail_before_resume(tmp_path):
    spec = _spec(tmp_path)
    root = tmp_path / "job"
    journal = root / "spectral_rows.jsonl"
    row = _append(journal, root, spec, 4e-6, 0.1)
    value = json.loads(journal.read_text(encoding="utf-8"))
    value["A"] = 0.2
    journal.write_text(json.dumps(value) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="row hash"):
        read_spectral_rows(journal, spec, artifact_root=root)

    journal.write_text(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    inner = root / row["audit_artifact"]["inner_relative_path"]
    inner.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="size|hash"):
        read_spectral_rows(journal, spec, artifact_root=root)


def test_append_hashes_the_accepted_canonical_row_representation(tmp_path):
    spec = _spec(tmp_path)
    root = tmp_path / "job"
    artifact = _artifact(root, spec, 4e-6)
    for field in (
        "wrapper_sha256",
        "inner_sha256",
        "physical_evidence_sha256",
    ):
        artifact[field] = artifact[field].upper()

    row = append_spectral_row(
        root / "spectral_rows.jsonl",
        spec,
        attempt=1,
        stage_index=0,
        stage_kind="initial_locator",
        requested_wavelength_m=4e-6,
        evaluated_wavelength_m=4e-6,
        frequency_wavelength_m=4e-6,
        R=0,
        T=0,
        A=1,
        mesh_element_count=0,
        mesh_vertex_count=0,
        solve_seconds=0,
        audit_artifact=artifact,
        artifact_root=root,
        created_at_epoch=1000,
    )

    assert row["R"] == 0.0
    assert row["created_at_epoch"] == 1000.0
    assert row["audit_artifact"]["inner_sha256"].islower()
    assert read_spectral_rows(root / "spectral_rows.jsonl", spec, artifact_root=root) == [row]


def test_non_object_inner_artifact_uses_the_validation_error_boundary(tmp_path):
    spec = _spec(tmp_path)
    root = tmp_path / "job"
    artifact = _artifact(root, spec, 4e-6)
    inner = root / artifact["inner_relative_path"]
    inner.write_text("[]", encoding="utf-8")
    artifact["inner_sha256"] = hashlib.sha256(inner.read_bytes()).hexdigest()
    artifact["inner_size_bytes"] = inner.stat().st_size

    with pytest.raises(ValueError, match="audit inner artifact must be an object"):
        append_spectral_row(
            root / "spectral_rows.jsonl",
            spec,
            attempt=1,
            stage_index=0,
            stage_kind="initial_locator",
            requested_wavelength_m=4e-6,
            evaluated_wavelength_m=4e-6,
            frequency_wavelength_m=4e-6,
            R=0.1,
            T=0.05,
            A=0.85,
            mesh_element_count=12,
            mesh_vertex_count=8,
            solve_seconds=0.2,
            audit_artifact=artifact,
            artifact_root=root,
        )


def test_changed_configuration_cannot_reuse_rows(tmp_path):
    spec = _spec(tmp_path)
    root = tmp_path / "job"
    journal = root / "spectral_rows.jsonl"
    _append(journal, root, spec, 4e-6, 0.1)
    changed = deepcopy(spec)
    changed["configuration_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="configuration hash|spec fingerprint"):
        read_spectral_rows(journal, changed, artifact_root=root)


def test_newline_free_oversized_tail_is_repaired_to_empty_journal(tmp_path):
    spec = _spec(tmp_path)
    journal = tmp_path / "spectral_rows.jsonl"
    journal.write_bytes(b"x" * (MAX_SPECTRAL_ROW_BYTES + 1))

    assert read_spectral_rows(journal, spec, artifact_root=tmp_path) == []
    assert journal.read_bytes() == b""


def test_one_ulp_wavelength_variants_share_one_canonical_point_identity(tmp_path):
    spec = _spec(tmp_path)
    exact = spectral_point_identity(spec, 5e-6)
    one_ulp_lower = spectral_point_identity(spec, 4.999999999999999e-6)
    one_ulp_upper = spectral_point_identity(spec, 5.000000000000001e-6)

    assert exact == one_ulp_lower == one_ulp_upper
    assert exact["requested_wavelength_m"] == 5e-6

    root = tmp_path / "job"
    journal = root / "spectral_rows.jsonl"
    artifact = _artifact(root, spec, 5e-6)

    def append_variant(wavelength):
        return append_spectral_row(
            journal,
            spec,
            attempt=1,
            stage_index=0,
            stage_kind="initial_locator",
            requested_wavelength_m=wavelength,
            evaluated_wavelength_m=wavelength,
            frequency_wavelength_m=wavelength,
            R=0.15,
            T=0.05,
            A=0.8,
            mesh_element_count=12,
            mesh_vertex_count=8,
            solve_seconds=0.2,
            audit_artifact=artifact,
            artifact_root=root,
            created_at_epoch=1000.0,
        )

    append_variant(5e-6)
    for variant in (4.999999999999999e-6, 5.000000000000001e-6):
        with pytest.raises(ValueError, match="already exists"):
            append_variant(variant)


def test_concurrent_appends_are_serialized_and_leave_no_lock(tmp_path):
    spec = _spec(tmp_path)
    root = tmp_path / "job"
    journal = root / "spectral_rows.jsonl"
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(
            pool.map(
                lambda item: _append(journal, root, spec, *item),
                [(4e-6, 0.1), (5e-6, 0.8)],
            )
        )

    replayed = read_spectral_rows(journal, spec, artifact_root=root)
    assert {row["row_sha256"] for row in replayed} == {row["row_sha256"] for row in rows}
    assert [row["sequence"] for row in replayed] == [1, 2]
    assert replayed[1]["previous_row_sha256"] == replayed[0]["row_sha256"]
    assert not (root / ".spectral_rows.jsonl.lock").exists()


def test_complete_newline_free_tail_is_retained_before_next_append(tmp_path):
    spec = _spec(tmp_path)
    root = tmp_path / "job"
    journal = root / "spectral_rows.jsonl"
    first = _append(journal, root, spec, 4e-6, 0.1)
    journal.write_bytes(journal.read_bytes().removesuffix(b"\n"))

    second = _append(journal, root, spec, 5e-6, 0.8)

    assert read_spectral_rows(journal, spec, artifact_root=root) == [first, second]
    assert journal.read_bytes().endswith(b"\n")
