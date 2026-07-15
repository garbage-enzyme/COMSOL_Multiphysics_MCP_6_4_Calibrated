from __future__ import annotations

import builtins

import pytest

from src.jobs.validation_matrix import (
    MAX_VALIDATION_MATRIX_POINTS,
    normalize_validation_matrix_spec,
)


def _point(point_id: str = "off-resonance", wavelength: float = 5.1) -> dict:
    return {
        "point_id": point_id,
        "configuration_sha256": "a" * 64,
        "wavelength": {"value": wavelength, "unit": "um", "parameter": "wl"},
        "incidence": {
            "theta_degrees": 0.0,
            "phi_degrees": 0.0,
            "polarization": "S",
        },
        "collectors": [
            {
                "name": "wave_optics_point_audit",
                "inputs": {"component_tag": "comp1", "physics_tag": "ewfd"},
            }
        ],
        "expected_artifact_ids": [f"audit-{point_id}"],
    }


def _spec(source, points=None, **overrides) -> dict:
    value = {
        "job_type": "validation_matrix",
        "source_model_path": str(source),
        "points": points or [_point()],
        "point_limit": 2,
        "cores": 1,
        "resource_policy": {
            "wall_time_budget_seconds": 120,
            "minimum_next_point_seconds": 30,
            "max_mesh_elements": 100_000,
        },
    }
    value.update(overrides)
    return value


def test_normalization_is_solver_free_and_binds_exact_point_identity(tmp_path, monkeypatch):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"controlled fixture")
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "mph" or name.startswith("mph."):
            raise AssertionError("normalization must not import mph")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    first = normalize_validation_matrix_spec(_spec(source))
    second = normalize_validation_matrix_spec(_spec(source))

    assert first == second
    assert first["source_model_sha256"] != first["points"][0]["point_fingerprint"]
    assert len(first["source_model_sha256"]) == 64
    assert len(first["points"][0]["point_fingerprint"]) == 64
    assert len(first["spec_fingerprint"]) == 64
    assert first["points"][0]["incidence"]["polarization_evidence"] == "label_only"
    assert first["resource_policy"]["host_defaults_applied"] is False


def test_source_or_point_changes_change_immutable_fingerprints(tmp_path):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"v1")
    first = normalize_validation_matrix_spec(_spec(source))
    changed_point = normalize_validation_matrix_spec(
        _spec(source, points=[_point(wavelength=5.2)])
    )
    source.write_bytes(b"v2")
    changed_source = normalize_validation_matrix_spec(_spec(source))

    assert first["points"][0]["point_fingerprint"] != changed_point["points"][0]["point_fingerprint"]
    assert first["source_model_sha256"] != changed_source["source_model_sha256"]
    assert first["spec_fingerprint"] != changed_source["spec_fingerprint"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda spec: spec.update(point_limit=0), "point_limit must be a positive integer"),
        (
            lambda spec: spec.update(point_limit=MAX_VALIDATION_MATRIX_POINTS + 1),
            "point_limit must not exceed",
        ),
        (
            lambda spec: spec.update(points=[_point("one"), _point("two")], point_limit=1),
            "points exceed the caller-declared point_limit",
        ),
        (
            lambda spec: spec.update(
                resource_policy={
                    "wall_time_budget_seconds": 20,
                    "minimum_next_point_seconds": 30,
                    "max_mesh_elements": 100_000,
                }
            ),
            "minimum_next_point_seconds must not exceed wall_time_budget_seconds",
        ),
        (
            lambda spec: spec.update(
                points=[_point("one"), _point("two", 5.2)],
                resource_policy={
                    "wall_time_budget_seconds": 50,
                    "minimum_next_point_seconds": 30,
                    "max_mesh_elements": 100_000,
                },
            ),
            "points exceed the caller-declared wall-time budget",
        ),
        (
            lambda spec: spec.update(
                resource_policy={
                    "wall_time_budget_seconds": 120,
                    "minimum_next_point_seconds": 30,
                }
            ),
            "at least one non-wall resource limit",
        ),
    ],
)
def test_declared_point_time_and_resource_bounds_fail_closed_before_runtime(
    tmp_path, mutation, message
):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    spec = _spec(source)
    mutation(spec)

    with pytest.raises(ValueError, match=message):
        normalize_validation_matrix_spec(spec)


def test_duplicate_exact_points_and_artifact_ids_are_rejected(tmp_path):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    duplicate_identity = _point("second")
    duplicate_artifact = _point("second", 5.2)
    duplicate_artifact["expected_artifact_ids"] = ["audit-off-resonance"]

    with pytest.raises(ValueError, match="unique exact configuration identities"):
        normalize_validation_matrix_spec(
            _spec(source, points=[_point(), duplicate_identity])
        )
    with pytest.raises(ValueError, match="unique across the matrix"):
        normalize_validation_matrix_spec(
            _spec(source, points=[_point(), duplicate_artifact])
        )


def test_collector_inputs_are_finite_bounded_json(tmp_path):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    nonfinite = _point()
    nonfinite["collectors"][0]["inputs"]["bad"] = float("nan")
    oversized = _point()
    oversized["collectors"][0]["inputs"]["payload"] = "x" * (64 * 1024)

    with pytest.raises(ValueError, match="finite JSON"):
        normalize_validation_matrix_spec(_spec(source, points=[nonfinite]))
    with pytest.raises(ValueError, match="collector-input limit"):
        normalize_validation_matrix_spec(_spec(source, points=[oversized]))


def test_only_declared_collectors_and_portable_artifact_ids_are_accepted(tmp_path):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    unsupported = _point()
    unsupported["collectors"][0]["name"] = "arbitrary_python"
    unsafe_artifact = _point()
    unsafe_artifact["expected_artifact_ids"] = ["C:\\private\\result.npz"]

    with pytest.raises(ValueError, match="not a supported validation collector"):
        normalize_validation_matrix_spec(_spec(source, points=[unsupported]))
    with pytest.raises(ValueError, match="bounded portable identifier"):
        normalize_validation_matrix_spec(_spec(source, points=[unsafe_artifact]))


def test_each_collector_requires_one_unique_expected_artifact_id(tmp_path):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    point = _point()
    point["expected_artifact_ids"] = ["first", "extra"]

    with pytest.raises(ValueError, match="exactly one expected artifact ID per collector"):
        normalize_validation_matrix_spec(_spec(source, points=[point]))
