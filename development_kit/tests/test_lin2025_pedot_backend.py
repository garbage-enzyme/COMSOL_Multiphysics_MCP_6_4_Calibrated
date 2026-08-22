from __future__ import annotations

import copy

import pytest

from comsol_mcp.research.lin2025_pedot_backend import (
    ClientapiLin2025PedotControlBackend,
    prepare_lin2025_pedot_shape_controls,
)
from comsol_mcp.research.lin2025_pedot_cylinder import SCHEMA_NAME
from development_kit.tests.test_derivative_support import _support as base_support


def _fixture() -> dict:
    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": "1.0.0",
        "adapter_id": "lin2025_pedot_cylinder_v1",
        "source_identity": {
            "source_sha256": "a" * 64,
            "tree_sha256": "b" * 64,
            "comsol_build": "6.4.0.293",
        },
        "geometry": {
            "lattice": "hexagonal_primitive",
            "period_um": 1.7,
            "pedot_height_um": 0.2,
            "air_above": True,
            "substrate_n": 1.5,
        },
        "domains": {"substrate": 1, "pedot_cylinder": 5, "air": 4},
        "material_states": [{"state_id": "OX"}, {"state_id": "MR"}],
        "mutable_variables": [
            {"variable_id": "pedot_cylinder_radius_x"},
            {"variable_id": "pedot_cylinder_radius_y"},
        ],
        "temperature_k": 300.0,
    }


def _tree() -> dict:
    return {
        "source_sha256": "a" * 64,
        "domain_map": {"substrate": 1, "pedot_cylinder": 5, "air": 4},
        "domain_z_bounds_um": {"5": [0.0, 0.2]},
        "material_tags": {"pedot_cylinder": "OX"},
        "feature_tags": ["ewfd", "mesh1"],
        "domain_count": 6,
        "boundary_count": 32,
        "exterior_boundaries": [
            1, 2, 3, 4, 5, 7, 8, 10, 11, 13, 14, 15, 16, 17, 29, 30, 31, 32,
        ],
        "pedot_boundaries": [18, 19, 20, 23, 25, 27],
        "pedot_lateral_boundaries": [18, 19, 25, 27],
        "pedot_cap_boundaries": [20, 23],
        "baseline_radius_um": 0.26,
        "center_um": [0.85, 0.0],
    }


def _derivative_support() -> dict:
    value = base_support()
    value["adapter_id"] = "lin2025_pedot_cylinder_v1"
    value["source_identity"] = "a" * 64
    value["contract_id"] = "lin2025-pedot-adjoint-v1"
    first = value["variables"][0]
    first.update(
        variable_id="pedot_cylinder_radius_x",
        baseline=260.0,
        lower=200.0,
        upper=320.0,
        scale=260.0,
    )
    first["mapping"].update(
        feature_tag="pedot_pedot72",
        feature_type="PrescribedMeshDisplacement",
        property_name="dx",
        readback_expression="pedot_cylinder_radius_x",
    )
    second = copy.deepcopy(first)
    second.update(variable_id="pedot_cylinder_radius_y", order=1)
    second["mapping"].update(
        property_index=1, readback_expression="pedot_cylinder_radius_y"
    )
    value["variables"] = [first, second]
    value["objective"]["objective_id"] = "pedot_contrast"
    value["result_identity"]["derivative_expression"] = (
        "real(fsens(pedot_cylinder_radius_x))"
    )
    return value


class _Backend:
    def __init__(self, failure: bool = False):
        self.state = {"nodes": []}
        self.failure = failure

    def snapshot(self):
        return copy.deepcopy(self.state)

    def restore(self, snapshot):
        self.state = copy.deepcopy(snapshot)

    def prepare_controls(self, derivative_support, shape_support):
        self.state["nodes"] = [
            "dg_pedot72",
            "free_pedot72",
            "fix_pedot72",
            "pedot_pedot72",
        ]
        if self.failure:
            raise ValueError("injected preparation failure")
        return {
            "parameters": {
                item["variable_id"]: str(item["baseline"]) + "[" + item["unit"] + "]"
                for item in derivative_support["variables"]
            },
            "circle": {"radius_m": 2.6e-7, "center_m": [8.5e-7, 0.0]},
            "deformed_geometry": {
                "physics_tag": "dg_pedot72",
                "physics_type": "DeformedGeometry",
                "free_domains": shape_support["free_domains"],
                "fixed_boundaries": shape_support["fixed_boundaries"],
                "pedot_boundaries": shape_support["pedot_boundaries"],
                "displacement": ["dx", "dy", "0"],
                "height_preserved": True,
                "center_preserved": True,
            },
        }


def test_pedot_controls_bind_two_radii_and_caps():
    receipt = prepare_lin2025_pedot_shape_controls(
        _Backend(), _fixture(), _tree(), _derivative_support()
    )
    assert receipt["controls"]["deformed_geometry"]["pedot_boundaries"] == [
        18, 19, 20, 23, 25, 27
    ]
    assert receipt["controls"]["deformed_geometry"]["height_preserved"] is True
    assert len(receipt["receipt_fingerprint"]) == 64


def test_pedot_controls_restore_complete_snapshot_on_failure():
    backend = _Backend(failure=True)
    before = backend.snapshot()
    with pytest.raises(ValueError, match="injected preparation failure"):
        prepare_lin2025_pedot_shape_controls(
            backend, _fixture(), _tree(), _derivative_support()
        )
    assert backend.snapshot() == before


def test_pedot_controls_reject_source_identity_drift():
    support = _derivative_support()
    support["source_identity"] = "c" * 64
    with pytest.raises(ValueError, match="source identity"):
        prepare_lin2025_pedot_shape_controls(_Backend(), _fixture(), _tree(), support)


class _FeatureContainer(dict):
    def tags(self):
        return [str(key) for key in self]

    def get(self, tag):
        return dict.__getitem__(self, tag)


class _Circle:
    def getType(self):
        return "Circle"

    def getDouble(self, name):
        assert name == "r"
        return 2.6e-7

    def getDoubleArray(self, name):
        assert name == "pos"
        return [8.5e-7, 0.0]


class _WorkPlane:
    def __init__(self, circles):
        self._circles = _FeatureContainer(circles)

    def getType(self):
        return "WorkPlane"

    def geom(self):
        return self

    def feature(self):
        return self._circles


class _Geometry:
    def __init__(self, features):
        self._features = _FeatureContainer(features)

    def feature(self):
        return self._features


class _Component:
    def __init__(self, geometries, physics):
        self._geometries = _FeatureContainer(geometries)
        self._physics = _FeatureContainer(physics)

    def geom(self):
        return self._geometries

    def physics(self):
        return self._physics


class _StrictParameters:
    def __init__(self):
        self.values = {}
        self.set_calls = 0

    def set(self, name, value):
        self.set_calls += 1
        self.values[name] = value


class _Java:
    def __init__(self, components, parameters):
        self._components = components
        self._parameters = parameters

    def component(self):
        return self._components

    def param(self):
        return self._parameters


class _Model:
    def __init__(self, component, parameters):
        self.java = _Java(_FeatureContainer({"comp1": component}), parameters)

    def parameters(self):
        return {}


def test_prepare_controls_rejects_duplicate_physics_before_parameter_writes():
    geometry = _Geometry({"wp_pedot_cyl": _WorkPlane({"circ_pedot_cyl": _Circle()})})
    component = _Component(
        {"geom1": geometry},
        {
            "ewfd": object(),
            "dg_pedot72": object(),
        },
    )
    parameters = _StrictParameters()
    backend = ClientapiLin2025PedotControlBackend(
        _Model(component, parameters), component_tag="comp1", geometry_tag="geom1"
    )
    shape_support = {
        "baseline_radius_um": 0.26,
        "center_um": [0.85, 0.0],
        "free_domains": [5],
        "fixed_boundaries": [20, 23],
        "pedot_boundaries": [18, 19, 25, 27],
    }
    with pytest.raises(ValueError, match="deformation interface already exists"):
        backend.prepare_controls(_derivative_support(), shape_support)
    # The precondition must fail closed before the first model mutation.
    assert parameters.set_calls == 0
    assert parameters.values == {}
