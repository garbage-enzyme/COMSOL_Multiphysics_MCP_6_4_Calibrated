"""Optimizer checkpoint and candidate portfolio contracts."""

from __future__ import annotations

import copy

import pytest

from comsol_mcp.research.state import normalize_optimizer_checkpoint, normalize_portfolio


def _checkpoint() -> dict:
    return {
        "schema_name": "comsol_mcp.research_optimizer_checkpoint",
        "schema_version": "1.0.0",
        "campaign_fingerprint": "a" * 64,
        "sequence": 3,
        "decision_fingerprint": "b" * 64,
        "backend": {"name": "deterministic_random", "version": "1", "identity": "c" * 64},
        "random_state": {"seed": 17001, "draws": 3},
        "optimizer_state": {"asked": 3, "told": 2},
        "history_fingerprint": "d" * 64,
        "candidate_fingerprints": ["e" * 64, "f" * 64],
        "created_at": "2026-08-06T00:00:00Z",
    }


def _portfolio() -> dict:
    return {
        "schema_name": "comsol_mcp.research_portfolio",
        "schema_version": "1.0.0",
        "campaign_fingerprint": "a" * 64,
        "items": [
            {
                "candidate_id": "candidate-001",
                "candidate_fingerprint": "e" * 64,
                "disposition": "converged",
                "objective_values": {"peak_error_nm": 2.0, "q_error": 0.5},
                "evidence_fingerprints": ["1" * 64],
                "unresolved_risks": ["Independent validation pending."],
                "strictly_verified": False,
            },
            {
                "candidate_id": "candidate-002",
                "candidate_fingerprint": "f" * 64,
                "disposition": "rejected",
                "objective_values": {},
                "evidence_fingerprints": [],
                "unresolved_risks": ["Geometry preflight failed."],
                "strictly_verified": False,
            },
        ],
        "selected_candidate_ids": ["candidate-001"],
        "created_at": "2026-08-06T00:00:00Z",
    }


def test_checkpoint_is_backend_neutral_stable_and_defensive():
    value = _checkpoint()
    normalized = normalize_optimizer_checkpoint(value)
    value["optimizer_state"]["asked"] = 99
    assert normalized["optimizer_state"]["asked"] == 3
    assert normalized == normalize_optimizer_checkpoint(_checkpoint())
    assert len(normalized["checkpoint_fingerprint"]) == 64


def test_checkpoint_rejects_unknown_fields_duplicate_history_and_boolean_sequence():
    value = _checkpoint()
    value["backend"]["callable"] = "execute"
    with pytest.raises(ValueError, match="fields mismatch"):
        normalize_optimizer_checkpoint(value)
    value = _checkpoint()
    value["candidate_fingerprints"] = ["e" * 64, "e" * 64]
    with pytest.raises(ValueError, match="unique"):
        normalize_optimizer_checkpoint(value)
    value = _checkpoint()
    value["sequence"] = True
    with pytest.raises(ValueError):
        normalize_optimizer_checkpoint(value)


def test_portfolio_is_canonical_and_keeps_negative_results():
    first = normalize_portfolio(_portfolio())
    reordered = _portfolio()
    reordered["items"].reverse()
    second = normalize_portfolio(reordered)
    assert first["portfolio_fingerprint"] == second["portfolio_fingerprint"]
    assert [item["disposition"] for item in first["items"]] == ["converged", "rejected"]


def test_portfolio_rejects_duplicate_points_and_missing_or_rejected_selection():
    value = _portfolio()
    value["items"][1]["candidate_fingerprint"] = "e" * 64
    with pytest.raises(ValueError, match="fingerprints must be unique"):
        normalize_portfolio(value)
    value = _portfolio()
    value["selected_candidate_ids"] = ["missing"]
    with pytest.raises(ValueError, match="must exist"):
        normalize_portfolio(value)
    value = _portfolio()
    value["selected_candidate_ids"] = ["candidate-002"]
    with pytest.raises(ValueError, match="rejected candidates"):
        normalize_portfolio(value)


def test_strict_verification_requires_zero_unresolved_risks():
    value = _portfolio()
    value["items"][0]["strictly_verified"] = True
    with pytest.raises(ValueError, match="unresolved risks"):
        normalize_portfolio(value)
    value["items"][0]["unresolved_risks"] = []
    assert normalize_portfolio(value)["items"][0]["strictly_verified"] is True


def test_portfolio_inputs_are_defensive_copies_and_finite():
    value = _portfolio()
    normalized = normalize_portfolio(value)
    value["items"][0]["objective_values"]["q_error"] = 99
    assert normalized["items"][0]["objective_values"]["q_error"] == 0.5
    invalid = copy.deepcopy(value)
    invalid["items"][0]["objective_values"]["q_error"] = float("nan")
    with pytest.raises(ValueError):
        normalize_portfolio(invalid)
