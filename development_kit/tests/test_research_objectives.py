"""Deterministic objective scoring from exact evidence pointers."""

from __future__ import annotations

import copy

import pytest

from comsol_mcp.research.objectives import score_objectives
from development_kit.tests.test_research_contracts import _goal


def _evidence() -> dict:
    return {
        "response": {"peak_wavelength": 1554.0, "q_factor": 19.5},
        "provenance": {"artifact": "synthetic"},
    }


def _pointers() -> list[dict]:
    return [
        {"objective_id": "peak", "path": ["response", "peak_wavelength"]},
        {"objective_id": "q", "path": ["response", "q_factor"]},
    ]


def test_peak_and_q_scoring_cites_exact_paths_and_preserves_evidence():
    evidence = _evidence()
    original = copy.deepcopy(evidence)
    receipt = score_objectives(_goal(), evidence, _pointers())
    assert evidence == original
    scores = {item["objective_id"]: item for item in receipt["scores"]}
    assert scores["peak"]["observed_value"] == 1554.0
    assert scores["peak"]["normalized_loss"] == 4.0
    assert scores["peak"]["evidence_path"] == ["response", "peak_wavelength"]
    assert scores["q"]["normalized_loss"] == 0.5
    assert receipt["success_claim_allowed"] is True
    assert receipt["all_thresholds_met"] is True


def test_receipt_identity_is_independent_of_pointer_order():
    first = score_objectives(_goal(), _evidence(), _pointers())
    reordered = list(reversed(_pointers()))
    second = score_objectives(_goal(), _evidence(), reordered)
    assert first == second
    assert len(first["score_fingerprint"]) == 64


def test_missing_tolerance_retains_score_but_forbids_success_claim():
    goal = _goal()
    goal["objectives"][0]["tolerance"] = None
    receipt = score_objectives(goal, _evidence(), _pointers())
    assert receipt["success_claim_allowed"] is False
    assert receipt["all_thresholds_met"] is None
    assert {item["objective_id"]: item["threshold_met"] for item in receipt["scores"]}["q"] is None


@pytest.mark.parametrize(
    "mutation",
    [
        lambda pointers: pointers.pop(),
        lambda pointers: pointers[0].update({"objective_id": "missing"}),
        lambda pointers: pointers[0].update({"path": ["response", "missing"]}),
        lambda pointers: pointers[0].update({"path": []}),
    ],
)
def test_pointers_must_exactly_cover_and_resolve_every_objective(mutation):
    pointers = _pointers()
    mutation(pointers)
    with pytest.raises(ValueError):
        score_objectives(_goal(), _evidence(), pointers)


@pytest.mark.parametrize("invalid", [True, float("nan"), float("inf"), "1550"])
def test_observed_values_must_be_finite_numbers_not_booleans(invalid):
    evidence = _evidence()
    evidence["response"]["peak_wavelength"] = invalid
    with pytest.raises(ValueError):
        score_objectives(_goal(), evidence, _pointers())


def test_relative_error_uses_declared_target_and_rejects_zero_denominator():
    goal = _goal()
    goal["objectives"][0]["metric"] = "relative_error"
    goal["objectives"][0]["tolerance"] = 0.1
    receipt = score_objectives(goal, _evidence(), _pointers())
    q_score = {item["objective_id"]: item for item in receipt["scores"]}["q"]
    assert q_score["normalized_loss"] == pytest.approx(0.025)
    goal["objectives"][0]["target"] = 0.0
    with pytest.raises(ValueError, match="nonzero target"):
        score_objectives(goal, _evidence(), _pointers())


def test_normalized_goal_is_idempotent_but_tampered_identity_is_rejected():
    first = score_objectives(_goal(), _evidence(), _pointers())
    from comsol_mcp.research.contracts import normalize_research_goal

    normalized = normalize_research_goal(_goal())
    assert score_objectives(normalized, _evidence(), _pointers()) == first
    normalized["objectives"][0]["target"] = 999.0
    with pytest.raises(ValueError, match="fingerprint is invalid"):
        score_objectives(normalized, _evidence(), _pointers())
