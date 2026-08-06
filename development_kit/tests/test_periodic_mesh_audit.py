"""Periodic-mesh audit and clone-smoke tests without COMSOL."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from src.tools.periodic_mesh_audit import (
    _mesh_sequence,
    _periodic_groups,
    _recipe_for_group,
    collect_periodic_mesh_audit,
    run_clone_mesh_smoke,
)

from development_kit.tests.test_wave_optics_preflight import FullFakeModel


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _audit(tmp_path, monkeypatch, **fixture):
    source = tmp_path / "periodic.mph"
    source.write_bytes(b"immutable periodic model")
    expected_source_sha256 = _hash(source)
    monkeypatch.setattr(
        "src.tools.wave_optics_preflight.ownership_manager.status",
        lambda **_kwargs: {"collision": False, "session": {"connected": True}},
    )
    model = FullFakeModel(source, **fixture)
    result = collect_periodic_mesh_audit(
        model,
        model_name="ExactModel",
        session_state={"connected": True},
        active_profile="wave_optics",
        expected_source_path=str(source),
        expected_source_sha256=expected_source_sha256,
        loaded_source_identity={
            "source_path": str(source),
            "source_sha256": expected_source_sha256,
            "capture": "test_load",
        },
        expected_component_tag="comp1",
        expected_physics_tag="ewfd",
        expected_study_tag="std1",
        expected_mesh_tag="mesh1",
    )
    assert _hash(source) == expected_source_sha256
    assert result["source"]["source_sha256"] == expected_source_sha256
    return result


def test_valid_rectangular_recipe_is_reported_without_compatibility_overclaim(
    tmp_path, monkeypatch
):
    result = _audit(tmp_path, monkeypatch)

    assert len(result["periodic_groups"]) == 2
    assert result["summary"] == {
        "geometry_consistent": True,
        "mesh_recipe_present": True,
        "built_mesh_observed": True,
        "compatibility_assessment": "compatibility_unproven",
        "node_by_node_mesh_equality": "not_evaluated",
    }
    assert [item["tag"] for item in result["mesh_sequence"]["features_in_execution_order"]] == [
        "ft_x",
        "cp_x",
        "ft_y",
        "cp_y",
        "ftet1",
    ]
    assert all(item["order_verified"] for item in result["group_recipes"])


def test_segmented_oblique_group_reports_balanced_translation_without_axis_guess():
    normal = [-0.2, 1.0, 0.0]
    opposite = [0.2, -1.0, 0.0]
    boundaries = [
        {"boundary": 1, "normal": normal, "center": [0.0, 0.0, 0.25], "interior": False},
        {"boundary": 2, "normal": normal, "center": [0.0, 0.0, 0.75], "interior": False},
        {"boundary": 3, "normal": opposite, "center": [0.2, 1.0, 0.25], "interior": False},
        {"boundary": 4, "normal": opposite, "center": [0.2, 1.0, 0.75], "interior": False},
    ]
    preflight = {
        "topology": {"boundaries": boundaries},
        "periodicity": {
            "floquet_features": [
                {
                    "tag": "fpc_oblique",
                    "type": "PeriodicCondition",
                    "selection": [1, 2, 3, 4],
                    "opposing_face_groups": {"adjacent_domain_signatures_match": True},
                }
            ]
        },
    }

    group = _periodic_groups(preflight)[0]

    assert group["cardinality"] == {
        "source_candidate": 2,
        "destination_candidate": 2,
        "balanced": True,
    }
    assert group["inferred_translation"] == pytest.approx([0.2, 1.0, 0.0])
    assert group["geometry_consistent"] is True
    assert group["source_destination_orientation"] == "not_inferred_from_floquet_selection"


def test_periodic_group_rejects_noncollinear_normals_in_the_same_hemisphere():
    preflight = {
        "topology": {
            "boundaries": [
                {"boundary": 1, "normal": [1, 0, 0], "center": [0, 0, 0], "interior": False},
                {"boundary": 2, "normal": [0.01, 1, 0], "center": [0, 1, 0], "interior": False},
                {"boundary": 3, "normal": [-1, 0, 0], "center": [1, 0, 0], "interior": False},
                {"boundary": 4, "normal": [-0.01, -1, 0], "center": [1, 1, 0], "interior": False},
            ]
        },
        "periodicity": {
            "floquet_features": [
                {"tag": "mixed", "type": "PeriodicCondition", "selection": [1, 2, 3, 4]}
            ]
        },
    }

    group = _periodic_groups(preflight)[0]

    assert group["geometry_consistent"] is False
    assert group["ambiguous_faces"] == [2, 4]


def test_periodic_group_requires_one_to_one_center_translation():
    preflight = {
        "topology": {
            "boundaries": [
                {"boundary": 1, "normal": [1, 0, 0], "center": [0, 0, 0], "interior": False},
                {"boundary": 2, "normal": [1, 0, 0], "center": [0, 2, 0], "interior": False},
                {"boundary": 3, "normal": [-1, 0, 0], "center": [1, 0, 0], "interior": False},
                {"boundary": 4, "normal": [-1, 0, 0], "center": [1, 3, 0], "interior": False},
            ]
        },
        "periodicity": {
            "floquet_features": [
                {"tag": "mismatch", "type": "PeriodicCondition", "selection": [1, 2, 3, 4]}
            ]
        },
    }

    group = _periodic_groups(preflight)[0]

    assert group["inferred_translation"] is None
    assert group["translation_pairing_verified"] is False
    assert group["geometry_consistent"] is False


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ({"mesh_case": "missing_copy_y"}, "add_matching_copyface_source_destination"),
        ({"mesh_case": "wrong_order"}, "place_freetri_for_copyface_source_before_copyface"),
        ({"mismatched_floquet": True}, "repair_periodic_geometry_group_before_meshing"),
    ],
)
def test_smallest_actionable_periodic_mesh_mismatch_is_reported(
    tmp_path, monkeypatch, fixture, expected
):
    result = _audit(tmp_path, monkeypatch, **fixture)
    mismatches = {
        mismatch for item in result["actionable_mismatches"] for mismatch in item["mismatches"]
    }

    assert expected in mismatches
    assert result["summary"]["compatibility_assessment"] == "compatibility_unproven"


def test_unrelated_freetet_does_not_prove_periodic_mesh_recipe(tmp_path, monkeypatch):
    result = _audit(tmp_path, monkeypatch)
    features = [
        {**item, "selection": [99]} if item["tag"] == "ftet1" else item
        for item in result["mesh_sequence"]["features_in_execution_order"]
    ]
    recipe = _recipe_for_group(
        result["periodic_groups"][0],
        features,
    )

    assert recipe["mesh_recipe_present"] is False
    assert recipe["free_tet_coverage_verified"] is False
    assert recipe["actionable_mismatches"] == [
        "place_freetet_covering_periodic_adjacent_domains_after_copyface"
    ]


def test_mesh_feature_truncation_requires_an_observed_extra_item():
    from development_kit.tests.test_wave_optics_preflight import (
        FakeComponent,
        FakeFeature,
        FakeMesh,
    )

    component = FakeComponent()
    exactly = {f"size{index}": FakeFeature(f"size{index}", "Size") for index in range(256)}
    component._mesh.items["mesh1"] = FakeMesh(1, exactly)
    complete, _mesh = _mesh_sequence(component, "mesh1")
    component._mesh.items["mesh1"] = FakeMesh(
        1,
        {**exactly, "late_copy": FakeFeature("late_copy", "CopyFace")},
    )
    truncated, _mesh = _mesh_sequence(component, "mesh1")

    assert complete["feature_count_truncated"] is False
    assert truncated["feature_count_truncated"] is True
    group = {
        "group_id": "fpc1",
        "source_candidate": [1],
        "destination_candidate": [2],
        "adjacent_domains": [1],
        "geometry_consistent": True,
    }
    recipe = _recipe_for_group(
        group,
        truncated["features_in_execution_order"],
        feature_count_truncated=True,
    )
    assert recipe["actionable_mismatches"] == ["inspect_complete_mesh_feature_sequence"]
    assert recipe["order_ambiguous"] is True


class SmokeMesh:
    def __init__(self):
        self.ran = False

    def feature(self):
        return type("Features", (), {"tags": lambda self: []})()

    def run(self):
        self.ran = True

    def getNumElem(self):
        return 250 if self.ran else 0

    def getNumVertex(self):
        return 125 if self.ran else 0


class SmokeComponent:
    def __init__(self, mesh):
        self._mesh = mesh

    def mesh(self):
        component_mesh = self._mesh

        class Meshes:
            def tags(self):
                return ["mesh1"]

            def get(self, _tag):
                return component_mesh

        return Meshes()


class SmokeJava:
    def __init__(self, source, component=None):
        self.source = source
        self._component = component

    def save(self, path, copy=False):
        assert copy is True
        Path(path).write_bytes(self.source.read_bytes())

    def component(self):
        component = self._component

        class Components:
            def tags(self):
                return ["comp1"]

            def get(self, _tag):
                return component

        return Components()


class SmokeModel:
    def __init__(self, source, component=None):
        self._source = source
        self.java = SmokeJava(source, component)

    def file(self):
        return str(self._source)


class SmokeClient:
    def __init__(self, clone):
        self.clone = clone
        self.removed = []

    def load(self, path):
        assert Path(path).is_file()
        return self.clone

    def remove(self, model):
        self.removed.append(model)


def test_clone_only_native_mesh_smoke_preserves_source_and_cleans_artifact(tmp_path):
    source = tmp_path / "source.mph"
    source.write_bytes(b"source bytes")
    mesh = SmokeMesh()
    component = SmokeComponent(mesh)
    clone = SmokeModel(source, component)
    client = SmokeClient(clone)

    result = run_clone_mesh_smoke(
        SmokeModel(source),
        client,
        expected_source_sha256=_hash(source),
        expected_component_tag="comp1",
        expected_mesh_tag="mesh1",
        runtime_dir=tmp_path,
    )

    assert result["success"] is True
    assert result["native_mesh_build"] == "passed"
    assert result["counts"]["element_count"] == 250
    assert result["source_integrity"]["unchanged"] is True
    assert result["cleanup"] == {
        "client_model_removed": True,
        "clone_file_removed": True,
        "clone_dir_removed": True,
    }
    assert client.removed == [clone]
    assert not list(tmp_path.glob("periodic_mesh_smoke_*"))


def test_clone_mesh_smoke_cleans_loaded_clone_after_post_creation_failure(tmp_path):
    class FailingMesh(SmokeMesh):
        def run(self):
            raise RuntimeError("injected mesh failure")

    source = tmp_path / "source.mph"
    source.write_bytes(b"source bytes")
    clone = SmokeModel(source, SmokeComponent(FailingMesh()))
    client = SmokeClient(clone)

    result = run_clone_mesh_smoke(
        SmokeModel(source),
        client,
        expected_source_sha256=_hash(source),
        expected_component_tag="comp1",
        expected_mesh_tag="mesh1",
        runtime_dir=tmp_path,
    )

    assert result["success"] is False
    assert result["native_mesh_build"] == "failed"
    assert result["cleanup"] == {
        "client_model_removed": True,
        "clone_file_removed": True,
        "clone_dir_removed": True,
    }
    assert client.removed == [clone]
    assert not list(tmp_path.glob("periodic_mesh_smoke_*"))


def test_clone_smoke_rejects_wrong_source_hash_before_creating_artifact(tmp_path):
    source = tmp_path / "source.mph"
    source.write_bytes(b"source bytes")
    with pytest.raises(ValueError, match="does not match"):
        run_clone_mesh_smoke(
            SmokeModel(source),
            SmokeClient(None),
            expected_source_sha256="0" * 64,
            runtime_dir=tmp_path,
        )

    assert not list(tmp_path.glob("periodic_mesh_smoke_*"))
