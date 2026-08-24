"""Canonical gradient-row and caller-budgeted optimizer contract tests."""

import copy

import pytest

from comsol_mcp.research.gradient_contracts import (
    normalize_gradient_record,
    normalize_native_optimizer_configuration,
)
from development_kit.tests.test_derivative_support import _support


def _gradient() -> dict:
    support = normalize_gradient_support()
    return {
        "schema_name": "comsol_mcp.gradient_record",
        "schema_version": "1.0.0",
        "support_fingerprint": support["support_fingerprint"],
        "candidate_fingerprint": "c" * 64,
        "variable_order": ["patch_length_x"],
        "physical_values": [856.0],
        "objective_value": 0.8,
        "constraint_values": {},
        "native_gradient": [0.002],
        "native_units": ["1/nm"],
        "objective_sign": -1.0,
        "transform_jacobian": [856.0],
        "optimizer_gradient": [-1.712],
        "active_bounds": [False],
        "projected_gradient": [-1.712],
        "method": "adjoint",
        "identities": {
            "primal": "1" * 64,
            "adjoint": "2" * 64,
            "mesh": "3" * 64,
            "study": "4" * 64,
            "solution": "5" * 64,
            "dataset": "6" * 64,
        },
        "suspect_components": [],
        "evidence_state": "native_unchecked",
    }


def normalize_gradient_support():
    from comsol_mcp.research.derivative_support import normalize_derivative_support

    return normalize_derivative_support(_support())


def _optimizer() -> dict:
    return {
        "schema_name": "comsol_mcp.native_optimizer_configuration",
        "schema_version": "1.0.0",
        "optimizer_id": "effect-comparison-candidate",
        "backend": "comsol_native",
        "method": "gcmma",
        "move_limit": 0.1,
        "optimality_tolerance": 1e-3,
        "constraint_tolerance": 1e-3,
        "budget": {
            "cores": 3,
            "max_solves": 20,
            "max_iterations": 10,
            "max_wall_time_seconds": 3600,
            "max_commit_fraction": 0.5,
            "max_disk_bytes": 1 << 30,
            "max_review_items": 20,
        },
        "checkpoint_policy": {
            "every_accepted_iteration": True,
            "save_copy": True,
            "exact_native_resume_required": False,
        },
        "deterministic_seed": 71001,
    }


def _outer_optimizer() -> dict:
    value = _optimizer()
    value.update(
        {
            "schema_version": "1.1.0",
            "backend": "mmapy_outer_comsol_conditions",
            "backend_configuration": {
                "condition_solver_backend": "comsol_native",
                "package_name": "mmapy",
                "package_version": "0.3.1",
                "distribution_license": "GPL-3.0-or-later",
                "distribution_sha256": "4" * 64,
                "distribution_path": "D:\\mcp_tests\\mmapy.whl",
                "objective_direction": "maximize",
                "max_inner_iterations": 15,
            },
        }
    )
    return value


def test_gradient_row_binds_order_sign_scale_and_all_native_identities():
    support = normalize_gradient_support()
    first = normalize_gradient_record(_gradient(), support)
    assert first["variable_order"] == ["patch_length_x"]
    assert first["objective_sign"] == -1.0
    assert len(first["gradient_fingerprint"]) == 64
    assert normalize_gradient_record(first, support) == first


def test_gradient_row_constraint_values_must_reference_support_constraints():
    from comsol_mcp.research.derivative_support import normalize_derivative_support

    # The base fixture declares no constraints: any reported value is foreign.
    row = _gradient()
    row["constraint_values"] = {"undeclared": 1.0}
    with pytest.raises(ValueError, match="derivative-support constraint ids"):
        normalize_gradient_record(row, normalize_gradient_support())

    support_value = _support()
    support_value["constraints"] = [
        {
            "constraint_id": "power",
            "kind": "bound",
            "expression": "R+T+A",
            "unit": "1",
            "lower": 0.99,
            "upper": 1.01,
            "derivative_supported": False,
        }
    ]
    bound_support = normalize_derivative_support(support_value)
    accepted = _gradient()
    accepted["support_fingerprint"] = bound_support["support_fingerprint"]
    accepted["constraint_values"] = {"power": 1.0}
    normalized = normalize_gradient_record(accepted, bound_support)
    assert normalized["constraint_values"] == {"power": 1.0}
    accepted["constraint_values"] = {"power": 1.0, "foreign": 2.0}
    with pytest.raises(ValueError, match="derivative-support constraint ids"):
        normalize_gradient_record(accepted, bound_support)


@pytest.mark.parametrize(
    "field,value",
    [
        ("variable_order", ["patch_length_y"]),
        ("objective_sign", 0.0),
        ("native_gradient", [float("nan")]),
        ("method", "forward"),
        ("active_bounds", [0]),
    ],
)
def test_gradient_row_rejects_permuted_nonfinite_or_stale_semantics(field, value):
    row = _gradient()
    row[field] = value
    with pytest.raises(ValueError):
        normalize_gradient_record(row, normalize_gradient_support())


def test_optimizer_requires_every_caller_budget_and_stable_identity():
    first = normalize_native_optimizer_configuration(_optimizer())
    assert first["budget"]["cores"] == 3
    assert first["budget"]["max_commit_fraction"] == 0.5
    assert normalize_native_optimizer_configuration(first) == first
    missing = _optimizer()
    missing["budget"].pop("cores")
    with pytest.raises(ValueError, match="fields mismatch"):
        normalize_native_optimizer_configuration(missing)


def test_optimizer_rejects_unreviewed_backend_or_unbounded_commit_fraction():
    value = _optimizer()
    value["backend"] = "external"
    with pytest.raises(ValueError, match="comsol_native"):
        normalize_native_optimizer_configuration(value)
    value = copy.deepcopy(_optimizer())
    value["budget"]["max_commit_fraction"] = 1.01
    with pytest.raises(ValueError, match="must not exceed one"):
        normalize_native_optimizer_configuration(value)


def test_untrusted_json_leaves_reject_unhashable_values_as_value_errors():
    # Set-membership checks on untrusted leaves must never leak TypeError for
    # valid-JSON arrays or objects.
    row = _gradient()
    row["evidence_state"] = ["native_unchecked"]
    with pytest.raises(ValueError, match="evidence_state"):
        normalize_gradient_record(row, normalize_gradient_support())

    value = copy.deepcopy(_optimizer())
    value["method"] = {"gcmma": True}
    with pytest.raises(ValueError, match="method is unsupported"):
        normalize_native_optimizer_configuration(value)

    value = copy.deepcopy(_optimizer())
    value["schema_version"] = ["1.0.0"]
    with pytest.raises(ValueError, match="schema identity"):
        normalize_native_optimizer_configuration(value)


def test_outer_gcmma_binds_reviewed_dependency_and_native_condition_solver():
    first = normalize_native_optimizer_configuration(_outer_optimizer())
    assert first["backend"] == "mmapy_outer_comsol_conditions"
    assert first["backend_configuration"]["condition_solver_backend"] == "comsol_native"
    assert normalize_native_optimizer_configuration(first) == first
    changed = _outer_optimizer()
    changed["backend_configuration"]["package_version"] = "0.3.0"
    with pytest.raises(ValueError, match="mmapy 0.3.1"):
        normalize_native_optimizer_configuration(changed)
