"""Crash and identity tests for adjoint optimization journals."""

import copy
import json

import pytest

from comsol_mcp.jobs.adjoint_rows import append_adjoint_row, read_adjoint_rows

JOB = "a" * 64


def _iteration(index: int = 0) -> dict:
    return {
        "iteration_id": f"it-{index}",
        "iteration_index": index,
        "candidate_fingerprint": "b" * 64,
        "objective_value": 0.8,
        "status": "accepted",
        "gradient_fingerprint": "c" * 64,
        "forward_fingerprint": "d" * 64,
        "reason_code": "fresh_forward_accepted",
    }


def _gradient() -> dict:
    return {
        "iteration_id": "it-0",
        "gradient_fingerprint": "c" * 64,
        "check_fingerprint": "e" * 64,
        "evidence_state": "gradient_validated",
    }


def test_rows_are_hash_chained_and_replayable(ascii_tmp_path):
    path = ascii_tmp_path / "optimization_rows.jsonl"
    first = append_adjoint_row(
        path, job_fingerprint=JOB, attempt=1, kind="iteration", payload=_iteration()
    )
    second = append_adjoint_row(
        path, job_fingerprint=JOB, attempt=1, kind="gradient", payload=_gradient()
    )
    rows = read_adjoint_rows(path, job_fingerprint=JOB)
    assert [row["kind"] for row in rows] == ["iteration", "gradient"]
    assert second["previous_row_sha256"] == first["row_sha256"]
    assert rows[-1]["row_sha256"] == second["row_sha256"]


def test_partial_final_json_is_truncated_and_next_append_continues_chain(ascii_tmp_path):
    path = ascii_tmp_path / "partial_rows.jsonl"
    first = append_adjoint_row(
        path, job_fingerprint=JOB, attempt=1, kind="iteration", payload=_iteration()
    )
    with path.open("ab") as handle:
        handle.write(b'{"partial":')
    rows = read_adjoint_rows(path, job_fingerprint=JOB)
    assert rows[-1]["row_sha256"] == first["row_sha256"]
    second = append_adjoint_row(
        path,
        job_fingerprint=JOB,
        attempt=1,
        kind="trial",
        payload={
            "iteration_id": "it-0",
            "trial_id": "trial-1",
            "candidate_fingerprint": "f" * 64,
            "objective_value": 0.7,
            "status": "rejected",
            "reason_code": "forward_acceptance_failed",
        },
    )
    assert second["previous_row_sha256"] == first["row_sha256"]


def test_changed_job_or_hash_payload_is_rejected(ascii_tmp_path):
    path = ascii_tmp_path / "identity_rows.jsonl"
    append_adjoint_row(path, job_fingerprint=JOB, attempt=1, kind="iteration", payload=_iteration())
    with pytest.raises(ValueError, match="job identity"):
        read_adjoint_rows(path, job_fingerprint="9" * 64)
    value = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    value["payload"] = copy.deepcopy(value["payload"])
    value["payload"]["objective_value"] = 0.1
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        read_adjoint_rows(path, job_fingerprint=JOB)
