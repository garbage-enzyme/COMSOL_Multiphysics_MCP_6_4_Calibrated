"""Deterministic gap proofs for the frozen alpha7 synthetic benchmarks."""

from __future__ import annotations

import copy
import math

import pytest

from development_kit.benchmarks.research_campaign import (
    fake_mim_response,
    frozen_benchmark_suite,
)


def test_suite_freezes_all_required_s0_benchmark_families():
    suite = frozen_benchmark_suite()
    assert {item["kind"] for item in suite["benchmarks"]} == {
        "feasible_hidden_target",
        "impossible_target",
        "multi_objective",
        "material_choice",
        "crash_resume",
    }
    assert suite["mutable_variables"] == ["patch_length_x", "patch_length_y"]
    assert all(item["evaluation_budget"] == 32 for item in suite["benchmarks"])


def test_suite_identity_and_returned_state_are_stable_and_defensive():
    first = frozen_benchmark_suite()
    second = frozen_benchmark_suite()
    assert first == second
    first["bounds"]["patch_length_x"]["lower"] = 0
    assert frozen_benchmark_suite() == second


def test_feasible_target_is_derived_from_hidden_candidate_without_answer_leakage():
    spec = next(
        item
        for item in frozen_benchmark_suite()["benchmarks"]
        if item["kind"] == "feasible_hidden_target"
    )
    response = fake_mim_response(spec["hidden_candidate"])
    assert response["peak_wavelength_nm"] == spec["target"]["peak_wavelength_nm"]
    assert response["q_factor"] == spec["target"]["q_factor"]
    public_spec = copy.deepcopy(spec)
    public_spec.pop("hidden_candidate")
    assert "patch_length_x" not in public_spec["target"]


def test_fake_evaluator_is_deterministic_finite_and_power_closed():
    candidate = {
        "patch_length_x": 100.0,
        "patch_length_y": 80.0,
        "material_state": "gold_reference",
    }
    first = fake_mim_response(candidate)
    assert first == fake_mim_response(candidate)
    assert math.isfinite(first["peak_wavelength_nm"])
    assert math.isfinite(first["q_factor"])
    assert first["power_closure"] == 1.0


@pytest.mark.parametrize(
    "candidate",
    [
        {"patch_length_x": 74.9, "patch_length_y": 80.0, "material_state": "gold_reference"},
        {"patch_length_x": True, "patch_length_y": 80.0, "material_state": "gold_reference"},
        {"patch_length_x": 100.0, "patch_length_y": 80.0, "material_state": "unreviewed"},
    ],
)
def test_fake_evaluator_rejects_out_of_contract_candidates(candidate):
    with pytest.raises(ValueError):
        fake_mim_response(candidate)


def test_material_state_changes_both_observables_but_not_closure():
    baseline = {"patch_length_x": 100.0, "patch_length_y": 80.0}
    reference = fake_mim_response({**baseline, "material_state": "gold_reference"})
    low_loss = fake_mim_response({**baseline, "material_state": "gold_low_loss"})
    assert low_loss["peak_wavelength_nm"] > reference["peak_wavelength_nm"]
    assert low_loss["q_factor"] > reference["q_factor"]
    assert low_loss["power_closure"] == reference["power_closure"] == 1.0


def test_impossible_target_is_outside_the_analytical_response_envelope():
    suite = frozen_benchmark_suite()
    target = next(
        item["target"] for item in suite["benchmarks"] if item["kind"] == "impossible_target"
    )
    sampled = [
        fake_mim_response({"patch_length_x": x, "patch_length_y": y, "material_state": material})
        for x in (75.0, 100.0, 125.0)
        for y in (60.0, 80.0, 100.0)
        for material in ("gold_reference", "gold_low_loss")
    ]
    assert max(row["peak_wavelength_nm"] for row in sampled) < target["peak_wavelength_nm"]
    assert max(row["q_factor"] for row in sampled) < target["q_factor"]
