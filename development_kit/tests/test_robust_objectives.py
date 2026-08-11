"""Differentiable robust absolute-contrast objective tests."""

from __future__ import annotations

import copy

import pytest

from comsol_mcp.research.robust_conditions import normalize_optimization_condition_table
from comsol_mcp.research.robust_objectives import evaluate_robust_absolute_contrast
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
