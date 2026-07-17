"""Solver-free execution, evidence, and scientific outcome contract tests."""

from __future__ import annotations

from copy import deepcopy
from itertools import product

import pytest

from src.evidence.outcome_contract import (
    EVIDENCE_COMPLETENESS_STATES,
    EXECUTION_STATES,
    OUTCOME_SCHEMA_NAME,
    OUTCOME_SCHEMA_VERSION,
    SCIENTIFIC_DISPOSITIONS,
    build_outcome_contract,
    execution_from_terminal_job_state,
    validate_outcome_contract,
)


def _payload(execution: str, evidence: str, disposition: str) -> dict:
    cleanup_verified = execution in {"completed", "cancelled"}
    missing = [] if evidence == "complete" else ["spectrum.own_peak"]
    raw_ids = ["raw-point-001"]
    diagnostic = raw_ids if execution != "completed" else []
    next_action = {
        "accepted": "none",
        "residual": "revise_scientific_policy",
        "unresolved_at_declared_cap": "declare_larger_cap",
        "invalid_evidence": "repair_evidence_chain",
        "not_evaluated": "retry_execution",
    }[disposition]
    return {
        "schema_name": OUTCOME_SCHEMA_NAME,
        "schema_version": OUTCOME_SCHEMA_VERSION,
        "subject_id": "bounded-spectrum",
        "execution": {
            "state": execution,
            "reason_code": f"execution_{execution}",
            "completed_requested_work": execution == "completed",
            "cleanup": {
                "processes_absent": cleanup_verified,
                "descendants_absent": cleanup_verified,
                "port_closed": cleanup_verified,
                "lease_absent": cleanup_verified,
                "verified": cleanup_verified,
            },
        },
        "evidence": {
            "state": evidence,
            "missing_evidence": missing,
            "raw_artifact_ids": raw_ids,
            "diagnostic_artifact_ids": diagnostic,
        },
        "scientific": {
            "disposition": disposition,
            "reason_code": f"scientific_{disposition}",
            "declared_cap_reached": disposition == "unresolved_at_declared_cap",
            "next_eligible_action": next_action,
        },
    }


def _compatible(execution: str, evidence: str, disposition: str) -> bool:
    if disposition in {"accepted", "residual", "unresolved_at_declared_cap"}:
        return execution == "completed" and evidence == "complete"
    if disposition == "invalid_evidence":
        return evidence != "complete"
    return True


def test_cartesian_product_is_deterministically_accepted_or_rejected():
    combinations = set(product(EXECUTION_STATES, EVIDENCE_COMPLETENESS_STATES, SCIENTIFIC_DISPOSITIONS))
    observed = set()
    for execution, evidence, disposition in sorted(combinations):
        observed.add((execution, evidence, disposition))
        payload = _payload(execution, evidence, disposition)
        if _compatible(execution, evidence, disposition):
            contract = build_outcome_contract(payload)
            assert validate_outcome_contract(contract) == contract
        else:
            with pytest.raises(ValueError):
                build_outcome_contract(payload)
    assert observed == combinations


def test_boundary_high_can_complete_with_unresolved_disposition():
    contract = build_outcome_contract(
        _payload("completed", "complete", "unresolved_at_declared_cap")
    )

    assert contract["execution"]["state"] == "completed"
    assert contract["scientific"] == {
        "disposition": "unresolved_at_declared_cap",
        "reason_code": "scientific_unresolved_at_declared_cap",
        "declared_cap_reached": True,
        "next_eligible_action": "declare_larger_cap",
    }


def test_nonaccepted_outcomes_require_reason_cap_missing_list_and_next_action():
    payload = _payload("completed", "complete", "residual")
    for field in ("reason_code", "declared_cap_reached", "next_eligible_action"):
        broken = deepcopy(payload)
        broken["scientific"].pop(field)
        with pytest.raises(ValueError, match="incomplete"):
            build_outcome_contract(broken)

    broken = deepcopy(payload)
    broken["evidence"].pop("missing_evidence")
    with pytest.raises(ValueError, match="incomplete"):
        build_outcome_contract(broken)


def test_cancelled_terminal_requires_complete_cleanup_proof_and_keeps_rows_diagnostic():
    payload = _payload("cancelled", "incomplete", "not_evaluated")
    contract = build_outcome_contract(payload)
    assert contract["evidence"]["diagnostic_artifact_ids"] == ["raw-point-001"]

    for field in ("processes_absent", "descendants_absent", "port_closed", "lease_absent"):
        broken = deepcopy(payload)
        broken["execution"]["cleanup"][field] = False
        broken["execution"]["cleanup"]["verified"] = False
        with pytest.raises(ValueError, match="cancelled execution requires verified cleanup"):
            build_outcome_contract(broken)


def test_policy_failure_cannot_mutate_or_delete_raw_evidence_references():
    payload = _payload("completed", "complete", "residual")
    original = deepcopy(payload)
    contract = build_outcome_contract(payload)

    assert payload == original
    assert contract["evidence"]["raw_artifact_ids"] == original["evidence"]["raw_artifact_ids"]
    assert len(contract["outcome_sha256"]) == 64


def test_unknown_fields_and_hash_tampering_fail_closed():
    payload = _payload("completed", "complete", "accepted")
    payload["scientific"]["paper_target"] = 0.99
    with pytest.raises(ValueError, match="unknown fields"):
        build_outcome_contract(payload)

    contract = build_outcome_contract(_payload("completed", "complete", "accepted"))
    contract["scientific"]["reason_code"] = "tampered"
    with pytest.raises(ValueError, match="does not match"):
        validate_outcome_contract(contract)


def test_verified_cancelled_job_maps_to_terminal_execution_without_losing_diagnostic_rows():
    job_state = {
        "status": "cancelled",
        "cancel": {
            "verification": {
                "absent": True,
                "verdicts": [],
                "solver": {
                    "ok": True,
                    "lease_state": "absent",
                    "recorded_port_closed": True,
                },
            }
        },
    }
    execution = execution_from_terminal_job_state(job_state)
    payload = _payload("cancelled", "incomplete", "not_evaluated")
    payload["execution"] = execution
    contract = build_outcome_contract(payload)

    assert execution["cleanup"]["verified"] is True
    assert contract["evidence"]["raw_artifact_ids"] == ["raw-point-001"]
    assert contract["evidence"]["diagnostic_artifact_ids"] == ["raw-point-001"]


def test_cancelled_mapping_fails_closed_without_process_port_or_lease_proof():
    for verification in (
        {"absent": False, "solver": {"lease_state": "absent", "recorded_port_closed": True}},
        {"absent": True, "solver": {"lease_state": "uncertain", "recorded_port_closed": True}},
        {"absent": True, "solver": {"lease_state": "absent", "recorded_port_closed": False}},
    ):
        with pytest.raises(ValueError, match="cancelled execution requires verified cleanup"):
            execution_from_terminal_job_state(
                {"status": "cancelled", "cancel": {"verification": verification}}
            )


def test_process_loss_maps_to_interrupted_and_never_claims_cleanup_or_acceptance():
    execution = execution_from_terminal_job_state(
        {
            "status": "interrupted",
            "last_error": {
                "type": "WorkerInterrupted",
                "message": "worker PID no longer exists",
            },
        }
    )
    assert execution["state"] == "interrupted"
    assert execution["completed_requested_work"] is False
    assert execution["cleanup"]["verified"] is False

    payload = _payload("interrupted", "incomplete", "not_evaluated")
    payload["execution"] = execution
    assert build_outcome_contract(payload)["scientific"]["disposition"] == "not_evaluated"


def test_nonterminal_or_completed_without_cleanup_proof_cannot_be_summarized():
    for status in ("submitted", "running", "cancel_requested", "cancelling"):
        with pytest.raises(ValueError, match="reconciled"):
            execution_from_terminal_job_state({"status": status})

    with pytest.raises(ValueError, match="completed execution requires verified cleanup"):
        execution_from_terminal_job_state({"status": "completed"})

    cleanup = {
        "processes_absent": True,
        "descendants_absent": True,
        "port_closed": True,
        "lease_absent": True,
        "verified": True,
    }
    assert execution_from_terminal_job_state(
        {"status": "completed", "cleanup_verification": cleanup}
    )["state"] == "completed"
