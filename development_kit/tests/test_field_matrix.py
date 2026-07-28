from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from src.evidence.field_matrix import bind_validation_matrix_field_request
from src.jobs.validation_collectors import execute_field_evidence_collector
from src.jobs.validation_matrix import normalize_validation_matrix_spec


def _field_inputs(source_artifact_id="audit-target"):
    return {
        "request_id": "matrix-field-target",
        "source_artifact_id": source_artifact_id,
        "expressions": [{"name": "abs_ex", "expression": "abs(ewfd.Ex)", "unit": "V/m"}],
        "view": {
            "view_id": "target",
            "component_tag": "comp1",
            "dataset_name": "研究 1//解 1",
            "dataset_tag": "dset1",
            "solution_tag": "sol1",
            "outputs": {
                "array_artifact_id": "target-field-npz",
                "manifest_artifact_id": "target-field-manifest",
            },
        },
        "slice": {"axis": "z", "value": 0.5, "tolerance": 0.1, "unit": "um"},
        "coordinate_bounds": {
            "x": [-1.0, 1.0],
            "y": [-1.0, 1.0],
            "z": [0.0, 1.0],
            "unit": "um",
        },
        "grid": {"shape": [8, 8], "interpolation": "nearest"},
        "render": {"png": False, "color_scale": "linear", "shared_color_limits": False},
        "limits": {
            "max_raw_points": 1000,
            "max_grid_points": 64,
            "max_artifact_bytes": 1024 * 1024,
            "max_inline_samples": 4,
        },
    }


def _spec(tmp_path, *, collectors=None, artifacts=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    return normalize_validation_matrix_spec(
        {
            "job_type": "validation_matrix",
            "source_model_path": str(source),
            "points": [
                {
                    "point_id": "target",
                    "configuration_sha256": "a" * 64,
                    "wavelength": {"value": 5.292, "unit": "um", "parameter": "wl"},
                    "collectors": collectors
                    or [
                        {"name": "wave_optics_point_audit", "inputs": {}},
                        {"name": "wave_optics_field_evidence", "inputs": _field_inputs()},
                    ],
                    "expected_artifact_ids": artifacts or ["audit-target", "field-target"],
                }
            ],
            "point_limit": 1,
            "cores": 1,
            "resource_policy": {
                "wall_time_budget_seconds": 60,
                "minimum_next_point_seconds": 30,
                "max_mesh_elements": 100000,
            },
        }
    )


def test_matrix_field_template_is_normalized_and_bound_to_exact_point(tmp_path):
    spec = _spec(tmp_path)
    point = spec["points"][0]
    request = bind_validation_matrix_field_request(
        point["collectors"][1]["inputs"],
        job_id="job-123",
        point=point,
        source_model_sha256=spec["source_model_sha256"],
    )

    source = request["views"][0]["source"]
    assert request["configuration_sha256"] == point["configuration_sha256"]
    assert request["views"][0]["wavelength_m"] == pytest.approx(5.292e-6)
    assert source["job_id"] == "job-123"
    assert source["point_fingerprint"] == point["point_fingerprint"]
    assert source["artifact_id"] == "audit-target"
    assert source["source_model_sha256"] == spec["source_model_sha256"]


def test_matrix_field_collector_requires_preceding_point_audit(tmp_path):
    field = {"name": "wave_optics_field_evidence", "inputs": _field_inputs()}
    point_audit = {"name": "wave_optics_point_audit", "inputs": {}}

    with pytest.raises(ValueError, match="must precede"):
        _spec(
            tmp_path / "order",
            collectors=[field, point_audit],
            artifacts=["field-target", "audit-target"],
        )
    with pytest.raises(ValueError, match="not declared"):
        _spec(
            tmp_path / "missing",
            collectors=[point_audit, field],
            artifacts=["other-audit", "field-target"],
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda point: point.pop("configuration_sha256"),
        lambda point: point["collectors"].__setitem__(0, ["not-an-object"]),
        lambda point: point["wavelength"].__setitem__("value", object()),
        lambda point: point["wavelength"].__setitem__("value", 10**10_000),
    ],
    ids=[
        "missing_identity",
        "non_object_collector",
        "non_numeric_wavelength",
        "overflowing_wavelength",
    ],
)
def test_matrix_field_binding_rejects_malformed_points_without_exception_leaks(tmp_path, mutation):
    spec = _spec(tmp_path)
    point = spec["points"][0]
    mutation(point)

    with pytest.raises(ValueError, match="matrix point"):
        bind_validation_matrix_field_request(
            point["collectors"][1]["inputs"],
            job_id="job-123",
            point=point,
            source_model_sha256=spec["source_model_sha256"],
        )


def test_matrix_field_template_rejects_png_and_unknown_fields_before_client(tmp_path):
    png_inputs = _field_inputs()
    png_inputs["render"] = {
        "png": True,
        "color_scale": "linear",
        "shared_color_limits": False,
    }
    png_inputs["view"]["outputs"]["png_artifact_id"] = "target-field-png"
    unknown_inputs = _field_inputs()
    unknown_inputs["python_callback"] = "unsafe"

    for index, (inputs, message) in enumerate(
        ((png_inputs, "does not render PNGs"), (unknown_inputs, "unsupported fields"))
    ):
        with pytest.raises(ValueError, match=message):
            _spec(
                tmp_path / f"invalid-{index}",
                collectors=[
                    {"name": "wave_optics_point_audit", "inputs": {}},
                    {"name": "wave_optics_field_evidence", "inputs": inputs},
                ],
            )


def _fake_field_runner(status="measurement_complete"):
    def run(*, request, artifact_root, **_kwargs):
        root = Path(artifact_root)
        view = request["views"][0]
        directory = root / view["view_fingerprint"]
        directory.mkdir(parents=True)
        array = directory / "field_arrays.npz"
        manifest = directory / "field_manifest.json"
        array.write_bytes(b"bounded-array")
        manifest.write_text(
            json.dumps(
                {
                    "measurement_status": status,
                    "visual_review_state": "visual_review_required",
                    "semantic_mode_label": "not_assigned",
                }
            ),
            encoding="utf-8",
        )

        def descriptor(path, artifact_id):
            return {
                "artifact_id": artifact_id,
                "relative_path": path.relative_to(root).as_posix(),
                "media_type": "application/json" if path.suffix == ".json" else "application/x-npz",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "byte_count": path.stat().st_size,
            }

        return {
            "array_artifact": descriptor(array, view["outputs"]["array_artifact_id"]),
            "manifest_artifact": descriptor(manifest, view["outputs"]["manifest_artifact_id"]),
        }

    return run


def test_matrix_field_collector_wraps_complete_artifacts(tmp_path):
    spec = _spec(tmp_path / "complete")
    point = spec["points"][0]
    collector = point["collectors"][1]
    result = execute_field_evidence_collector(
        point,
        collector,
        tmp_path / "artifact",
        model=object(),
        job_id="job-123",
        expected_source_sha256=spec["source_model_sha256"],
        field_runner=_fake_field_runner(),
    )

    assert result["success"] is True
    assert result["audit_status"] == "measurement_complete"
    wrapper = json.loads(Path(result["artifacts"]["manifest"]).read_text(encoding="utf-8"))
    assert wrapper["job_id"] == "job-123"
    assert wrapper["source_artifact_id"] == "audit-target"
    assert wrapper["visual_review_state"] == "visual_review_required"
    assert wrapper["semantic_mode_label"] == "not_assigned"


def test_partial_matrix_field_artifact_remains_retryable(tmp_path):
    spec = _spec(tmp_path / "partial")
    point = spec["points"][0]
    result = execute_field_evidence_collector(
        point,
        point["collectors"][1],
        tmp_path / "artifact-partial",
        model=object(),
        job_id="job-123",
        expected_source_sha256=spec["source_model_sha256"],
        field_runner=_fake_field_runner("partial"),
    )

    assert result["success"] is False
    assert result["audit_status"] == "partial"
    assert not (tmp_path / "artifact-partial" / "matrix_collector.json").exists()
