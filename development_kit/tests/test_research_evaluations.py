"""Started and terminal research evaluation contracts."""

from __future__ import annotations

import pytest

from comsol_mcp.research.evaluations import normalize_evaluation_record
from comsol_mcp.research.journal import append_research_journal_record, recover_research_journal


def _evaluation(status: str = "started") -> dict:
    terminal = status != "started"
    return {
        "schema_name": "comsol_mcp.research_evaluation",
        "schema_version": "1.0.0",
        "evaluation_id": "evaluation-001-attempt-1",
        "campaign_fingerprint": "a" * 64,
        "candidate_id": "candidate-001",
        "candidate_fingerprint": "b" * 64,
        "attempt": 1,
        "status": status,
        "fidelity": "fake_fem",
        "evaluator_identity": "c" * 64,
        "started_at": "2026-08-06T00:00:00Z",
        "completed_at": "2026-08-06T00:00:01Z" if terminal else None,
        "response": {"peak_wavelength_nm": 1550.0, "q_factor": 20.0}
        if status == "completed"
        else None,
        "evidence_fingerprints": ["d" * 64] if status == "completed" else [],
        "failure_reason": "synthetic_failure" if terminal and status != "completed" else None,
    }


def test_started_and_completed_records_are_distinct_stable_authority():
    started = normalize_evaluation_record(_evaluation())
    completed = normalize_evaluation_record(_evaluation("completed"))
    assert started["status"] == "started"
    assert completed["status"] == "completed"
    assert started["evaluation_fingerprint"] != completed["evaluation_fingerprint"]
    assert completed == normalize_evaluation_record(_evaluation("completed"))


@pytest.mark.parametrize("status", ["failed", "infeasible", "cancelled"])
def test_unsuccessful_terminal_records_require_failure_reason(status):
    value = _evaluation(status)
    assert normalize_evaluation_record(value)["failure_reason"] == "synthetic_failure"
    value["failure_reason"] = None
    with pytest.raises(ValueError, match="failure reason"):
        normalize_evaluation_record(value)


def test_started_record_rejects_terminal_fields_and_completed_requires_response():
    value = _evaluation()
    value["completed_at"] = "2026-08-06T00:00:01Z"
    with pytest.raises(ValueError, match="cannot contain terminal"):
        normalize_evaluation_record(value)
    value = _evaluation("completed")
    value["response"] = None
    with pytest.raises(ValueError, match="require response"):
        normalize_evaluation_record(value)


def test_attempt_and_evidence_contracts_reject_boolean_and_duplicates():
    value = _evaluation("completed")
    value["attempt"] = True
    with pytest.raises(ValueError):
        normalize_evaluation_record(value)
    value = _evaluation("completed")
    value["evidence_fingerprints"] = ["d" * 64, "d" * 64]
    with pytest.raises(ValueError, match="must be unique"):
        normalize_evaluation_record(value)


def test_evaluation_records_round_trip_through_research_journal(tmp_path):
    path = tmp_path / "research.jsonl"
    started = normalize_evaluation_record(_evaluation())
    first = append_research_journal_record(
        path,
        "evaluation",
        started,
        expected_previous_record_fingerprint=None,
    )
    append_research_journal_record(
        path,
        "evaluation",
        normalize_evaluation_record(_evaluation("completed")),
        expected_previous_record_fingerprint=first["record_fingerprint"],
    )
    recovered = recover_research_journal(path)
    assert [item["payload"]["status"] for item in recovered["records"]] == [
        "started",
        "completed",
    ]
