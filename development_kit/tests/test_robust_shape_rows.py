"""Crash recovery and identity tests for robust shape journals."""

from __future__ import annotations

import copy
import json

import pytest

from comsol_mcp.jobs.robust_shape_rows import append_robust_shape_row, read_robust_shape_rows

JOB = "a" * 64


def _condition(order: int = 0) -> dict:
    return {
        "iteration_id": "it-0",
        "condition_id": f"condition-{order:02d}",
        "condition_order": order,
        "status": "completed",
        "observation_fingerprint": "b" * 64,
        "objective_contribution": 0.2,
        "reason_code": "measured",
    }


def _iteration() -> dict:
    return {
        "iteration_id": "it-0",
        "iteration_index": 0,
        "candidate_fingerprint": "c" * 64,
        "aggregate_objective": 0.2,
        "status": "accepted",
        "robust_objective_fingerprint": "d" * 64,
        "fresh_forward_fingerprint": "e" * 64,
        "reason_code": "fresh_forward_accepted",
    }


def test_all_row_kinds_share_one_contiguous_hash_chain(ascii_tmp_path):
    path = ascii_tmp_path / "robust_rows.jsonl"
    payloads = [
        ("condition", _condition()),
        (
            "gradient",
            {
                "iteration_id": "it-0",
                "gradient_fingerprint": "f" * 64,
                "acceptance_fingerprint": "1" * 64,
                "evidence_state": "gradient_validated",
            },
        ),
        ("iteration", _iteration()),
        (
            "trial",
            {
                "iteration_id": "it-0",
                "trial_id": "trial-0",
                "candidate_fingerprint": "2" * 64,
                "aggregate_objective": 0.1,
                "status": "rejected",
                "reason_code": "forward_acceptance_failed",
            },
        ),
        (
            "finalist_validation",
            {
                "iteration_id": "it-0",
                "candidate_fingerprint": "c" * 64,
                "policy_fingerprint": "5" * 64,
                "receipt_fingerprint": "6" * 64,
                "status": "validated",
                "reason_codes": [],
            },
        ),
        (
            "checkpoint",
            {
                "iteration_id": "it-0",
                "checkpoint_fingerprint": "3" * 64,
                "completed_condition_count": 24,
                "retained_model_disposition": "finalist_only",
            },
        ),
        (
            "cleanup",
            {
                "source_unchanged": True,
                "client_clear": True,
                "owned_processes_absent": True,
                "lease_released": True,
                "cleanup_fingerprint": "4" * 64,
            },
        ),
    ]
    previous = None
    for kind, payload in payloads:
        row = append_robust_shape_row(
            path, job_fingerprint=JOB, attempt=1, kind=kind, payload=payload
        )
        assert row["previous_row_sha256"] == previous
        previous = row["row_sha256"]
    rows = read_robust_shape_rows(path, job_fingerprint=JOB)
    assert [row["kind"] for row in rows] == [item[0] for item in payloads]
    assert rows[-1]["row_sha256"] == previous


def test_partial_tail_is_truncated_before_next_hash_chained_append(ascii_tmp_path):
    path = ascii_tmp_path / "partial.jsonl"
    first = append_robust_shape_row(
        path, job_fingerprint=JOB, attempt=1, kind="condition", payload=_condition()
    )
    with path.open("ab") as handle:
        handle.write(b'{"partial":')
    assert (
        read_robust_shape_rows(path, job_fingerprint=JOB)[-1]["row_sha256"] == first["row_sha256"]
    )
    second = append_robust_shape_row(
        path, job_fingerprint=JOB, attempt=2, kind="iteration", payload=_iteration()
    )
    assert second["previous_row_sha256"] == first["row_sha256"]


def test_changed_job_payload_or_chain_is_rejected(ascii_tmp_path):
    path = ascii_tmp_path / "tampered.jsonl"
    append_robust_shape_row(
        path, job_fingerprint=JOB, attempt=1, kind="condition", payload=_condition()
    )
    with pytest.raises(ValueError, match="job identity"):
        read_robust_shape_rows(path, job_fingerprint="9" * 64)
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    row["payload"] = copy.deepcopy(row["payload"])
    row["payload"]["objective_contribution"] = 0.9
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        read_robust_shape_rows(path, job_fingerprint=JOB)


def test_failed_condition_cannot_claim_observation_or_objective_contribution(ascii_tmp_path):
    payload = _condition()
    payload["status"] = "failed"
    payload["observation_fingerprint"] = None
    payload["objective_contribution"] = None
    row = append_robust_shape_row(
        ascii_tmp_path / "failed.jsonl",
        job_fingerprint=JOB,
        attempt=1,
        kind="condition",
        payload=payload,
    )
    assert row["payload"]["status"] == "failed"
    payload["observation_fingerprint"] = "b" * 64
    with pytest.raises(ValueError, match="must not claim"):
        append_robust_shape_row(
            ascii_tmp_path / "invalid-failed.jsonl",
            job_fingerprint=JOB,
            attempt=1,
            kind="condition",
            payload=payload,
        )


def test_rejected_finalist_row_requires_bounded_unique_reasons(ascii_tmp_path):
    payload = {
        "iteration_id": "it-3",
        "candidate_fingerprint": "a" * 64,
        "policy_fingerprint": "b" * 64,
        "receipt_fingerprint": "c" * 64,
        "status": "rejected",
        "reason_codes": ["mesh_convergence_failed", "branch_guard_failed"],
    }
    row = append_robust_shape_row(
        ascii_tmp_path / "rejected-finalist.jsonl",
        job_fingerprint=JOB,
        attempt=1,
        kind="finalist_validation",
        payload=payload,
    )
    assert row["payload"]["reason_codes"] == payload["reason_codes"]
    for reasons in ([], ["same", "same"], [f"reason-{index}" for index in range(7)]):
        payload["reason_codes"] = reasons
        with pytest.raises(ValueError, match="reason_codes"):
            append_robust_shape_row(
                ascii_tmp_path / f"invalid-finalist-{len(reasons)}.jsonl",
                job_fingerprint=JOB,
                attempt=1,
                kind="finalist_validation",
                payload=payload,
            )
