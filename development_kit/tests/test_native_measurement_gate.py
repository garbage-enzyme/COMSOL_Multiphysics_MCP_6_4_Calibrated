"""Native measurements keep finite raw values with an explicit scientific gate."""

from __future__ import annotations

import math

from src.jobs.native_condition_receipts import evaluate_scientific_gate


def test_tiny_negative_residuals_stay_measurements():
    gate = evaluate_scientific_gate(-1e-12, 0.4, 0.6)

    assert gate["passed"] is True
    assert gate["range_checks"]["reflectance"] is True
    assert abs(gate["closure_residual"]) <= 1e-4


def test_finite_non_passive_values_fail_the_gate_with_raw_values_kept():
    gate = evaluate_scientific_gate(0.5, 0.6, 0.1)

    assert gate["passed"] is False
    assert abs(gate["closure_residual"] - 0.2) < 1e-12


def test_caller_owned_tolerances_change_the_verdict():
    strict = evaluate_scientific_gate(0.5, 0.6, 0.1, closure_abs_tolerance=1e-6)
    loose = evaluate_scientific_gate(0.5, 0.6, 0.1, closure_abs_tolerance=0.5)
    widened = evaluate_scientific_gate(-1e-6, 0.5, 0.5, range_epsilon=1e-3)

    assert strict["passed"] is False
    assert loose["passed"] is True
    assert widened["passed"] is True


def test_gate_is_deterministic_and_reports_its_contract():
    gate = evaluate_scientific_gate(0.2, 0.7, 0.1)

    assert gate["passed"] is True
    assert gate["range_checks"] == {
        "reflectance": True,
        "transmittance": True,
        "absorption": True,
    }
    assert gate["range_epsilon"] == 1e-9
    assert gate["closure_abs_tolerance"] == 1e-4
    assert abs(gate["closure_residual"]) < 1e-12
    assert math.isfinite(gate["closure_residual"])
