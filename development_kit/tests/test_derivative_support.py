"""Solver-free derivative-support contract tests."""

import copy

import pytest

from comsol_mcp.research.derivative_support import (
    normalize_derivative_support,
    normalize_derivative_variable,
)


def _variable(order: int, variable_id: str = "patch_length_x") -> dict:
    return {
        "variable_id": variable_id,
        "order": order,
        "kind": "continuous",
        "meaning": "patch length",
        "unit": "nm",
        "baseline": 856.0,
        "lower": 642.0,
        "upper": 1070.0,
        "scale": 856.0,
        "mapping": {
            "feature_tag": "blk1",
            "feature_type": "Block",
            "property_name": "size",
            "readback_expression": "patch_length_x",
        },
        "dependency_class": "geometry",
        "step_policy": {
            "relative_steps": [0.01, 0.003, 0.001],
            "absolute_floor": 1e-9,
            "central_difference": True,
            "near_bound_mode": "one_sided",
        },
        "active_bound_semantics": "projected_zero",
    }


def _support() -> dict:
    return {
        "schema_name": "comsol_mcp.derivative_support",
        "schema_version": "1.0.0",
        "contract_id": "periodic-mim-adjoint-v1",
        "comsol_version": "6.4",
        "comsol_build": "6.4.0.293",
        "required_products": ["Wave Optics", "Optimization"],
        "adapter_id": "periodic_mim_patch_v1",
        "adapter_version": "1.0.0",
        "source_identity": "a" * 64,
        "study_identity": "b" * 64,
        "derivative_method": "adjoint",
        "variables": [_variable(0)],
        "objective": {
            "objective_id": "fixed_scalar",
            "expression": "ewfd.Tport1",
            "direction": "maximize",
            "unit": "1",
            "wavelength_um": 1.717657785,
            "study_tag": "std1",
            "solution_tag": "sol1",
            "dataset_tag": "dset1",
            "evidence_paths": ["forward.transmission"],
        },
        "constraints": [],
        "mesh_policy": {
            "topology": "fixed",
            "selection": "preserve",
            "quality_expression": "mesh.minqual",
            "finalist_remesh": True,
        },
        "nondifferentiable_events": ["topology_changed", "branch_switch_unresolved"],
        "result_identity": {
            "study_tag": "std1",
            "solution_tag": "sol1",
            "dataset_tag": "dset1",
            "derivative_expression": "dJ/dpatch_length_x",
            "derivative_units": "1/nm",
        },
        "support_state": "structurally_supported",
    }


def test_support_is_canonical_and_fingerprinted():
    first = normalize_derivative_support(_support())
    reordered = copy.deepcopy(_support())
    reordered["required_products"].reverse()
    assert normalize_derivative_support(reordered) == first
    assert len(first["support_fingerprint"]) == 64
    assert first["variables"][0]["order"] == 0


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["variables"][0].update({"order": 1}),
        lambda value: value["variables"][0].update({"kind": "categorical"}),
        lambda value: value["variables"][0].update({"lower": 856.0}),
        lambda value: value["objective"].update({"dataset_tag": "other"}),
        lambda value: value["mesh_policy"].update({"topology": "adaptive"}),
    ],
)
def test_support_rejects_derivative_identity_or_safety_drift(mutation):
    value = _support()
    mutation(value)
    with pytest.raises(ValueError):
        normalize_derivative_support(value)


def test_variable_rejects_nonfinite_and_invalid_mapping():
    value = _variable(0)
    value["baseline"] = float("nan")
    with pytest.raises(ValueError):
        normalize_derivative_variable(value, index=0)
    value = _variable(0)
    value["mapping"]["property_name"] = ""
    with pytest.raises(ValueError):
        normalize_derivative_variable(value, index=0)


def test_forward_only_constraint_cannot_claim_native_derivative():
    value = _support()
    value["constraints"] = [
        {
            "constraint_id": "power",
            "kind": "forward_only",
            "expression": "R+T+A",
            "unit": "1",
            "lower": 0.99,
            "upper": 1.01,
            "derivative_supported": True,
        }
    ]
    with pytest.raises(ValueError, match="forward_only"):
        normalize_derivative_support(value)
