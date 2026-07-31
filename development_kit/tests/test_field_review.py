from __future__ import annotations

import _winapi
import hashlib
import json
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
import src.jobs.field_review as field_review_module
from src.evidence.contracts import build_physical_evidence
from src.evidence.field_pipeline import build_field_evidence_from_samples
from src.jobs.field_review import assemble_validation_matrix_field_review
from src.jobs.store import atomic_write_json
from src.jobs.validation_collectors import execute_field_evidence_collector
from src.jobs.validation_matrix import normalize_validation_matrix_spec
from src.jobs.validation_rows import append_validation_row, read_validation_rows

from development_kit.tests.test_field_matrix import _field_inputs


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _point(point_id, wavelength, *, grid=None):
    artifact_stem = point_id.replace(":", "-")
    inputs = deepcopy(_field_inputs(source_artifact_id=f"audit-{artifact_stem}"))
    inputs["request_id"] = f"field-{point_id}"
    inputs["view"]["view_id"] = point_id
    inputs["view"]["outputs"] = {
        "array_artifact_id": f"{point_id}-npz",
        "manifest_artifact_id": f"{point_id}-manifest",
    }
    if grid is not None:
        inputs["grid"]["shape"] = list(grid)
        inputs["limits"]["max_grid_points"] = grid[0] * grid[1]
    return {
        "point_id": point_id,
        "configuration_sha256": ("a" if point_id.startswith("off") else "b") * 64,
        "wavelength": {"value": wavelength, "unit": "um", "parameter": "wl"},
        "collectors": [
            {"name": "wave_optics_point_audit", "inputs": {}},
            {"name": "wave_optics_field_evidence", "inputs": inputs},
        ],
        "expected_artifact_ids": [f"audit-{artifact_stem}", f"field-{artifact_stem}"],
    }


def _create_job(tmp_path, *, second_grid=None, mutate_field_manifest=None):
    directory = tmp_path / "job-pair"
    directory.mkdir(parents=True)
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    spec = normalize_validation_matrix_spec(
        {
            "job_type": "validation_matrix",
            "source_model_path": str(source),
            "points": [
                _point("off:res", 5.25),
                _point("target", 5.292, grid=second_grid),
            ],
            "point_limit": 2,
            "cores": 1,
            "resource_policy": {
                "wall_time_budget_seconds": 120,
                "minimum_next_point_seconds": 30,
                "max_mesh_elements": 100000,
            },
        }
    )
    atomic_write_json(directory / "spec.json", spec)

    for point_index, point in enumerate(spec["points"]):
        audit_root = directory / "artifacts" / point["expected_artifact_ids"][0] / "attempt-1"
        audit_root.mkdir(parents=True)
        audit_inner = audit_root / "inner.json"
        physical = build_physical_evidence(
            {
                "schema_name": "comsol_mcp.physical_evidence",
                "schema_version": "1.1.0",
                "artifact_type": "wave_optics_point_audit",
                "producer": {
                    "tool": "wave_optics_point_audit",
                    "tool_schema_version": "test",
                },
                "identity": {
                    "config_id": point["point_fingerprint"],
                    "config_sha256": point["configuration_sha256"],
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
        atomic_write_json(
            audit_inner,
            {
                "audit_status": "policy_evaluated",
                "measurement": {
                    "wavelength": {"requested_m": point["wavelength"]["value"] * 1.0e-6},
                    "solve": {"ran": True, "error": None},
                    "measurement_errors": [],
                    "integrity_errors": [],
                },
                "physical_evidence": physical,
            },
        )
        audit_wrapper = audit_root / "matrix_collector.json"
        atomic_write_json(
            audit_wrapper,
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
                "audit_status": "policy_evaluated",
                "inner_manifest": {
                    "relative_path": audit_inner.relative_to(audit_root).as_posix(),
                    "sha256": _sha256(audit_inner),
                    "size_bytes": audit_inner.stat().st_size,
                },
            },
        )

        field_root = directory / "artifacts" / point["expected_artifact_ids"][1] / "attempt-1"

        def runner(*, request, artifact_root, view_id, **_kwargs):
            x = np.array([-1.0, 1.0, -1.0, 1.0])
            y = np.array([-1.0, -1.0, 1.0, 1.0])
            z = np.full(4, 0.5)
            return build_field_evidence_from_samples(
                request=request,
                view_id=view_id,
                artifact_root=artifact_root,
                coordinates={"x": x, "y": y, "z": z},
                quantities={"abs_ex": x**2 + y**2 + point_index * 10.0},
            )

        field_result = execute_field_evidence_collector(
            point,
            point["collectors"][1],
            field_root,
            model=object(),
            job_id=directory.name,
            expected_source_sha256=spec["source_model_sha256"],
            field_runner=runner,
        )
        field_wrapper = Path(field_result["artifacts"]["manifest"])
        if mutate_field_manifest is not None:
            wrapper = json.loads(field_wrapper.read_text(encoding="utf-8"))
            manifest_path = field_root / wrapper["field_manifest"]["relative_path"]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            mutate_field_manifest(manifest)
            atomic_write_json(manifest_path, manifest)
            wrapper["field_manifest"]["sha256"] = _sha256(manifest_path)
            wrapper["field_manifest"]["byte_count"] = manifest_path.stat().st_size
            atomic_write_json(field_wrapper, wrapper)
        summaries = [
            {
                "collector": "wave_optics_point_audit",
                "artifact_id": point["expected_artifact_ids"][0],
                "audit_status": "policy_evaluated",
                "manifest_relative_path": audit_wrapper.relative_to(directory).as_posix(),
                "manifest_sha256": _sha256(audit_wrapper),
                "manifest_size_bytes": audit_wrapper.stat().st_size,
            },
            {
                "collector": "wave_optics_field_evidence",
                "artifact_id": point["expected_artifact_ids"][1],
                "audit_status": "measurement_complete",
                "manifest_relative_path": field_wrapper.relative_to(directory).as_posix(),
                "manifest_sha256": _sha256(field_wrapper),
                "manifest_size_bytes": field_wrapper.stat().st_size,
            },
        ]
        append_validation_row(
            directory / "matrix_rows.jsonl",
            spec,
            attempt=1,
            point_id=point["point_id"],
            status="ok",
            collector_summaries=summaries,
            created_at_epoch=100.0 + point_index,
        )
    return directory


def test_pair_assembler_verifies_rows_and_renders_shared_scale_bundle(tmp_path):
    directory = _create_job(tmp_path)

    result = assemble_validation_matrix_field_review(
        job_directory=directory,
        point_ids=["off:res", "target"],
        bundle_id="on-off-abs-ex",
        quantity_name="abs_ex",
        quantity_unit="V/m",
        coordinate_unit="um",
    )

    assert result["success"] is True
    assert result["point_count"] == 2
    assert result["plot_process_isolated"] is True
    assert result["visual_review_state"] == "visual_review_required"
    bundle_path = directory / result["bundle_artifact"]["relative_path"]
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert (
        bundle["points"][0]["png_artifact"]["color_limits"]
        == bundle["points"][1]["png_artifact"]["color_limits"]
    )
    assert bundle["shared_color_limits"] == bundle["points"][0]["png_artifact"]["color_limits"]
    assert bundle["artifact_path_base"] == "job_directory"
    for point in bundle["points"]:
        png = directory / point["png_artifact"]["relative_path"]
        assert png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert (directory / point["array_artifact"]["relative_path"]).is_file()
        assert (directory / point["field_manifest"]["relative_path"]).is_file()
        assert (directory / point["source_audit"]["wrapper_relative_path"]).is_file()
        assert (directory / point["source_audit"]["inner_relative_path"]).is_file()
    assert ":" not in bundle["points"][0]["png_artifact"]["relative_path"]


def test_failed_field_runner_cannot_publish_stale_complete_descriptors(tmp_path):
    directory = _create_job(tmp_path)
    spec = json.loads((directory / "spec.json").read_text(encoding="utf-8"))
    point = spec["points"][0]
    artifact_root = tmp_path / "failed-field"

    def failed_runner(*, request, view_id, artifact_root, **_kwargs):
        result = build_field_evidence_from_samples(
            request=request,
            view_id=view_id,
            artifact_root=artifact_root,
            coordinates={
                "x": np.array([-1.0, 1.0, -1.0, 1.0]),
                "y": np.array([-1.0, -1.0, 1.0, 1.0]),
                "z": np.full(4, 0.5),
            },
            quantities={"abs_ex": np.array([2.0, 2.0, 2.0, 2.0])},
        )
        return {**result, "success": False, "error": "runner failed explicitly"}

    result = execute_field_evidence_collector(
        point,
        point["collectors"][1],
        artifact_root,
        model=object(),
        job_id=directory.name,
        expected_source_sha256=spec["source_model_sha256"],
        field_runner=failed_runner,
    )

    assert result["success"] is False
    assert result["error"] == "runner failed explicitly"
    assert not (artifact_root / "matrix_collector.json").exists()


def test_pair_assembler_rejects_junctioned_publication_parent(tmp_path):
    directory = _create_job(tmp_path)
    outside = tmp_path / "outside-review"
    outside.mkdir()
    visual_root = directory / "artifacts" / "visual-review"
    _winapi.CreateJunction(str(outside), str(visual_root))
    try:
        with pytest.raises(ValueError, match="link or junction"):
            assemble_validation_matrix_field_review(
                job_directory=directory,
                point_ids=["off:res", "target"],
                bundle_id="escaped",
                quantity_name="abs_ex",
                quantity_unit="V/m",
                coordinate_unit="um",
            )
        assert list(outside.iterdir()) == []
    finally:
        visual_root.rmdir()


def test_pair_assembler_rejects_tampered_wrapper_before_rendering(tmp_path, monkeypatch):
    directory = _create_job(tmp_path)
    wrapper = next(directory.glob("artifacts/field-off-res/attempt-1/matrix_collector.json"))
    wrapper.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        field_review_module,
        "render_field_png_bundle",
        lambda *_args, **_kwargs: pytest.fail("renderer must not run"),
    )

    with pytest.raises(ValueError, match="differs from the durable row"):
        assemble_validation_matrix_field_review(
            job_directory=directory,
            point_ids=["off:res", "target"],
            bundle_id="tampered",
            quantity_name="abs_ex",
            quantity_unit="V/m",
            coordinate_unit="um",
        )
    assert not (directory / "artifacts" / "visual-review" / "tampered").exists()


def test_pair_assembler_rejects_mismatched_common_grid(tmp_path):
    directory = _create_job(tmp_path, second_grid=(9, 8))

    with pytest.raises(ValueError, match="differ in grid"):
        assemble_validation_matrix_field_review(
            job_directory=directory,
            point_ids=["off:res", "target"],
            bundle_id="grid-mismatch",
            quantity_name="abs_ex",
            quantity_unit="V/m",
            coordinate_unit="um",
        )


def test_pair_assembler_uses_complete_request_bound_manifest_validation(tmp_path):
    def add_noncanonical_claim(manifest):
        manifest["semantic_claim"] = "localized mode"

    directory = _create_job(tmp_path, mutate_field_manifest=add_noncanonical_claim)

    with pytest.raises(ValueError, match="unsupported fields"):
        assemble_validation_matrix_field_review(
            job_directory=directory,
            point_ids=["off:res", "target"],
            bundle_id="noncanonical-manifest",
            quantity_name="abs_ex",
            quantity_unit="V/m",
            coordinate_unit="um",
        )


def test_pair_assembler_rejects_tampered_source_audit_before_rendering(tmp_path, monkeypatch):
    directory = _create_job(tmp_path)
    audit = next(directory.glob("artifacts/audit-off-res/attempt-1/inner.json"))
    audit.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        field_review_module,
        "render_field_png_bundle",
        lambda *_args, **_kwargs: pytest.fail("renderer must not run"),
    )

    with pytest.raises(ValueError, match="inner manifest differs"):
        assemble_validation_matrix_field_review(
            job_directory=directory,
            point_ids=["off:res", "target"],
            bundle_id="audit-tampered",
            quantity_name="abs_ex",
            quantity_unit="V/m",
            coordinate_unit="um",
        )
    assert not (directory / "artifacts" / "visual-review" / "audit-tampered").exists()


def test_pair_assembler_rejects_tampered_spec_fingerprint(tmp_path):
    directory = _create_job(tmp_path)
    spec_path = directory / "spec.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["cores"] = 2
    atomic_write_json(spec_path, spec)

    with pytest.raises(ValueError, match="spec fingerprint"):
        assemble_validation_matrix_field_review(
            job_directory=directory,
            point_ids=["off:res", "target"],
            bundle_id="spec-tampered",
            quantity_name="abs_ex",
            quantity_unit="V/m",
            coordinate_unit="um",
        )


def test_pair_assembler_rejects_windows_device_bundle_name(tmp_path):
    directory = _create_job(tmp_path)

    with pytest.raises(ValueError, match="portable identifier"):
        assemble_validation_matrix_field_review(
            job_directory=directory,
            point_ids=["off:res", "target"],
            bundle_id="CON.json",
            quantity_name="abs_ex",
            quantity_unit="V/m",
            coordinate_unit="um",
        )


def test_pair_assembler_rejects_bundle_ids_the_renderer_cannot_accept(tmp_path):
    directory = _create_job(tmp_path)

    with pytest.raises(ValueError, match="portable identifier"):
        assemble_validation_matrix_field_review(
            job_directory=directory,
            point_ids=["off:res", "target"],
            bundle_id="1-leading-digit",
            quantity_name="abs_ex",
            quantity_unit="V/m",
            coordinate_unit="um",
        )


def test_field_review_proves_npz_shape_against_the_manifest(tmp_path):
    directory = _create_job(tmp_path)
    spec = json.loads((directory / "spec.json").read_text(encoding="utf-8"))
    row = deepcopy(read_validation_rows(directory / "matrix_rows.jsonl", spec)[0])
    field_summary = next(
        item
        for item in row["collector_summaries"]
        if item["collector"] == "wave_optics_field_evidence"
    )
    wrapper_path = directory / field_summary["manifest_relative_path"]
    wrapper = json.loads(wrapper_path.read_text(encoding="utf-8"))
    artifact_root = wrapper_path.parent
    array_path = artifact_root / wrapper["array_artifact"]["relative_path"]
    manifest_path = artifact_root / wrapper["field_manifest"]["relative_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    with np.load(array_path, allow_pickle=False) as archive:
        arrays = {name: np.asarray(archive[name]).copy() for name in archive.files}
    arrays["quantity_abs_ex"] = np.zeros((1, 1), dtype=np.float64)
    np.savez_compressed(array_path, **arrays)

    wrapper["array_artifact"]["sha256"] = _sha256(array_path)
    wrapper["array_artifact"]["byte_count"] = array_path.stat().st_size
    manifest["artifacts"]["array"] = deepcopy(wrapper["array_artifact"])
    manifest["artifact_byte_count"] = wrapper["array_artifact"]["byte_count"]
    manifest_body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(
            manifest_body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    atomic_write_json(manifest_path, manifest)
    wrapper["field_manifest"]["sha256"] = _sha256(manifest_path)
    wrapper["field_manifest"]["byte_count"] = manifest_path.stat().st_size
    atomic_write_json(wrapper_path, wrapper)
    field_summary["manifest_sha256"] = _sha256(wrapper_path)
    field_summary["manifest_size_bytes"] = wrapper_path.stat().st_size

    with pytest.raises(ValueError, match="quantity_abs_ex differs from the manifest grid"):
        field_review_module._load_point_field(directory, spec, row)


def test_pair_assembler_cleans_owned_output_if_bundle_commit_fails(tmp_path, monkeypatch):
    directory = _create_job(tmp_path)
    neighbor = directory / "artifacts" / "visual-review" / "neighbor.txt"
    neighbor.parent.mkdir(parents=True, exist_ok=True)
    neighbor.write_bytes(b"unrelated")

    def fail_bundle_write(*_args, **_kwargs):
        raise OSError("injected bundle write failure")

    monkeypatch.setattr(field_review_module, "atomic_write_json", fail_bundle_write)
    with pytest.raises(OSError, match="injected bundle write failure"):
        assemble_validation_matrix_field_review(
            job_directory=directory,
            point_ids=["off:res", "target"],
            bundle_id="write-failed",
            quantity_name="abs_ex",
            quantity_unit="V/m",
            coordinate_unit="um",
        )
    assert not (directory / "artifacts" / "visual-review" / "write-failed").exists()
    assert neighbor.read_bytes() == b"unrelated"
