"""Failure-atomic fake-backend tests for the trusted native adapter."""

import copy

import pytest

from comsol_mcp.research.adjoint_adapter import configure_native_adjoint
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

    def study_tags(self):
        return sorted(self.studies)

    def create_study(self, tag):
        self.studies[tag] = _Study()
        return self.studies[tag]

    def get_study(self, tag):
        return self.studies[tag]

    def remove_study(self, tag):
        self.studies.pop(tag, None)

    def snapshot(self):
        return copy.deepcopy({tag: study._features.nodes for tag, study in self.studies.items()})

    def restore(self, snapshot):
        self.studies = {}
        for tag, nodes in snapshot.items():
            study = _Study()
            study._features.nodes = copy.deepcopy(nodes)
            self.studies[tag] = study

    def prepare_controls(self, support):
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
    support = _support()
    support["objective"]["expression"] = ""
    with pytest.raises(ValueError):
        configure_native_adjoint(backend, support, _optimizer())
    assert backend.snapshot() == original
