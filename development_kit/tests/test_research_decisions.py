"""Hash-chained adaptive research decision contracts."""

from __future__ import annotations

import copy

import pytest

from comsol_mcp.research.decisions import normalize_decision_record


def _decision(sequence: int = 0) -> dict:
    return {
        "schema_name": "comsol_mcp.research_decision",
        "schema_version": "1.0.0",
        "decision_id": f"decision-{sequence:04d}",
        "campaign_fingerprint": "a" * 64,
        "sequence": sequence,
        "previous_decision_fingerprint": None if sequence == 0 else "b" * 64,
        "action": "propose",
        "candidate_ids": ["candidate-001"],
        "reason_code": "initial_design",
        "rationale": "Use the frozen deterministic initial design.",
        "stop_reason": None,
        "budget": {
            "started_fem_evaluations": 0,
            "max_fem_evaluations": 32,
            "elapsed_wall_time_seconds": 0.0,
            "max_wall_time_seconds": 8 * 3600,
        },
        "evidence_fingerprints": [],
        "optimizer_checkpoint_fingerprint": None,
        "producer_identity": "c" * 64,
        "created_at": "2026-08-06T00:00:00Z",
    }


def test_decision_is_stable_and_derives_remaining_budget():
    first = normalize_decision_record(_decision())
    second = normalize_decision_record(_decision())
    assert first == second
    assert first["budget"]["remaining_fem_evaluations"] == 32
    assert first["budget"]["point_budget_exhausted"] is False
    assert len(first["decision_fingerprint"]) == 64


def test_decision_chain_requires_exact_predecessor_shape():
    value = _decision(1)
    assert normalize_decision_record(value)["previous_decision_fingerprint"] == "b" * 64
    value["previous_decision_fingerprint"] = None
    with pytest.raises(ValueError, match="sequence zero"):
        normalize_decision_record(value)
    value = _decision(0)
    value["previous_decision_fingerprint"] = "b" * 64
    with pytest.raises(ValueError, match="sequence zero"):
        normalize_decision_record(value)


def test_stop_reason_is_closed_and_only_valid_for_stop_action():
    value = _decision()
    value["action"] = "stop"
    value["stop_reason"] = "budget_exhausted"
    assert normalize_decision_record(value)["stop_reason"] == "budget_exhausted"
    value["stop_reason"] = "invented_success"
    with pytest.raises(ValueError, match="supported stop_reason"):
        normalize_decision_record(value)
    value = _decision()
    value["stop_reason"] = "success"
    with pytest.raises(ValueError, match="non-stop"):
        normalize_decision_record(value)


@pytest.mark.parametrize("action", ["evaluate", "promote", "reject"])
def test_candidate_actions_require_candidate_ids(action):
    value = _decision()
    value["action"] = action
    value["candidate_ids"] = []
    with pytest.raises(ValueError, match="at least one candidate"):
        normalize_decision_record(value)


def test_budget_snapshot_rejects_boolean_overrun_and_nonfinite_values():
    value = _decision()
    value["budget"]["started_fem_evaluations"] = True
    with pytest.raises(ValueError):
        normalize_decision_record(value)
    value = _decision()
    value["budget"]["started_fem_evaluations"] = 33
    with pytest.raises(ValueError):
        normalize_decision_record(value)
    value = _decision()
    value["budget"]["elapsed_wall_time_seconds"] = float("inf")
    with pytest.raises(ValueError):
        normalize_decision_record(value)


def test_budget_exhaustion_is_derived_not_caller_asserted():
    value = _decision()
    value["budget"]["started_fem_evaluations"] = 32
    value["budget"]["elapsed_wall_time_seconds"] = 8 * 3600
    normalized = normalize_decision_record(value)
    assert normalized["budget"]["remaining_fem_evaluations"] == 0
    assert normalized["budget"]["point_budget_exhausted"] is True
    assert normalized["budget"]["wall_budget_exhausted"] is True


def test_candidate_and_evidence_references_are_unique_and_sorted():
    value = _decision()
    value["candidate_ids"] = ["z", "a"]
    value["evidence_fingerprints"] = ["f" * 64, "d" * 64]
    normalized = normalize_decision_record(value)
    assert normalized["candidate_ids"] == ["a", "z"]
    assert normalized["evidence_fingerprints"] == ["d" * 64, "f" * 64]
    duplicate = copy.deepcopy(value)
    duplicate["candidate_ids"] = ["a", "a"]
    with pytest.raises(ValueError, match="candidate_ids must be unique"):
        normalize_decision_record(duplicate)
