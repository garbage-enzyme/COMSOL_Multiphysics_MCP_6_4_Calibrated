"""M3 typed, solver-free PeriodicStructure incidence preview gates."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP

from src.tools import incidence_config
from src.tools.derived_geometry import DerivedGeometryRecord, _DERIVED
from src.tools.incidence_config import preview_incidence, register_incidence_config_tools


class Container:
    def __init__(self, items):
        self.items = items

    def tags(self):
        return list(self.items)

    def get(self, tag):
        return self.items[str(tag)]


class Selection:
    def __init__(self, entities):
        self._entities = entities

    def entities(self):
        return list(self._entities)


class Feature:
    def __init__(self, kind, *, props=None, children=None, entities=()):
        self.kind = kind
        self.props = dict(props or {})
        self.children = Container(children or {})
        self._selection = Selection(entities)

    def getType(self):
        return self.kind

    def label(self):
        return self.kind

    def getString(self, name):
        return self.props[name]

    def feature(self):
        return self.children

    def selection(self):
        return self._selection


class Physics:
    def __init__(self, features):
        self.features = Container(features)

    def feature(self):
        return self.features


class Component:
    def __init__(self, physics):
        self.physics_container = Container(physics)

    def physics(self):
        return self.physics_container


class JavaModel:
    def __init__(self, component):
        self.components = Container({"comp1": component})

    def component(self):
        return self.components


class Model:
    def __init__(self, component, values=None):
        self.java = JavaModel(component)
        self.values = {"theta": 12.5, "phi": -7.0, **(values or {})}
        self.evaluate_calls = []

    def parameters(self, evaluate=False):
        assert evaluate is False
        return {"theta": "12.5[deg]", "phi": "-7[deg]"}

    def evaluate(self, expression, unit=None):
        self.evaluate_calls.append((expression, unit))
        if expression not in self.values:
            raise RuntimeError("unknown expression")
        return self.values[expression]


def fixture(*, periodic_count=1, port_count=2, rdir_entities=(41,), values=None):
    children = {
        f"pport{index + 1}": Feature(
            "PeriodicPort",
            props={"alpha1_inc": "old_theta", "alpha2_inc": "old_phi"},
        )
        for index in range(port_count)
    }
    children["rdir1"] = Feature("ReferenceDirection", entities=rdir_entities)
    parent_props = {
        "Polarization": "LinearPol",
        "LinearPol": "S",
        "alpha1_inc": "old_theta",
        "alpha2_inc": "old_phi",
    }
    features = {
        f"ps{index + 1}": Feature(
            "PeriodicStructure",
            props=parent_props,
            children=children,
        )
        for index in range(periodic_count)
    }
    model = Model(Component({"ewfd": Physics(features)}), values=values)
    record = DerivedGeometryRecord(
        "derived-incidence",
        "clone",
        "source.mph",
        "a" * 64,
        "clone.mph",
        "b" * 64,
    )
    return model, record


def preview(model, record, **overrides):
    arguments = {
        "alpha1_inc": "theta",
        "alpha2_inc": "phi",
        "alpha1_unit": "deg",
        "alpha2_unit": "deg",
        "polarization": "P",
        "physical_polarization_target": "laboratory x-linear",
        "component_tag": "comp1",
        "physics_tag": "ewfd",
    }
    arguments.update(overrides)
    return preview_incidence(model, record, **arguments)


def test_linear_preview_evaluates_parameters_and_is_read_only():
    model, record = fixture()
    result = preview(model, record)

    assert result["mutated"] is False
    assert result["solver_started"] is False
    assert result["evaluated_angles"] == {
        "alpha1_inc": {
            "expression": "theta",
            "expression_kind": "parameter",
            "evaluated_value": 12.5,
            "evaluated_unit": "deg",
        },
        "alpha2_inc": {
            "expression": "phi",
            "expression_kind": "parameter",
            "evaluated_value": -7.0,
            "evaluated_unit": "deg",
        },
    }
    assert result["planned"]["periodic_structure"]["settings"] == {
        "alpha1_inc": "theta",
        "alpha2_inc": "phi",
        "Polarization": "LinearPol",
        "LinearPol": "P",
    }
    assert len(result["planned"]["periodic_ports"]) == 2
    assert result["reference_edge_ids"] == [41]
    assert result["physical_polarization_evidence"] == "label_only"
    assert result["request"]["caller_physical_polarization_target"] == "laboratory x-linear"
    assert len(result["pre_state_sha256"]) == 64


@pytest.mark.parametrize("polarization", ["rhcp", "lhcp"])
def test_circular_preview_uses_verified_comsol_enums(polarization):
    model, record = fixture()
    result = preview(model, record, polarization=polarization)

    assert result["planned"]["periodic_structure"]["settings"] == {
        "alpha1_inc": "theta",
        "alpha2_inc": "phi",
        "Polarization": "CircularPol",
        "CircularPol": polarization,
    }


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"periodic_count": 0}, "found 0"),
        ({"periodic_count": 2}, "found 2"),
        ({"port_count": 1}, "two PeriodicPort"),
        ({"rdir_entities": ()}, "non-empty rdir1"),
    ],
)
def test_missing_ambiguous_and_incomplete_periodic_structure_fail_closed(changes, match):
    model, record = fixture(**changes)
    with pytest.raises(ValueError, match=match):
        preview(model, record)


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"polarization": "TE"}, "S, P, rhcp"),
        ({"alpha1_unit": "grad"}, "deg or rad"),
        ({"physical_polarization_target": ""}, "non-empty"),
    ],
)
def test_unsupported_or_ambiguous_contract_inputs_fail(overrides, match):
    model, record = fixture()
    with pytest.raises(ValueError, match=match):
        preview(model, record, **overrides)


@pytest.mark.parametrize(
    "value,match",
    [
        (float("nan"), "not finite"),
        (1 + 2j, "not real"),
        ([1.0, 2.0], "one scalar"),
    ],
)
def test_angle_evaluation_requires_one_finite_real_scalar(value, match):
    model, record = fixture(values={"theta": value})
    with pytest.raises(ValueError, match=match):
        preview(model, record)


def test_dirty_or_untracked_models_are_rejected_by_public_tool(monkeypatch):
    model, record = fixture()
    server = FastMCP("incidence-preview-test")
    register_incidence_config_tools(server)
    tool = server._tool_manager._tools["wave_optics_incidence_preview"]
    monkeypatch.setattr(incidence_config.session_manager, "get_model", lambda name: model)

    untracked = tool.fn(
        derived_model_id="missing",
        model_name="clone",
        alpha1_inc="theta",
        alpha2_inc="phi",
        polarization="S",
        physical_polarization_target="laboratory y-linear",
    )
    assert untracked["success"] is False
    assert "unknown or mismatched" in untracked["error"]

    record.dirty = True
    record.dirty_reason = "rollback unproven"
    _DERIVED[record.derived_model_id] = record
    try:
        dirty = tool.fn(
            derived_model_id=record.derived_model_id,
            model_name=record.model_name,
            alpha1_inc="theta",
            alpha2_inc="phi",
            polarization="S",
            physical_polarization_target="laboratory y-linear",
        )
        assert dirty["success"] is False
        assert "dirty" in dirty["error"]
    finally:
        _DERIVED.pop(record.derived_model_id, None)
