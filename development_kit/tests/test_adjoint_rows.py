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


def test_float_sequence_is_rejected_as_non_contiguous(ascii_tmp_path):
    path = ascii_tmp_path / "float_sequence_rows.jsonl"
    append_adjoint_row(path, job_fingerprint=JOB, attempt=1, kind="iteration", payload=_iteration())
    raw = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    raw["sequence"] = 0.0
    path.write_text(json.dumps(raw) + "\n", encoding="ascii")
    with pytest.raises(ValueError, match="not contiguous"):
        read_adjoint_rows(path, job_fingerprint=JOB)


@pytest.mark.parametrize("bad_epoch", [True, False, "123", [1.0], {"t": 1.0}])
def test_created_at_epoch_type_is_validated_before_float_coercion(ascii_tmp_path, bad_epoch):
    # Bools and numeric strings silently passed float() coercion and skipped
    # the declared float | None contract; they must fail before normalization.
    path = ascii_tmp_path / "epoch_rows.jsonl"
    with pytest.raises(ValueError, match="created_at_epoch must be a finite number"):
        append_adjoint_row(
            path,
            job_fingerprint=JOB,
            attempt=1,
            kind="iteration",
            payload=_iteration(),
            created_at_epoch=bad_epoch,
        )
    assert not path.exists()


def test_created_at_epoch_accepts_declared_numeric_contract(ascii_tmp_path):
    path = ascii_tmp_path / "epoch_ok_rows.jsonl"
    row = append_adjoint_row(
        path,
        job_fingerprint=JOB,
        attempt=1,
        kind="iteration",
        payload=_iteration(),
        created_at_epoch=123,
    )
    assert row["created_at_epoch"] == 123.0


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


def test_oversized_unterminated_tail_never_discards_earlier_rows(ascii_tmp_path):
    from comsol_mcp.jobs.adjoint_rows import MAX_ADJOINT_ROW_BYTES
    from comsol_mcp.jobs.journal import recover_jsonl_tail

    path = ascii_tmp_path / "oversized_tail.jsonl"
    first = append_adjoint_row(
        path, job_fingerprint=JOB, attempt=1, kind="iteration", payload=_iteration()
    )
    complete = path.read_bytes()
    with path.open("ab") as handle:
        handle.write(b'{"junk":' + b"x" * (MAX_ADJOINT_ROW_BYTES + 8))
    recover_jsonl_tail(path, max_row_bytes=MAX_ADJOINT_ROW_BYTES)
    assert path.read_bytes() == complete

    second = append_adjoint_row(
        path, job_fingerprint=JOB, attempt=1, kind="gradient", payload=_gradient()
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
