"""Failure-atomic fake-backend tests for the trusted native adapter."""

import copy
import sys
from types import SimpleNamespace

import pytest

from comsol_mcp.research.adjoint_adapter import (
    ClientapiAdjointStudyBackend,
    _deformation_selections,
    configure_native_adjoint,
)
from development_kit.tests.test_derivative_support import _support
from development_kit.tests.test_gradient_contracts import _optimizer


class _Node:
    def __init__(self, node_type: str):
        self.node_type = node_type
        self.values = {}

    def getType(self):
        return self.node_type

    def set(self, name, value):
        self.values[name] = list(value) if isinstance(value, list) else value

    def getString(self, name):
        value = self.values[name]
        if name == "optmethod" and value == "gcmma":
            return "gcmma"
        return str(value)

    def getStringArray(self, name):
        value = self.values[name]
        if name == "punit":
            return ["m" if item in {"nm", "um"} else item for item in value]
        return value


class _FeatureContainer:
    def __init__(self):
        self.nodes = {}

    def create(self, tag, node_type):
        node = _Node(node_type)
        self.nodes[tag] = node
        return node


class _Study:
    def __init__(self):
        self._features = _FeatureContainer()

    def feature(self):
        return self._features


class _Backend:
    def __init__(self):
        self.studies = {}
        self.physics = {"ewfd": {"ltr1": {"selection": [12]}}}
        self.fail_after_prepare = False

    def study_tags(self):
        return sorted(self.studies)

    def create_study(self, tag):
        if self.fail_after_prepare:
            raise RuntimeError("injected post-control failure")
        self.studies[tag] = _Study()
        return self.studies[tag]

    def get_study(self, tag):
        return self.studies[tag]

    def remove_study(self, tag):
        self.studies.pop(tag, None)

    def snapshot(self):
        return copy.deepcopy(
            {
                "studies": {tag: study._features.nodes for tag, study in self.studies.items()},
                "physics": self.physics,
            }
        )

    def restore(self, snapshot):
        self.studies = {}
        for tag, nodes in snapshot["studies"].items():
            study = _Study()
            study._features.nodes = copy.deepcopy(nodes)
            self.studies[tag] = study
        self.physics = copy.deepcopy(snapshot["physics"])

    def prepare_controls(self, support):
        self.physics["dg_a71"] = {
            "free": {"selection": [1, 2, 3]},
            "disp1": {"selection": [1, 2, 3]},
            "patch_a71": {"selection": [10, 11, 12, 13, 14, 15]},
        }
        return {
            "parameters": {
                item["variable_id"]: f"{item['baseline']}[{item['unit']}]"
                for item in support["variables"]
            },
            "patch_size_before": ["8.56e-7", "8.56e-7", "1e-7"],
            "patch_size_readback": ["patch_length_x", "patch_length_y", "1e-7"],
        }


def test_adapter_configures_fixed_nodes_and_canonicalizes_units():
    backend = _Backend()
    receipt = configure_native_adjoint(backend, _support(), _optimizer())
    assert receipt["sensitivity"]["gradientMethod"] == "adjoint"
    assert receipt["sensitivity"]["punit"]["readback"] == ["m"]
    assert receipt["optimization"]["optmethod"]["readback"] == "gcmma"
    assert backend.study_tags() == ["std1", "std2"]


def test_adapter_rolls_back_when_a_fixed_property_fails():
    backend = _Backend()
    original = backend.snapshot()
    backend.fail_after_prepare = True
    with pytest.raises(RuntimeError, match="injected post-control failure"):
        configure_native_adjoint(backend, _support(), _optimizer())
    assert backend.snapshot() == original


def _trusted_boundaries():
    exterior = {1, 2, 3, 4, 5, 7, 8, 9, 16, 17}
    patch = {10, 11, 12, 13, 14, 15}
    return [
        {
            "boundary_number": number,
            "interior": number not in exterior,
            "up_domain": 3 if number in patch else 1,
            "down_domain": 2 if number in patch or number == 6 else 0,
        }
        for number in range(1, 18)
    ]


def test_deformation_selections_accept_exact_trusted_topology():
    assert _deformation_selections(_trusted_boundaries(), 3, 3) == {
        "domains": [1, 2, 3],
        "patch_boundaries": [10, 11, 12, 13, 14, 15],
        "fixed_outer_boundaries": [1, 2, 3, 4, 5, 7, 8, 9, 16, 17],
    }


def test_deformation_selections_reject_changed_domain_count():
    with pytest.raises(ValueError, match="domain topology changed"):
        _deformation_selections(_trusted_boundaries(), 3, 4)


@pytest.mark.parametrize("boundary_number", [10, 15])
def test_deformation_selections_reject_missing_patch_interface(boundary_number):
    boundaries = [
        item for item in _trusted_boundaries() if item["boundary_number"] != boundary_number
    ]
    with pytest.raises(ValueError, match="deformation boundary topology changed"):
        _deformation_selections(boundaries, 3, 3)


def test_deformation_selections_reject_extra_patch_interface():
    boundaries = _trusted_boundaries()
    boundaries[5]["up_domain"] = 3
    with pytest.raises(ValueError, match="deformation boundary topology changed"):
        _deformation_selections(boundaries, 3, 3)


def test_deformation_selections_reject_duplicate_or_overlapping_identity():
    boundaries = _trusted_boundaries()
    boundaries[0]["boundary_number"] = 10
    with pytest.raises(ValueError, match="boundary identities changed"):
        _deformation_selections(boundaries, 3, 3)


class _TaggedContainer:
    def __init__(self, nodes=None):
        self.nodes = dict(nodes or {})

    def tags(self):
        return list(self.nodes)

    def get(self, tag):
        return self.nodes[tag]

    def __call__(self, tag):
        return self.nodes[tag]

    def remove(self, tag):
        del self.nodes[tag]


class _PhysicsInterface:
    def __init__(self, feature_tags):
        self._features = _TaggedContainer({tag: object() for tag in feature_tags})

    def feature(self):
        return self._features


class _Selection:
    def __init__(self, entities=None):
        self._entities = list(entities or [])

    def set(self, entities):
        self._entities = list(entities)

    def entities(self):
        return self._entities


class _PhysicsFeature:
    def __init__(self, feature_type, entities=None):
        self._type = feature_type
        self._selection = _Selection(entities)
        self.values = {"dx": ["", "", ""]}

    def getType(self):
        return self._type

    def selection(self):
        return self._selection

    def setIndex(self, name, value, index):
        self.values.setdefault(name, [None, None, None])[index] = value

    def getStringArray(self, name):
        return self.values[name]


class _DeformationFeatures(_TaggedContainer):
    def create(self, tag, feature_type, _dimension):
        node = _PhysicsFeature(feature_type)
        self.nodes[tag] = node
        return node


class _DeformationInterface:
    def __init__(self):
        self._features = _DeformationFeatures(
            {
                "free": _PhysicsFeature("FreeDeformation"),
                "disp1": _PhysicsFeature("PrescribedMeshDisplacement"),
            }
        )

    def getType(self):
        return "DeformedGeometry"

    def feature(self):
        return self._features


class _PhysicsContainer(_TaggedContainer):
    def create(self, tag, feature_type, geometry_tag):
        assert (feature_type, geometry_tag) == ("DeformedGeometry", "geom1")
        node = _DeformationInterface()
        self.nodes[tag] = node
        return node


class _Block:
    def __init__(self):
        self.size = ["856[nm]", "856[nm]", "100[nm]"]

    def getStringArray(self, name):
        assert name == "size"
        return self.size

    def set(self, name, values):
        assert name == "size"
        self.size = list(values)

    def getType(self):
        return "Block"

    def getValueType(self, name):
        assert name in {"size", "pos"}
        return "double[]"

    def getDoubleArray(self, name):
        if name == "size":
            return [856e-9, 856e-9, 100e-9]
        assert name == "pos"
        return [247e-9, 247e-9, 40e-9]


class _Geometry:
    def __init__(self, block):
        self.block = block
        self.run_count = 0

    def feature(self, tag):
        assert tag == "b_pat"
        return self.block

    def run(self):
        self.run_count += 1


class _Component:
    def __init__(self, physics, geometry):
        self._physics = physics
        self._geometry = geometry

    def physics(self):
        return self._physics

    def geom(self, tag):
        assert tag == "geom1"
        return self._geometry


class _JavaModel:
    def __init__(self, component, studies):
        self._component = component
        self._studies = studies
        self._parameters = _ParameterContainer()

    def component(self, tag):
        assert tag == "comp1"
        return self._component

    def study(self):
        return self._studies

    def param(self):
        return self._parameters


class _ParameterContainer:
    def __init__(self):
        self.values = {"existing": "1"}

    def set(self, name, value):
        self.values[name] = value

    def remove(self, name):
        del self.values[name]


class _ClientapiModel:
    def __init__(self):
        self.block = _Block()
        self.geometry = _Geometry(self.block)
        layered = _PhysicsFeature("LayeredTransitionBoundaryCondition", [12])
        ewfd = _PhysicsInterface([])
        ewfd._features.nodes["ltr1"] = layered
        self.physics = _PhysicsContainer({"ewfd": ewfd})
        self.studies = _TaggedContainer()
        self.java = _JavaModel(_Component(self.physics, self.geometry), self.studies)

    def parameters(self):
        return dict(self.java._parameters.values)


def test_clientapi_backend_restore_removes_new_physics_and_features():
    model = _ClientapiModel()
    backend = ClientapiAdjointStudyBackend(model)
    original = dict(backend.snapshot())
    model.physics.nodes["dg_a71"] = _PhysicsInterface(["free", "disp1", "patch_a71"])
    model.physics.nodes["ewfd"]._features.nodes["unexpected"] = object()
    model.java._parameters.values["patch_length_x"] = "856[nm]"
    model.block.size = ["changed", "856[nm]", "100[nm]"]

    backend.restore(original)

    assert backend.snapshot() == original
    assert model.physics.tags() == ["ewfd"]
    assert model.physics.get("ewfd").feature().tags() == ["ltr1"]
    assert model.geometry.run_count == 1


def _two_variable_support():
    support = _support()
    first = support["variables"][0]
    first["mapping"].update(
        {
            "feature_tag": "patch_a71",
            "feature_type": "PrescribedMeshDisplacement",
            "property_name": "dx",
            "property_index": 0,
        }
    )
    second = copy.deepcopy(first)
    second.update({"variable_id": "patch_length_y", "order": 1})
    second["mapping"].update({"property_index": 1, "readback_expression": "patch_length_y"})
    support["variables"] = [first, second]
    return support


def test_clientapi_prepare_controls_builds_exact_deformed_geometry(monkeypatch):
    model = _ClientapiModel()
    backend = ClientapiAdjointStudyBackend(model)
    monkeypatch.setitem(sys.modules, "jpype", SimpleNamespace(JBoolean=bool))
    monkeypatch.setattr(
        "comsol_mcp.tools.mim_patch._probe_boundaries",
        lambda _geometry: (_trusted_boundaries(), 3, 17, 3),
    )
    monkeypatch.setattr(
        "comsol_mcp.tools.mim_patch._identify_patch_topology",
        lambda *_args, **_kwargs: (3, [12]),
    )

    receipt = backend.prepare_controls(_two_variable_support())

    assert receipt["patch_size_before"] == ["856[nm]", "856[nm]", "100[nm]"]
    assert receipt["patch_size_readback"] == receipt["patch_size_before"]
    assert receipt["deformed_geometry"]["free_domains"] == [1, 2, 3]
    assert receipt["deformed_geometry"]["patch_boundaries"] == [10, 11, 12, 13, 14, 15]
    assert receipt["deformed_geometry"]["fixed_outer_boundaries"] == [
        1,
        2,
        3,
        4,
        5,
        7,
        8,
        9,
        16,
        17,
    ]
    assert "patch_length_x" in receipt["deformed_geometry"]["patch_displacement"][0]
    assert "patch_length_y" in receipt["deformed_geometry"]["patch_displacement"][1]
    assert receipt["deformed_geometry"]["patch_displacement"][2] == "0"
