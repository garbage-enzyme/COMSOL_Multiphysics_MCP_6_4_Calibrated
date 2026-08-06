"""Unit tests for MIM patch helpers without a COMSOL client."""

import json

import pytest
from mcp.server.mcpserver import MCPServer
from src.tools import mim_patch as mim_patch_module
from src.tools.mim_patch import (
    _build_periodic_mesh,
    _find_air_block_tag,
    _identify_patch_topology,
    _identify_side_pairs,
    _list_pair_metadata,
    _normalize_spectral_rows,
    _require_mim_selections,
    _set_copy_face_selections,
    register_mim_patch_tools,
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


def test_air_block_detection_selects_the_largest_candidate_not_the_first_tall_one():
    features = GeometryFeatures()
    features.nodes = {
        "small_tall": GeometryFeature("1e-6, 1e-6, 5e-7"),
        "largest": GeometryFeature("2e-6, 2e-6, 2e-6"),
        "fin": GeometryFeature(""),
    }
    geometry = type("LargestGeometry", (), {"feature": lambda self: features})()

    assert _find_air_block_tag(geometry) == "largest"


def test_equal_largest_air_candidates_require_an_explicit_tag():
    features = GeometryFeatures()
    features.nodes = {
        "first": GeometryFeature("2e-6, 2e-6, 2e-6"),
        "second": GeometryFeature("2e-6, 2e-6, 2e-6"),
    }
    geometry = type("AmbiguousGeometry", (), {"feature": lambda self: features})()

    assert _find_air_block_tag(geometry) is None


def test_air_block_detection_prefers_height_over_volume():
    features = GeometryFeatures()
    features.nodes = {
        "wide_substrate": GeometryFeature("20e-6, 20e-6, 0.5e-6"),
        "air": GeometryFeature("2e-6, 2e-6, 2e-6"),
    }
    geometry = type("HeightGeometry", (), {"feature": lambda self: features})()

    assert _find_air_block_tag(geometry) == "air"


def test_side_pair_classification_requires_cell_coordinates():
    with pytest.raises(ValueError, match="bbox or period"):
        _identify_side_pairs(
            [{"boundary_number": 1, "normal": [1.0, 0.0, 0.0], "center": [0.5, 0, 0]}]
        )


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


def test_patch_topology_is_bound_by_geometry_and_adjacency_not_domain_order():
    boundaries = [
        {
            "boundary_number": 31,
            "up_domain": 17,
            "down_domain": 4,
            "interior": True,
            "center": [0.3e-6, 0.3e-6, 30e-9],
            "normal": [0.0, 0.0, -1.0],
        },
        {
            "boundary_number": 32,
            "up_domain": 9,
            "down_domain": 17,
            "interior": True,
            "center": [0.3e-6, 0.3e-6, 60e-9],
            "normal": [0.0, 0.0, 1.0],
        },
        {
            "boundary_number": 33,
            "up_domain": 9,
            "down_domain": 17,
            "interior": True,
            "center": [0.15e-6, 0.3e-6, 45e-9],
            "normal": [-1.0, 0.0, 0.0],
        },
    ]

    domain, footprint = _identify_patch_topology(
        boundaries,
        [0.3e-6, 0.3e-6, 30e-9],
        [0.15e-6, 0.15e-6, 30e-9],
    )

    assert domain == 17
    assert footprint == [31]


def test_patch_topology_rejects_an_ambiguous_domain_identity():
    boundaries = [
        {
            "boundary_number": 1,
            "up_domain": 3,
            "down_domain": 4,
            "interior": True,
            "center": [0.5, 0.5, 0.0],
            "normal": [0.0, 0.0, -1.0],
        },
        {
            "boundary_number": 2,
            "up_domain": 3,
            "down_domain": 4,
            "interior": True,
            "center": [0.5, 0.5, 1.0],
            "normal": [0.0, 0.0, 1.0],
        },
    ]

    with pytest.raises(ValueError, match="ambiguous domain"):
        _identify_patch_topology(boundaries, [1.0, 1.0, 1.0], [0.0, 0.0, 0.0])


def test_patch_topology_uses_bottom_top_intersection_when_face_counts_tie():
    boundaries = [
        {
            "boundary_number": 1,
            "up_domain": 3,
            "down_domain": 7,
            "interior": True,
            "center": [0.5, 0.5, 0.0],
            "normal": [0.0, 0.0, -1.0],
        },
        {
            "boundary_number": 2,
            "up_domain": 3,
            "down_domain": 9,
            "interior": True,
            "center": [0.5, 0.5, 1.0],
            "normal": [0.0, 0.0, 1.0],
        },
        {
            "boundary_number": 3,
            "up_domain": 7,
            "down_domain": 9,
            "interior": True,
            "center": [0.0, 0.5, 0.5],
            "normal": [-1.0, 0.0, 0.0],
        },
    ]

    domain, footprint = _identify_patch_topology(
        boundaries, [1.0, 1.0, 1.0], [0.0, 0.0, 0.0]
    )

    assert domain == 3
    assert footprint == [1]


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


class UnsupportedCopyFace:
    def selection(self, _name="default"):
        raise RuntimeError("named directed selections unavailable")


def test_copy_face_requires_explicit_source_and_destination_contract():
    with pytest.raises(RuntimeError, match="directed source/destination"):
        _set_copy_face_selections(UnsupportedCopyFace(), [1], [2])


def test_expression_major_spectral_values_are_transposed_to_wavelength_rows():
    rows = _normalize_spectral_rows(
        [
            [0.1, 0.2, 0.3],
            [0.4, 0.5, 0.6],
            [0.5, 0.3, 0.1],
            [4.0e-6, 4.1e-6, 4.2e-6],
        ],
        4,
    )

    assert rows == [
        [0.1, 0.4, 0.5, 4.0e-6],
        [0.2, 0.5, 0.3, 4.1e-6],
        [0.3, 0.6, 0.1, 4.2e-6],
    ]


def test_spectral_emissivity_preserves_transmission_and_prefers_absorptivity(monkeypatch):
    class Model:
        def evaluate(self, expressions):
            values = {
                "ewfd.Rtotal": [0.2],
                "ewfd.Ttotal": [0.3],
                "ewfd.Atotal": [0.5],
                "wl": [4.0e-6],
            }
            return [values[expression] for expression in expressions]

    monkeypatch.setattr(
        mim_patch_module.session_manager,
        "get_model",
        lambda _name: Model(),
    )
    server = MCPServer("mim-emissivity-closure-test")
    register_mim_patch_tools(server)
    tool = server._tool_manager._tools["mim_evaluate_spectral"]

    evaluated = tool.fn(model_name="model")
    derived = tool.fn(
        model_name="model",
        expressions=["ewfd.Rtotal", "ewfd.Ttotal", "wl"],
    )

    assert evaluated["spectral_data"][0]["emissivity"] == 0.5
    assert (
        evaluated["spectral_data"][0]["emissivity_basis"]
        == "evaluated_absorptivity"
    )
    assert derived["spectral_data"][0]["emissivity"] == pytest.approx(0.5)
    assert (
        derived["spectral_data"][0]["emissivity_basis"]
        == "one_minus_reflectance_transmittance"
    )


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
