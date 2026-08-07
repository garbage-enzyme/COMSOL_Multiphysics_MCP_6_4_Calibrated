"""Trusted structure-family manifest and exact live-tree audit tests."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from comsol_mcp.research.adapters import (
    STRUCTURE_ADAPTER_MANIFEST_SCHEMA_NAME,
    STRUCTURE_ADAPTER_MANIFEST_SCHEMA_VERSION,
    STRUCTURE_TREE_AUDIT_SCHEMA_NAME,
    STRUCTURE_TREE_AUDIT_SCHEMA_VERSION,
    ClientapiPeriodicMimPatchBackend,
    adapter_state_sha256,
    apply_periodic_mim_patch_candidate,
    normalize_structure_adapter_manifest,
    normalize_structure_tree_audit,
)


def _manifest() -> dict:
    return {
        "schema_name": STRUCTURE_ADAPTER_MANIFEST_SCHEMA_NAME,
        "schema_version": STRUCTURE_ADAPTER_MANIFEST_SCHEMA_VERSION,
        "adapter_id": "periodic_mim_patch_v1",
        "structure_family": "periodic_mim_patch_v1",
        "source_identity": {
            "source_sha256": "a" * 64,
            "comsol_build": "6.4.0.293",
            "tree_sha256": "b" * 64,
        },
        "component_tag": "comp1",
        "geometry_tag": "geom1",
        "patch_feature": {
            "tag_path": ["wp1", "r1"],
            "feature_types": ["WorkPlane", "Rectangle"],
            "size_property": "size",
            "position_property": "pos",
        },
        "mutable_dimensions": [
            {
                "variable_id": "patch_length_x",
                "property_index": 0,
                "unit": "m",
                "baseline": 300e-9,
                "lower": 225e-9,
                "upper": 375e-9,
            },
            {
                "variable_id": "patch_length_y",
                "property_index": 1,
                "unit": "m",
                "baseline": 300e-9,
                "lower": 225e-9,
                "upper": 375e-9,
            },
        ],
        "patch_center": [300e-9, 300e-9],
        "required_features": [
            {"scope": "geometry", "tag_path": ["wp1"], "feature_type": "WorkPlane"},
            {
                "scope": "geometry",
                "tag_path": ["wp1", "r1"],
                "feature_type": "Rectangle",
            },
            {"scope": "physics", "tag_path": ["ewfd"], "feature_type": "EWFD"},
            {
                "scope": "physics",
                "tag_path": ["ewfd", "ps1"],
                "feature_type": "PeriodicStructure",
            },
            {"scope": "mesh", "tag_path": ["mesh1"], "feature_type": "MeshSequence"},
            {"scope": "study", "tag_path": ["std1"], "feature_type": "Study"},
        ],
        "fixed_contracts": {
            "materials": "c" * 64,
            "incidence": "d" * 64,
            "period": "e" * 64,
            "layer_stack": "f" * 64,
            "topology": "1" * 64,
            "mesh": "2" * 64,
            "study": "3" * 64,
            "evidence": "4" * 64,
        },
        "topology_invariants": {
            "domain_count": 2,
            "boundary_count": 13,
            "x_pair_count": 1,
            "y_pair_count": 1,
            "top_port_count": 1,
            "bottom_port_count": 1,
        },
        "evidence_collectors": ["wave_optics_point_audit", "wave_optics_reference_audit"],
        "review": {
            "status": "accepted",
            "reviewer": "caller",
            "reviewed_at": "2026-08-07T00:00:00Z",
            "baseline_receipt_sha256": "5" * 64,
        },
    }


def _audit(manifest: dict) -> dict:
    normalized = normalize_structure_adapter_manifest(manifest)
    return {
        "schema_name": STRUCTURE_TREE_AUDIT_SCHEMA_NAME,
        "schema_version": STRUCTURE_TREE_AUDIT_SCHEMA_VERSION,
        "manifest_fingerprint": normalized["manifest_fingerprint"],
        "source_identity": normalized["source_identity"],
        "features": normalized["required_features"],
        "fixed_contracts": normalized["fixed_contracts"],
        "topology": normalized["topology_invariants"],
    }


def test_manifest_is_stable_defensive_and_reorders_declared_features():
    first = normalize_structure_adapter_manifest(_manifest())
    reordered = _manifest()
    reordered["required_features"].reverse()
    reordered["mutable_dimensions"].reverse()
    second = normalize_structure_adapter_manifest(reordered)
    assert first == second
    assert first["mutable_dimensions"][0]["variable_id"] == "patch_length_x"
    first["patch_center"][0] = 0
    assert normalize_structure_adapter_manifest(_manifest()) == second


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda value: value["mutable_dimensions"][0].update(lower=224e-9), "25 percent"),
        (lambda value: value["mutable_dimensions"][0].update(variable_id="period"), "trusted"),
        (lambda value: value["mutable_dimensions"][1].update(property_index=0), "size contract"),
        (lambda value: value["review"].update(status="pending"), "reviewed"),
        (lambda value: value["fixed_contracts"].pop("mesh"), "immutable"),
    ],
)
def test_manifest_rejects_scope_expansion_or_incomplete_trust(mutate, match):
    value = _manifest()
    mutate(value)
    with pytest.raises(ValueError, match=match):
        normalize_structure_adapter_manifest(value)


def test_tree_audit_accepts_only_exact_manifest_observation_and_fingerprints_it():
    manifest = normalize_structure_adapter_manifest(_manifest())
    audit = normalize_structure_tree_audit(_audit(manifest), manifest)
    assert audit["accepted"] is True
    assert normalize_structure_tree_audit(audit, manifest) == audit


@pytest.mark.parametrize("field", ["source", "feature", "fixed", "topology"])
def test_tree_audit_rejects_every_observed_drift(field):
    manifest = normalize_structure_adapter_manifest(_manifest())
    audit = _audit(manifest)
    if field == "source":
        audit["source_identity"]["tree_sha256"] = "0" * 64
    elif field == "feature":
        audit["features"][0]["feature_type"] = "Block"
    elif field == "fixed":
        audit["fixed_contracts"]["materials"] = "0" * 64
    else:
        audit["topology"]["boundary_count"] += 1
    with pytest.raises(ValueError, match="does not match"):
        normalize_structure_tree_audit(audit, manifest)


def test_manifest_and_audit_reject_tampered_fingerprints():
    manifest = normalize_structure_adapter_manifest(_manifest())
    tampered_manifest = copy.deepcopy(manifest)
    tampered_manifest["patch_center"][0] += 1e-9
    with pytest.raises(ValueError, match="manifest fingerprint"):
        normalize_structure_adapter_manifest(tampered_manifest)
    audit = normalize_structure_tree_audit(_audit(manifest), manifest)
    audit["audit_fingerprint"] = "0" * 64
    with pytest.raises(ValueError, match="audit fingerprint"):
        normalize_structure_tree_audit(audit, manifest)


class FakeBackend:
    def __init__(self, manifest: dict, fail_phase: str | None = None) -> None:
        self.manifest = normalize_structure_adapter_manifest(manifest)
        size = [item["baseline"] for item in self.manifest["mutable_dimensions"]]
        center = self.manifest["patch_center"]
        self.current = {
            "patch_size": size,
            "patch_position": [center[index] - size[index] / 2.0 for index in range(2)],
            "topology": copy.deepcopy(self.manifest["topology_invariants"]),
            "selection_identity": "6" * 64,
            "mesh_identity": "7" * 64,
        }
        self.source = self.manifest["source_identity"]["source_sha256"]
        self.fail_phase = fail_phase
        self.failed_once = False
        self.dirty_reason = None
        self.calls = []
        self.restoring = False

    def _fail(self, phase: str) -> None:
        if self.fail_phase == phase and not self.failed_once:
            self.failed_once = True
            raise RuntimeError(f"injected {phase}")

    def source_sha256(self) -> str:
        return self.source

    def snapshot(self) -> dict:
        return copy.deepcopy(self.current)

    def apply_patch(self, size: list[float], position: list[float]) -> None:
        self.calls.append("apply_patch")
        self._fail("apply")
        self.current["patch_size"] = list(size)
        self.current["patch_position"] = list(position)

    def rebuild_geometry(self) -> None:
        self.calls.append("geometry")
        self._fail("geometry")

    def reprobe_selections(self) -> dict[str, int]:
        self.calls.append("reprobe")
        self._fail("reprobe")
        if self.fail_phase == "topology" and not self.failed_once:
            self.failed_once = True
            self.current["topology"]["boundary_count"] += 1
        if not self.restoring:
            self.current["selection_identity"] = "8" * 64
        return copy.deepcopy(self.current["topology"])

    def rebuild_mesh(self) -> str:
        self.calls.append("mesh")
        self._fail("mesh")
        if not self.restoring:
            self.current["mesh_identity"] = "9" * 64
        self.restoring = False
        return self.current["mesh_identity"]

    def restore(self, snapshot: dict) -> None:
        self.calls.append("restore")
        if self.fail_phase == "rollback":
            raise RuntimeError("injected rollback")
        self.current = copy.deepcopy(snapshot)
        self.restoring = True

    def mark_dirty(self, reason_code: str) -> None:
        self.dirty_reason = reason_code


def _application_inputs(fail_phase: str | None = None):
    manifest = normalize_structure_adapter_manifest(_manifest())
    backend = FakeBackend(manifest, fail_phase=fail_phase)
    audit = normalize_structure_tree_audit(_audit(manifest), manifest)
    expected = adapter_state_sha256(manifest, backend.snapshot())
    return manifest, backend, audit, expected


def test_atomic_application_rebuilds_reprobes_reads_back_and_preserves_source():
    manifest, backend, audit, expected = _application_inputs()
    result = apply_periodic_mim_patch_candidate(
        backend,
        manifest,
        audit,
        {"patch_length_x": 330e-9, "patch_length_y": 270e-9},
        expected_state_sha256=expected,
    )
    assert result["success"] is True
    assert result["pre_state_sha256"] == expected
    assert result["post_state_sha256"] != expected
    assert backend.current["patch_size"] == [330e-9, 270e-9]
    assert backend.current["patch_position"] == pytest.approx([135e-9, 165e-9], abs=1e-18)
    assert backend.source == manifest["source_identity"]["source_sha256"]
    assert backend.calls == ["apply_patch", "geometry", "reprobe", "mesh"]


@pytest.mark.parametrize(
    "candidate",
    [
        {"patch_length_x": 400e-9, "patch_length_y": 300e-9},
        {"patch_length_x": 300e-9},
        {"patch_length_x": 300e-9, "patch_length_y": True},
    ],
)
def test_application_rejects_invalid_candidate_before_mutation(candidate):
    manifest, backend, audit, expected = _application_inputs()
    with pytest.raises(ValueError):
        apply_periodic_mim_patch_candidate(
            backend, manifest, audit, candidate, expected_state_sha256=expected
        )
    assert backend.calls == []


def test_application_rejects_stale_state_before_mutation():
    manifest, backend, audit, _expected = _application_inputs()
    with pytest.raises(ValueError, match="stale"):
        apply_periodic_mim_patch_candidate(
            backend,
            manifest,
            audit,
            {"patch_length_x": 300e-9, "patch_length_y": 300e-9},
            expected_state_sha256="0" * 64,
        )
    assert backend.calls == []


@pytest.mark.parametrize("phase", ["apply", "geometry", "reprobe", "topology", "mesh"])
def test_each_application_failure_restores_complete_pre_state(phase):
    manifest, backend, audit, expected = _application_inputs(phase)
    before = backend.snapshot()
    result = apply_periodic_mim_patch_candidate(
        backend,
        manifest,
        audit,
        {"patch_length_x": 330e-9, "patch_length_y": 270e-9},
        expected_state_sha256=expected,
    )
    assert result["success"] is False
    assert result["rollback_proved"] is True
    assert result["derived_model_dirty"] is False
    assert backend.snapshot() == before
    assert "restore" in backend.calls


def test_unproved_rollback_marks_derived_model_dirty():
    manifest, backend, audit, expected = _application_inputs("rollback")
    backend.fail_phase = "topology"
    backend.failed_once = False
    original_restore = backend.restore

    def fail_restore(_snapshot):
        backend.fail_phase = "rollback"
        original_restore(_snapshot)

    backend.restore = fail_restore
    result = apply_periodic_mim_patch_candidate(
        backend,
        manifest,
        audit,
        {"patch_length_x": 330e-9, "patch_length_y": 270e-9},
        expected_state_sha256=expected,
    )
    assert result["success"] is False
    assert result["rollback_proved"] is False
    assert result["derived_model_dirty"] is True
    assert backend.dirty_reason == "adapter_rollback_unproved"


class _Container:
    def __init__(self, values):
        self.values = values

    def tags(self):
        return list(self.values)

    def get(self, tag):
        return self.values.get(str(tag))


class _Block:
    def __init__(self):
        self.values = {
            "size": [856e-9, 856e-9, 100e-9],
            "pos": [247e-9, 247e-9, 40e-9],
        }

    def getType(self):
        return "Block"

    def properties(self):
        return list(self.values)

    def getValueType(self, _name):
        return "DoubleArray"

    def getDoubleArray(self, name):
        return list(self.values[name])

    def set(self, name, values):
        self.values[name] = [float(value) for value in values]


class _Sequence:
    def __init__(self, features, *, elements=19808, vertices=3858):
        self.features = _Container(features)
        self.run_count = 0
        self.elements = elements
        self.vertices = vertices

    def feature(self):
        return self.features

    def run(self):
        self.run_count += 1

    def getNumElem(self):
        return self.elements

    def getNumVertex(self):
        return self.vertices


class _Component:
    def __init__(self, geometry, mesh):
        self.geometries = _Container({"geom1": geometry})
        self.meshes = _Container({"mesh1": mesh})

    def geom(self):
        return self.geometries

    def mesh(self):
        return self.meshes


class _Java:
    def __init__(self, component):
        self.components = _Container({"comp1": component})

    def component(self):
        return self.components


class _ClientapiModel:
    def __init__(self, backing: Path):
        self.block = _Block()
        self.geometry = _Sequence({"b_pat": self.block})
        self.mesh = _Sequence({"size": SimpleNamespace(getType=lambda: "Size")})
        self.java = _Java(_Component(self.geometry, self.mesh))
        self.backing = backing

    def file(self):
        return str(self.backing)


def _clientapi_fixture(tmp_path: Path):
    source = tmp_path / "source.mph"
    backing = tmp_path / "derived.mph"
    source.write_bytes(b"immutable source")
    backing.write_bytes(source.read_bytes())
    manifest = _manifest()
    manifest["source_identity"]["source_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest["patch_feature"] = {
        "tag_path": ["b_pat"],
        "feature_types": ["Block"],
        "size_property": "size",
        "position_property": "pos",
    }
    manifest["required_features"] = [
        {"scope": "geometry", "tag_path": ["b_pat"], "feature_type": "Block"},
        {"scope": "physics", "tag_path": ["ewfd"], "feature_type": "EWFD"},
        {"scope": "mesh", "tag_path": ["mesh1"], "feature_type": "MeshSequence"},
        {"scope": "study", "tag_path": ["std1"], "feature_type": "Study"},
    ]
    manifest["mutable_dimensions"][0].update(
        baseline=856e-9, lower=0.75 * 856e-9, upper=1.25 * 856e-9
    )
    manifest["mutable_dimensions"][1].update(
        baseline=856e-9, lower=0.75 * 856e-9, upper=1.25 * 856e-9
    )
    manifest["patch_center"] = [675e-9, 675e-9]
    normalized = normalize_structure_adapter_manifest(manifest)
    model = _ClientapiModel(backing)
    record = SimpleNamespace(
        source_path=str(source),
        backing_path=str(backing),
        dirty=False,
        dirty_reason=None,
    )

    def topology(_model, expected, _size, _position):
        return copy.deepcopy(expected["topology_invariants"]), "8" * 64

    def mesh(_model, _expected):
        return "9" * 64

    backend = ClientapiPeriodicMimPatchBackend(
        model,
        record,
        normalized,
        topology_observer=topology,
        mesh_observer=mesh,
    )
    return normalized, model, record, backend


def test_concrete_clientapi_backend_mutates_only_xy_and_rebuilds(tmp_path: Path):
    manifest, model, record, backend = _clientapi_fixture(tmp_path)
    audit = normalize_structure_tree_audit(_audit(manifest), manifest)
    expected = adapter_state_sha256(manifest, backend.snapshot())

    result = apply_periodic_mim_patch_candidate(
        backend,
        manifest,
        audit,
        {"patch_length_x": 900e-9, "patch_length_y": 700e-9},
        expected_state_sha256=expected,
    )

    assert result["success"] is True
    assert model.block.values["size"] == pytest.approx([900e-9, 700e-9, 100e-9])
    assert model.block.values["pos"] == pytest.approx([225e-9, 325e-9, 40e-9])
    assert model.geometry.run_count == 1
    assert model.mesh.run_count == 1
    assert record.dirty is False
    assert backend.source_sha256() == manifest["source_identity"]["source_sha256"]


def test_concrete_backend_requires_distinct_clean_derived_backing(tmp_path: Path):
    manifest, model, record, _backend = _clientapi_fixture(tmp_path)
    record.backing_path = record.source_path
    model.backing = Path(record.source_path)

    with pytest.raises(ValueError, match="distinct provenance"):
        ClientapiPeriodicMimPatchBackend(model, record, manifest)


def test_concrete_backend_marks_the_derived_record_dirty(tmp_path: Path):
    _manifest_value, _model, record, backend = _clientapi_fixture(tmp_path)

    backend.mark_dirty("adapter_rollback_unproved")

    assert record.dirty is True
    assert record.dirty_reason == "adapter_rollback_unproved"
