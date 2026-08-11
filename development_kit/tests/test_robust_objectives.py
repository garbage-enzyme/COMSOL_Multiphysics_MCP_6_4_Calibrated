"""Differentiable robust absolute-contrast objective tests."""

from __future__ import annotations

import copy

import pytest

from comsol_mcp.research.robust_conditions import normalize_optimization_condition_table
from comsol_mcp.research.robust_objectives import (
    aggregate_robust_absolute_contrast_gradient,
    evaluate_robust_absolute_contrast,
)
from development_kit.tests.test_robust_conditions import _table


def _configuration(state_ids: list[str] | None = None) -> dict:
    return {
        "schema_name": "comsol_mcp.robust_objective_configuration",
        "schema_version": "1.0.0",
        "objective_id": "pedot-absolute-transmission-contrast",
        "kind": "smooth_worst_case_absolute_contrast",
        "direction": "maximize",
        "state_ids": state_ids or ["OX", "MR"],
        "observable_id": "transmission_order_0_0",
        "absolute_smoothing_epsilon": 1.0e-6,
        "worst_case_temperature": 0.01,
    }


def _observations() -> list[dict]:
    table = normalize_optimization_condition_table(_table())
    values = []
    for row in table["conditions"]:
        base = 0.7 - row["order"] * 0.001
        value = base if row["material_state_id"] == "OX" else base - 0.2
        values.append(
            {
                "condition_id": row["condition_id"],
                "observable_id": row["observable_id"],
                "value": value,
                "evidence_sha256": f"{row['order'] % 10}" * 64,
                "disposition": "measured",
            }
        )
    return values


def _gradients() -> list[dict]:
    table = normalize_optimization_condition_table(_table())
    return [
        {
            "condition_id": row["condition_id"],
            "variable_ids": ["patch_length_x", "patch_length_y"],
            "values": [
                0.5 + row["order"] * 0.01,
                -0.2 + row["order"] * 0.005,
            ],
            "evidence_sha256": f"{(row['order'] + 3) % 10}" * 64,
            "disposition": "measured",
        }
        for row in table["conditions"]
    ]


def test_24_conditions_form_12_complete_symmetric_contrast_pairs():
    receipt = evaluate_robust_absolute_contrast(_configuration(), _table(), _observations())
    assert receipt["pair_count"] == 12
    assert receipt["complete"] is True
    assert receipt["minimum_smooth_absolute_contrast"] == pytest.approx(0.20100000000248758)
    assert sum(item["smooth_worst_case_weight"] for item in receipt["pairs"]) == pytest.approx(1.0)
    assert all(item["smooth_absolute_derivative"] > 0.0 for item in receipt["pairs"])


def test_state_order_changes_derivative_sign_but_not_absolute_objective():
    forward = evaluate_robust_absolute_contrast(_configuration(), _table(), _observations())
    reversed_states = evaluate_robust_absolute_contrast(
        _configuration(["MR", "OX"]), _table(), _observations()
    )
    assert reversed_states["smooth_worst_case_absolute_contrast"] == pytest.approx(
        forward["smooth_worst_case_absolute_contrast"]
    )
    assert reversed_states["pairs"][0]["smooth_absolute_derivative"] == pytest.approx(
        -forward["pairs"][0]["smooth_absolute_derivative"]
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda observations: observations.pop(), "exactly cover"),
        (lambda observations: observations[0].update(disposition="failed"), "unmeasured"),
        (
            lambda observations: observations[0].update(observable_id="reflectance"),
            "observation observable",
        ),
        (lambda observations: observations[0].update(value=float("nan")), "finite"),
    ],
)
def test_failed_missing_mismatched_or_nonfinite_conditions_cannot_disappear(mutation, message):
    observations = _observations()
    mutation(observations)
    with pytest.raises(ValueError, match=message):
        evaluate_robust_absolute_contrast(_configuration(), _table(), observations)


def test_pair_weights_must_match_and_configuration_fingerprint_is_immutable():
    table = _table()
    table["conditions"][0]["weight"] = 2.0
    with pytest.raises(ValueError, match="same weight"):
        evaluate_robust_absolute_contrast(_configuration(), table, _observations())
    receipt = evaluate_robust_absolute_contrast(_configuration(), _table(), _observations())
    config = _configuration()
    from comsol_mcp.research.robust_objectives import normalize_robust_objective_configuration

    normalized = normalize_robust_objective_configuration(config)
    tampered = copy.deepcopy(normalized)
    tampered["worst_case_temperature"] = 0.02
    with pytest.raises(ValueError, match="fingerprint"):
        normalize_robust_objective_configuration(tampered)
    assert len(receipt["receipt_fingerprint"]) == 64


def test_aggregate_gradient_matches_independent_objective_finite_difference():
    observations = _observations()
    gradients = _gradients()
    receipt = aggregate_robust_absolute_contrast_gradient(
        _configuration(), _table(), observations, gradients
    )
    step = 1e-7
    for variable_index, expected in enumerate(receipt["aggregate_gradient"]):
        plus = copy.deepcopy(observations)
        minus = copy.deepcopy(observations)
        by_condition = {item["condition_id"]: item for item in gradients}
        for target in plus:
            target["value"] += step * by_condition[target["condition_id"]]["values"][variable_index]
        for target in minus:
            target["value"] -= step * by_condition[target["condition_id"]]["values"][variable_index]
        plus_value = evaluate_robust_absolute_contrast(_configuration(), _table(), plus)[
            "smooth_worst_case_absolute_contrast"
        ]
        minus_value = evaluate_robust_absolute_contrast(_configuration(), _table(), minus)[
            "smooth_worst_case_absolute_contrast"
        ]
        finite_difference = (plus_value - minus_value) / (2.0 * step)
        assert expected == pytest.approx(finite_difference, rel=1e-7, abs=1e-9)


def test_state_order_does_not_change_the_physical_aggregate_gradient():
    forward = aggregate_robust_absolute_contrast_gradient(
        _configuration(), _table(), _observations(), _gradients()
    )
    reversed_states = aggregate_robust_absolute_contrast_gradient(
        _configuration(["MR", "OX"]), _table(), _observations(), _gradients()
    )
    assert reversed_states["aggregate_gradient"] == pytest.approx(forward["aggregate_gradient"])


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda rows: rows.pop(), "exactly cover"),
        (lambda rows: rows[0].update(disposition="failed"), "unmeasured"),
        (lambda rows: rows[1]["variable_ids"].reverse(), "variable order"),
        (lambda rows: rows[0]["values"].pop(), "match variable_ids"),
    ],
)
def test_aggregate_gradient_rejects_incomplete_or_inconsistent_condition_evidence(
    mutation, message
):
    gradients = _gradients()
    mutation(gradients)
    with pytest.raises(ValueError, match=message):
        aggregate_robust_absolute_contrast_gradient(
            _configuration(), _table(), _observations(), gradients
        )
