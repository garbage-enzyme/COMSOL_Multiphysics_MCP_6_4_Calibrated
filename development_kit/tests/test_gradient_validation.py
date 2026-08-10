"""Synthetic finite-difference gradient evidence tests."""

import copy

import pytest

from comsol_mcp.research.gradient_contracts import normalize_gradient_record
from comsol_mcp.research.gradient_validation import (
    compare_directional_gradient,
    compare_gradient,
)
from development_kit.tests.test_gradient_contracts import _gradient, normalize_gradient_support


def _finite_difference() -> list[dict]:
    return [
        {
            "variable_id": "patch_length_x",
            "steps": [
                {
                    "relative_step": 0.01,
                    "mode": "central",
                    "plus_objective": 0.8000201,
                    "minus_objective": 0.7999799,
                    "base_objective": None,
                },
                {
                    "relative_step": 0.003,
                    "mode": "central",
                    "plus_objective": 0.800006003,
                    "minus_objective": 0.799993997,
                    "base_objective": None,
                },
                {
                    "relative_step": 0.001,
                    "mode": "central",
                    "plus_objective": 0.8000020001,
                    "minus_objective": 0.7999979999,
                    "base_objective": None,
                },
            ],
        }
    ]


def _policy() -> dict:
    return {
        "relative_error_limit": 0.1,
        "absolute_error_floor": 1e-12,
        "cosine_floor": 0.9,
        "require_sign": True,
    }


def test_gradient_check_records_step_rows_and_passes_synthetic_derivative():
    receipt = compare_gradient(
        _gradient(), normalize_gradient_support(), _finite_difference(), _policy()
    )
    assert receipt["passed"] is True
    assert receipt["variable_order"] == ["patch_length_x"]
    assert receipt["checks"]["cosine_above_floor"] is True
    assert len(receipt["check_fingerprint"]) == 64


@pytest.mark.parametrize(
    "mutation",
    [
        lambda rows: rows[0]["steps"][0].update({"plus_objective": 0.79998}),
        lambda rows: rows[0]["steps"][0].update({"plus_objective": 0.805}),
    ],
)
def test_gradient_check_rejects_sign_order_or_error_failures(mutation):
    rows = copy.deepcopy(_finite_difference())
    mutation(rows)
    receipt = compare_gradient(_gradient(), normalize_gradient_support(), rows, _policy())
    assert receipt["passed"] is False or receipt["checks"]["signs_agree"] is False


def test_gradient_check_rejects_permuted_finite_difference_rows():
    rows = copy.deepcopy(_finite_difference())
    rows[0]["variable_id"] = "patch_length_y"
    with pytest.raises(ValueError, match="variable order"):
        compare_gradient(_gradient(), normalize_gradient_support(), rows, _policy())


def test_gradient_record_rejects_tampered_check_input_identity():
    row = _gradient()
    normalized = normalize_gradient_record(row, normalize_gradient_support())
    row["gradient_fingerprint"] = normalized["gradient_fingerprint"]
    row["native_gradient"] = [0.003]
    with pytest.raises(ValueError, match="fingerprint"):
        normalize_gradient_record(row, normalize_gradient_support())


def test_directional_gradient_check_records_central_prediction():
    receipt = compare_directional_gradient(
        _gradient(),
        normalize_gradient_support(),
        [1.0],
        step=0.01,
        plus_objective=0.80002,
        minus_objective=0.79998,
        policy=_policy(),
    )
    assert receipt["mode"] == "central"
    assert receipt["passed"] is True
    assert len(receipt["directional_check_fingerprint"]) == 64


def test_directional_gradient_check_requires_a_base_for_one_sided_evidence():
    with pytest.raises(ValueError, match="one-sided"):
        compare_directional_gradient(
            _gradient(),
            normalize_gradient_support(),
            [1.0],
            step=0.01,
            plus_objective=0.80002,
            minus_objective=None,
            policy=_policy(),
        )
