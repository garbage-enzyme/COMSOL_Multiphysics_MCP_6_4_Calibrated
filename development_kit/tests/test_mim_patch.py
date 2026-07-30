"""Unit tests for MIM patch helpers without a COMSOL client."""

import json

import pytest
from src.tools.mim_patch import (
    _build_periodic_mesh,
    _find_air_block_tag,
    _list_pair_metadata,
    _require_mim_selections,
)


class JavaStringLike:
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return self.value


class PairNode:
    def label(self):
        return JavaStringLike("Identity Pair 1")


class PairCollection:
    def tags(self):
        return [JavaStringLike("pair1")]

    def get(self, tag):
        assert str(tag) == "pair1"
        return PairNode()


class Component:
    def pair(self):
        return PairCollection()


class GeometryFeature:
    def __init__(self, size):
        self.size = size

    def getString(self, name):
        assert name == "size"
        return JavaStringLike(self.size)


class GeometryFeatures:
    def __init__(self):
        self.nodes = {
            "thin": GeometryFeature("1e-6, 1e-6, 4e-8"),
            "air": GeometryFeature("1e-6, 1e-6, 2e-6"),
            "fin": GeometryFeature(""),
        }

    def tags(self):
        return [JavaStringLike(tag) for tag in self.nodes]

    def get(self, tag):
        return self.nodes[str(tag)]


class Geometry:
    def feature(self):
        return GeometryFeatures()


def test_pair_metadata_normalizes_clientapi_strings_for_json():
    pairs = _list_pair_metadata(Component())

    assert pairs == [{"tag": "pair1", "label": "Identity Pair 1"}]
    assert json.loads(json.dumps(pairs)) == pairs


def test_air_block_detection_returns_python_string_tag():
    tag = _find_air_block_tag(Geometry())

    assert tag == "air"
    assert type(tag) is str


def _side_pairs():
    return {"x_src": [1], "x_dst": [2], "y_src": [3], "y_dst": [4]}


@pytest.mark.parametrize(
    "missing",
    ["patch_footprint", "bottom", "top", "x_src", "x_dst", "y_src", "y_dst"],
)
def test_required_mim_selections_reject_every_missing_build_input(missing):
    values = {
        "patch_footprint": [5],
        "bottom": [6],
        "top": [7],
        **_side_pairs(),
    }
    values[missing] = []

    with pytest.raises(ValueError, match=missing):
        _require_mim_selections(
            values["patch_footprint"],
            values["bottom"],
            values["top"],
            values,
        )


class MeshSelection:
    def __init__(self):
        self.entities = None

    def set(self, entities):
        self.entities = list(entities)


class MeshFeature:
    def __init__(self):
        self.selections = {}

    def selection(self, name="default"):
        return self.selections.setdefault(name, MeshSelection())


class MeshFeatures:
    def __init__(self):
        self.created = []

    def create(self, tag, feature_type):
        feature = MeshFeature()
        self.created.append((tag, feature_type, feature))
        return feature


class MeshNode:
    def __init__(self, *, fail_run=False):
        self.features = MeshFeatures()
        self.fail_run = fail_run
        self.ran = False

    def feature(self):
        return self.features

    def run(self):
        if self.fail_run:
            raise RuntimeError("mesh build failure")
        self.ran = True


class MeshList:
    def __init__(self, *, fail_run=False):
        self.nodes = {"mesh1": object()}
        self.fail_run = fail_run
        self.removed = []

    def tags(self):
        return list(self.nodes)

    def create(self, tag):
        node = MeshNode(fail_run=self.fail_run)
        self.nodes[tag] = node
        return node

    def remove(self, tag):
        self.removed.append(tag)
        del self.nodes[tag]


class MeshComponent:
    def __init__(self, *, fail_run=False):
        self.meshes = MeshList(fail_run=fail_run)

    def mesh(self):
        return self.meshes


def test_periodic_mesh_build_preserves_existing_sequences():
    component = MeshComponent()

    mesh, tag, preserved = _build_periodic_mesh(component, _side_pairs())

    assert tag == "mesh2"
    assert preserved == ["mesh1"]
    assert component.meshes.removed == []
    assert set(component.meshes.nodes) == {"mesh1", "mesh2"}
    assert mesh.ran is True
    assert [item[1] for item in mesh.features.created] == [
        "FreeTri",
        "FreeTri",
        "CopyFace",
        "CopyFace",
        "FreeTet",
    ]


def test_periodic_mesh_failure_removes_only_new_sequence():
    component = MeshComponent(fail_run=True)

    with pytest.raises(RuntimeError, match="mesh build failure"):
        _build_periodic_mesh(component, _side_pairs())

    assert component.meshes.removed == ["mesh2"]
    assert set(component.meshes.nodes) == {"mesh1"}
