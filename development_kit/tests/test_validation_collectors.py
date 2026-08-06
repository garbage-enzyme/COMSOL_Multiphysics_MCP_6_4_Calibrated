from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from src.evidence.contracts import build_physical_evidence, validate_physical_evidence
from src.jobs.validation_collectors import execute_physical_audit_collector
from src.jobs.validation_matrix import normalize_validation_matrix_spec


def _normalized_point(tmp_path, collector_name="wave_optics_point_audit", inputs=None):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    spec = normalize_validation_matrix_spec(
        {
            "job_type": "validation_matrix",
            "source_model_path": str(source),
            "points": [
                {
                    "point_id": "target",
                    "configuration_sha256": "a" * 64,
                    "wavelength": {"value": 5.2, "unit": "um", "parameter": "wl"},
                    "incidence": {
                        "theta_degrees": 0.0,
                        "phi_degrees": 0.0,
                        "polarization": "S",
                    },
                    "collectors": [{"name": collector_name, "inputs": inputs or {}}],
                    "expected_artifact_ids": ["target-audit"],
                }
            ],
            "point_limit": 1,
            "cores": 1,
            "resource_policy": {
                "wall_time_budget_seconds": 60,
                "minimum_next_point_seconds": 30,
                "max_mesh_elements": 100_000,
            },
        }
    )
    return spec, spec["points"][0], spec["points"][0]["collectors"][0]


def _complete_runner(
    captured,
    artifact_name="inner.json",
    *,
    producer="wave_optics_point_audit",
):
    def run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        root = Path(kwargs["artifact_dir"])
        manifest = root / artifact_name
        wavelength_m = kwargs["wavelength_value"] * 1.0e-6
        physical = build_physical_evidence(
            {
                "schema_name": "comsol_mcp.physical_evidence",
                "schema_version": "1.1.0",
                "artifact_type": producer,
                "producer": {
                    "tool": producer,
                    "tool_schema_version": "test",
                },
                "identity": {
                    "config_id": kwargs["config_id"],
                    "config_sha256": "a" * 64,
                    "source_sha256": kwargs["expected_source_sha256"],
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
        manifest.write_text(
            json.dumps(
                {
                    "audit_status": "measurement_complete",
                    "measurement": {
                        "wavelength": {"requested_m": wavelength_m},
                        "solve": {"ran": True, "error": None},
                        "measurement_errors": [],
                        "integrity_errors": [],
                    },
                    "physical_evidence": physical,
                }
            ),
            encoding="utf-8",
        )
        return {
            "success": True,
            "audit_status": "measurement_complete",
            "measurement": {"large": "not copied" * 1000},
            "artifacts": {"manifest": str(manifest)},
        }

    return run


def test_point_audit_wrapper_rejects_semantically_unbound_inner_manifest(tmp_path):
    spec, point, collector = _normalized_point(tmp_path)

    with pytest.raises(ValueError, match="point audit inner manifest"):
        execute_physical_audit_collector(
            point,
            collector,
            tmp_path / "artifact",
            model=object(),
            client=object(),
            model_name="fixture",
            expected_source_sha256=spec["source_model_sha256"],
            session_state={"connected": True},
            ownership_preflight={"ready": True},
            point_audit_runner=lambda *_args, **kwargs: _write_unbound_audit(kwargs),
        )


def _write_unbound_audit(kwargs):
    manifest = Path(kwargs["artifact_dir"]) / "inner.json"
    manifest.write_text(json.dumps({"raw": "unbound evidence"}), encoding="utf-8")
    return {
        "success": True,
        "audit_status": "measurement_complete",
        "artifacts": {"manifest": str(manifest)},
    }


def test_point_audit_identity_fields_are_matrix_locked_and_wrapped(tmp_path):
    spec, point, collector = _normalized_point(
        tmp_path,
        inputs={"component_tag": "comp1", "physics_tag": "ewfd"},
    )
    captured = {}
    result = execute_physical_audit_collector(
        point,
        collector,
        tmp_path / "artifact",
        model="MODEL",
        client="CLIENT",
        model_name="fixture",
        expected_source_sha256=spec["source_model_sha256"],
        session_state={"connected": True},
        ownership_preflight={"ready": True},
        point_audit_runner=_complete_runner(captured),
    )

    assert captured["args"] == ("MODEL",)
    kwargs = captured["kwargs"]
    assert kwargs["wavelength_value"] == 5.2
    assert kwargs["wavelength_unit"] == "um"
    assert kwargs["wavelength_parameter"] == "wl"
    assert kwargs["config_id"] == point["point_fingerprint"]
    assert kwargs["expected_source_sha256"] == spec["source_model_sha256"]
    assert kwargs["model_name"] == "fixture"
    assert kwargs["artifact_dir"] == str((tmp_path / "artifact").resolve())
    assert kwargs["session_state"] == {"connected": True}
    assert kwargs["active_profile"] == "wave_optics"
    assert kwargs["ownership_preflight"] == {"ready": True}
    assert "clone_factory" not in kwargs
    assert "clone_register" not in kwargs
    assert "clone_cleanup" not in kwargs
    wrapper = json.loads(Path(result["artifacts"]["manifest"]).read_text(encoding="utf-8"))
    inner_path = tmp_path / "artifact" / wrapper["inner_manifest"]["relative_path"]
    inner = json.loads(inner_path.read_text(encoding="utf-8"))
    physical = validate_physical_evidence(inner["physical_evidence"], verify_hash=True)
    assert physical["identity"]["config_id"] == point["point_fingerprint"]
    assert physical["identity"]["source_sha256"] == spec["source_model_sha256"]
    assert result["success"] is True
    assert wrapper["point"]["incidence"]["polarization_evidence"] == "label_only"
    assert wrapper["point"]["incidence_application"] == "not_mutated_by_collector_adapter"
    assert wrapper["inner_manifest"]["relative_path"] == "inner.json"
    assert "measurement" not in wrapper


def test_relative_inner_manifest_resolves_from_the_assigned_artifact_root(tmp_path):
    spec, point, collector = _normalized_point(tmp_path)
    captured = {}
    real_runner = _complete_runner(captured)

    def relative_runner(*args, **kwargs):
        result = real_runner(*args, **kwargs)
        result["artifacts"]["manifest"] = "inner.json"
        return result

    result = execute_physical_audit_collector(
        point,
        collector,
        tmp_path / "relative-artifact",
        model="MODEL",
        client="CLIENT",
        model_name="fixture",
        expected_source_sha256=spec["source_model_sha256"],
        session_state={"connected": True},
        ownership_preflight={"ready": True},
        point_audit_runner=relative_runner,
    )

    assert Path(result["artifacts"]["manifest"]).is_file()


def test_reference_audit_uses_same_loaded_model_and_client(tmp_path):
    spec, point, collector = _normalized_point(
        tmp_path,
        collector_name="wave_optics_reference_audit",
        inputs={"component_tag": "comp1", "physics_tag": "ewfd"},
    )
    captured = {}
    result = execute_physical_audit_collector(
        point,
        collector,
        tmp_path / "reference",
        model="MODEL",
        client="CLIENT",
        model_name="fixture",
        expected_source_sha256=spec["source_model_sha256"],
        session_state={"connected": True},
        ownership_preflight={"ready": True},
        reference_audit_runner=_complete_runner(captured, producer="wave_optics_reference_audit"),
    )

    assert captured["args"] == ("MODEL", "CLIENT")
    assert result["success"] is True
    wrapper = json.loads(Path(result["artifacts"]["manifest"]).read_text(encoding="utf-8"))
    assert wrapper["schema_name"] == "comsol_mcp.validation_matrix_collector"
    assert wrapper["collector"] == "wave_optics_reference_audit"
    assert wrapper["audit_status"] == "measurement_complete"
    assert wrapper["inner_manifest"]["relative_path"] == "inner.json"
    assert "session_state" not in captured["kwargs"]
    assert "ownership_preflight" not in captured["kwargs"]


def test_reference_audit_rejects_an_inner_manifest_with_wrong_identity(tmp_path):
    spec, point, collector = _normalized_point(
        tmp_path, collector_name="wave_optics_reference_audit"
    )
    runner = _complete_runner({}, producer="wave_optics_reference_audit")

    def wrong_identity(*args, **kwargs):
        result = runner(*args, **kwargs)
        manifest = Path(result["artifacts"]["manifest"])
        document = json.loads(manifest.read_text(encoding="utf-8"))
        physical = document["physical_evidence"]
        physical["identity"]["source_sha256"] = "0" * 64
        body = {key: value for key, value in physical.items() if key != "contract_sha256"}
        physical["contract_sha256"] = hashlib.sha256(
            json.dumps(
                body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        manifest.write_text(json.dumps(document), encoding="utf-8")
        return result

    with pytest.raises(ValueError, match="identity differs"):
        execute_physical_audit_collector(
            point,
            collector,
            tmp_path / "reference-wrong-identity",
            model="MODEL",
            client="CLIENT",
            model_name="fixture",
            expected_source_sha256=spec["source_model_sha256"],
            session_state={"connected": True},
            ownership_preflight={"ready": True},
            reference_audit_runner=wrong_identity,
        )


@pytest.mark.parametrize(
    "locked_field",
    [
        "model_name",
        "wavelength_value",
        "wavelength_unit",
        "wavelength_parameter",
        "expected_source_sha256",
        "config_id",
        "artifact_dir",
        "session_state",
        "active_profile",
        "ownership_preflight",
        "clone_factory",
        "clone_register",
        "clone_cleanup",
    ],
)
def test_caller_cannot_override_matrix_owned_or_cleanup_fields(tmp_path, locked_field):
    spec, point, collector = _normalized_point(tmp_path, inputs={locked_field: "override"})

    with pytest.raises(ValueError, match="override locked fields"):
        execute_physical_audit_collector(
            point,
            collector,
            tmp_path / "artifact",
            model=object(),
            client=object(),
            model_name="fixture",
            expected_source_sha256=spec["source_model_sha256"],
            session_state={"connected": True},
            ownership_preflight={"ready": True},
            point_audit_runner=lambda *_args, **_kwargs: {},
        )


def test_failed_collector_is_returned_without_fabricating_wrapper(tmp_path):
    spec, point, collector = _normalized_point(tmp_path)
    root = tmp_path / "artifact"
    result = execute_physical_audit_collector(
        point,
        collector,
        root,
        model=object(),
        client=object(),
        model_name="fixture",
        expected_source_sha256=spec["source_model_sha256"],
        session_state={"connected": True},
        ownership_preflight={"ready": True},
        point_audit_runner=lambda *_args, **_kwargs: {"success": False, "error": "failed"},
    )

    assert result == {"success": False, "error": "failed"}
    assert not (root / "matrix_collector.json").exists()


def test_inner_manifest_must_remain_inside_assigned_artifact_root(tmp_path):
    spec, point, collector = _normalized_point(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="escapes"):
        execute_physical_audit_collector(
            point,
            collector,
            tmp_path / "artifact",
            model=object(),
            client=object(),
            model_name="fixture",
            expected_source_sha256=spec["source_model_sha256"],
            session_state={"connected": True},
            ownership_preflight={"ready": True},
            point_audit_runner=lambda *_args, **_kwargs: {
                "success": True,
                "audit_status": "measurement_complete",
                "artifacts": {"manifest": str(outside)},
            },
        )


def test_inner_manifest_cannot_claim_the_reserved_wrapper_path(tmp_path):
    spec, point, collector = _normalized_point(tmp_path)

    with pytest.raises(ValueError, match="collides with the reserved wrapper"):
        execute_physical_audit_collector(
            point,
            collector,
            tmp_path / "artifact",
            model=object(),
            client=object(),
            model_name="fixture",
            expected_source_sha256=spec["source_model_sha256"],
            session_state={"connected": True},
            ownership_preflight={"ready": True},
            point_audit_runner=_complete_runner({}, "matrix_collector.json"),
        )
