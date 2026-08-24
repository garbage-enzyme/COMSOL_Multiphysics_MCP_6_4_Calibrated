"""Solver-free contract suite for bounded-step receipts and replay decisions.

The suite covers the synthetic worker fault matrix from the alpha7.3 plan:
crash before checkpoint, crash after checkpoint, interruption during fsync,
interruption during post-readback, duplicate replay after a terminal row,
and cleanup uncertainty. Nothing here starts COMSOL, Java, MPh, or JPype.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from comsol_mcp.jobs.bounded_steps import (
    BOUNDED_STEP_RECEIPT_SCHEMA_NAME,
    BOUNDED_STEP_SCHEMA_VERSION,
    BOUNDED_STEPS_FILENAME,
    BoundedStepError,
    append_bounded_step,
    build_bounded_step_receipt,
    decide_resume,
    read_bounded_steps,
    verify_checkpoint_usability,
)
from comsol_mcp.schema_registry import check_schema_support


def _record(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_name": BOUNDED_STEP_RECEIPT_SCHEMA_NAME,
        "schema_version": BOUNDED_STEP_SCHEMA_VERSION,
        "job_id": "job-test",
        "attempt": 1,
        "step_id": "step-1",
        "created_at_epoch": 1_700_000_000.0,
        "input_fingerprint": "a" * 64,
        "pre_state_hash": "b" * 64,
        "post_state_hash": None,
        "checkpoint_policy": "none",
        "checkpoint_path_redacted": None,
        "checkpoint_sha256": None,
        "checkpoint_byte_size": None,
        "numerical_checks": [],
        "transport_status": "ok",
        "execution_status": "completed",
        "evidence_status": "complete",
        "cleanup": "proven",
        "scientific_disposition": "pass",
        "failure_disposition": "none",
        "failure_reason_codes": [],
        "source_identity": {
            "model_path_redacted": "**/model.mph",
            "file_sha256": "c" * 64,
            "model_fingerprint": "d" * 64,
            "revision": "rev-1",
        },
        "previous_step_sha256": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Receipt construction
# ---------------------------------------------------------------------------


def test_receipt_binds_every_published_field_and_is_deterministic():
    first = build_bounded_step_receipt(_record())
    second = build_bounded_step_receipt(_record())
    for field in (
        "job_id",
        "attempt",
        "step_id",
        "input_fingerprint",
        "pre_state_hash",
        "post_state_hash",
        "checkpoint_policy",
        "checkpoint_path_redacted",
        "checkpoint_sha256",
        "numerical_checks",
        "transport_status",
        "execution_status",
        "evidence_status",
        "cleanup",
        "failure_disposition",
        "source_identity",
        "receipt_sha256",
    ):
        mapped = {"receipt_sha256": "step_sha256"}.get(field, field)
        assert mapped in first, field
    assert first == second
    assert first["step_sha256"]
    body = {key: value for key, value in first.items() if key != "step_sha256"}
    assert first["step_sha256"] == build_bounded_step_receipt(body)["step_sha256"]


def test_unknown_and_missing_fields_are_rejected():
    with pytest.raises(BoundedStepError):
        build_bounded_step_receipt({**_record(), "unexpected": 1})
    incomplete = _record()
    del incomplete["pre_state_hash"]
    with pytest.raises(BoundedStepError):
        build_bounded_step_receipt(incomplete)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("transport_status", "fast"),
        ("execution_status", "mostly-done"),
        ("evidence_status", "solid"),
        ("cleanup", "probably"),
        ("scientific_disposition", "fine"),
        ("failure_disposition", "shrug"),
        ("checkpoint_policy", "sometimes"),
    ],
)
def test_status_vocabularies_fail_closed(field, value):
    with pytest.raises(BoundedStepError):
        build_bounded_step_receipt(_record(**{field: value}))


def test_checkpoint_policy_none_rules_are_enforced():
    with pytest.raises(BoundedStepError):
        build_bounded_step_receipt(
            _record(checkpoint_policy="none", checkpoint_path_redacted="**/x.mph")
        )
    with pytest.raises(BoundedStepError):
        build_bounded_step_receipt(
            _record(
                checkpoint_policy="none",
                execution_status="crashed",
                cleanup="pending",
                scientific_disposition="not_evaluated",
                failure_disposition="restart_from_checkpoint",
            )
        )


def test_failure_disposition_none_requires_clean_execution():
    with pytest.raises(BoundedStepError):
        build_bounded_step_receipt(_record(execution_status="failed"))
    with pytest.raises(BoundedStepError):
        build_bounded_step_receipt(_record(failure_reason_codes=["solver_timeout"]))


def test_numerical_checks_fail_closed_on_missing_observation_and_overrun():
    with pytest.raises(BoundedStepError):
        build_bounded_step_receipt(
            _record(
                numerical_checks=[
                    {
                        "name": "closure",
                        "expected": 1.0,
                        "observed": None,
                        "tolerance": 1e-9,
                        "unit": None,
                        "passed": True,
                        "reason_code": None,
                    }
                ]
            )
        )
    overrun = [
        {
            "name": f"check-{index}",
            "expected": None,
            "observed": 1.0,
            "tolerance": None,
            "unit": None,
            "passed": True,
            "reason_code": None,
        }
        for index in range(33)
    ]
    with pytest.raises(BoundedStepError):
        build_bounded_step_receipt(_record(numerical_checks=overrun))
    accepted = build_bounded_step_receipt(
        _record(
            numerical_checks=[
                {
                    "name": "closure",
                    "expected": 1.0,
                    "observed": 0.999999999,
                    "tolerance": 1e-9,
                    "unit": None,
                    "passed": True,
                    "reason_code": None,
                }
            ]
        )
    )
    assert accepted["numerical_checks"][0]["passed"] is True


# ---------------------------------------------------------------------------
# Journal: append, chain, duplicates, torn tails
# ---------------------------------------------------------------------------


def test_journal_roundtrip_chains_rows_and_rejects_duplicates(tmp_path):
    first = append_bounded_step(tmp_path, _record(step_id="s1"))
    second = append_bounded_step(
        tmp_path,
        _record(step_id="s2", previous_step_sha256=None),
    )
    assert second["previous_step_sha256"] == first["step_sha256"]

    outcome = read_bounded_steps(tmp_path / BOUNDED_STEPS_FILENAME)
    assert outcome["state"] == "current_valid"
    assert [row["step_id"] for row in outcome["rows"]] == ["s1", "s2"]
    assert outcome["warnings"] == []

    with pytest.raises(BoundedStepError):
        append_bounded_step(tmp_path, _record(step_id="s1"))


def test_torn_tail_is_classified_and_blocks_further_appends(tmp_path):
    append_bounded_step(tmp_path, _record(step_id="s1"))
    path = tmp_path / BOUNDED_STEPS_FILENAME
    raw = path.read_bytes()
    path.write_bytes(raw + b'{"schema_name": "trunca')

    outcome = read_bounded_steps(path)
    assert outcome["state"] == "incomplete"
    assert "bounded_step_torn_tail" in outcome["warnings"]
    assert len(outcome["rows"]) == 1

    with pytest.raises(BoundedStepError):
        append_bounded_step(tmp_path, _record(step_id="s2"))


def test_invalid_row_is_isolated_without_poisoning_valid_history(tmp_path):
    append_bounded_step(tmp_path, _record(step_id="s1"))
    path = tmp_path / BOUNDED_STEPS_FILENAME
    broken = json.dumps({**_record(step_id="broken"), "transport_status": "warp"})
    path.write_text(
        path.read_text(encoding="utf-8") + broken + "\n",
        encoding="utf-8",
    )
    outcome = read_bounded_steps(path)
    assert any(w.startswith("bounded_step_row_invalid:") for w in outcome["warnings"])
    # The broken row is classified and skipped; the earlier valid history
    # stays readable instead of poisoning the whole journal.
    assert [row["step_id"] for row in outcome["rows"]] == ["s1"]
    # And an uncertain journal refuses further appends.
    with pytest.raises(BoundedStepError):
        append_bounded_step(tmp_path, _record(step_id="s2"))


# ---------------------------------------------------------------------------
# Checkpoint usability and resume decisions
# ---------------------------------------------------------------------------


def _checkpoint_record(payload: bytes, **overrides: Any) -> dict[str, Any]:
    return _record(
        checkpoint_policy="after_step",
        checkpoint_path_redacted="**/ckpt.mph",
        checkpoint_sha256=hashlib.sha256(payload).hexdigest(),
        checkpoint_byte_size=len(payload),
        **overrides,
    )


def test_crash_before_checkpoint_decides_restart(tmp_path):
    crashed = build_bounded_step_receipt(
        _record(
            step_id="s1",
            execution_status="crashed",
            evidence_status="incomplete",
            cleanup="pending",
            scientific_disposition="not_evaluated",
            failure_disposition="pause_and_repair",
            failure_reason_codes=["worker_interrupted"],
        )
    )
    decision = decide_resume([crashed], checkpoint_verified=None)
    assert decision == {
        "decision": "restart",
        "reason_codes": ["checkpoint_unverified"],
    }


def test_crash_after_checkpoint_resumes_only_with_verified_bytes(tmp_path):
    payload = b"durable checkpoint bytes"
    row = build_bounded_step_receipt(
        _checkpoint_record(
            payload,
            step_id="s1",
            execution_status="cancelled",
            evidence_status="incomplete",
            cleanup="pending",
            scientific_disposition="not_evaluated",
            failure_disposition="restart_from_checkpoint",
        )
    )

    verified = verify_checkpoint_usability(
        row,
        tmp_path / "ckpt.mph",
        current_source_file_sha256=row["source_identity"]["file_sha256"],
        current_model_fingerprint=row["source_identity"]["model_fingerprint"],
        current_revision=row["source_identity"]["revision"],
    )
    assert verified["usable"] is False  # the checkpoint file does not exist yet
    (tmp_path / "ckpt.mph").write_bytes(payload)
    verified = verify_checkpoint_usability(
        row,
        tmp_path / "ckpt.mph",
        current_source_file_sha256=row["source_identity"]["file_sha256"],
        current_model_fingerprint=row["source_identity"]["model_fingerprint"],
        current_revision=row["source_identity"]["revision"],
    )
    assert verified == {"usable": True, "reason_codes": []}

    decision = decide_resume([row], checkpoint_verified=True)
    assert decision == {"decision": "resume_from_checkpoint", "reason_codes": []}


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (lambda p: p.write_bytes(b"tampered"), "checkpoint_hash_mismatch"),
        (lambda p: p.write_bytes(b""), "checkpoint_byte_size_mismatch"),
        (lambda p: p.unlink(), "checkpoint_unreadable"),
    ],
)
def test_checkpoint_verification_fails_closed_on_each_mutation(tmp_path, mutate, expected_code):
    payload = b"original checkpoint"
    row = build_bounded_step_receipt(_checkpoint_record(payload, step_id="s1"))
    target = tmp_path / "ckpt.mph"
    target.write_bytes(payload)
    mutate(target)

    result = verify_checkpoint_usability(
        row,
        target,
        current_source_file_sha256=row["source_identity"]["file_sha256"],
        current_model_fingerprint=row["source_identity"]["model_fingerprint"],
        current_revision=row["source_identity"]["revision"],
    )
    assert result["usable"] is False
    assert expected_code in result["reason_codes"]


def test_undeclared_current_expectations_fail_closed(tmp_path):
    payload = b"bytes"
    row = build_bounded_step_receipt(_checkpoint_record(payload))
    target = tmp_path / "ckpt.mph"
    target.write_bytes(payload)
    result = verify_checkpoint_usability(
        row,
        target,
        current_source_file_sha256=None,
        current_model_fingerprint=None,
        current_revision=None,
    )
    assert result["usable"] is False
    assert {
        "source_identity_undeclared",
        "model_identity_undeclared",
        "revision_undeclared",
    } <= set(result["reason_codes"])


def test_drifted_source_model_or_revision_blocks_resume(tmp_path):
    payload = b"bytes"
    row = build_bounded_step_receipt(_checkpoint_record(payload))
    target = tmp_path / "ckpt.mph"
    target.write_bytes(payload)

    drifted_source = verify_checkpoint_usability(
        row,
        target,
        current_source_file_sha256="f" * 64,
        current_model_fingerprint=row["source_identity"]["model_fingerprint"],
        current_revision=row["source_identity"]["revision"],
    )
    assert "source_identity_mismatch" in drifted_source["reason_codes"]

    drifted_revision = verify_checkpoint_usability(
        row,
        target,
        current_source_file_sha256=row["source_identity"]["file_sha256"],
        current_model_fingerprint=row["source_identity"]["model_fingerprint"],
        current_revision="rev-2",
    )
    assert "revision_mismatch" in drifted_revision["reason_codes"]

    drifted_model = verify_checkpoint_usability(
        row,
        target,
        current_source_file_sha256=row["source_identity"]["file_sha256"],
        current_model_fingerprint="e" * 64,
        current_revision=row["source_identity"]["revision"],
    )
    assert "model_identity_mismatch" in drifted_model["reason_codes"]


def test_resume_decision_matrix():
    finished = build_bounded_step_receipt(_record())
    assert decide_resume([finished], checkpoint_verified=None) == {
        "decision": "none_needed",
        "reason_codes": [],
    }
    abandoned = build_bounded_step_receipt(
        _record(
            execution_status="failed",
            cleanup="failed",
            evidence_status="incomplete",
            scientific_disposition="not_evaluated",
            failure_disposition="abandoned",
            failure_reason_codes=["irrecoverable"],
        )
    )
    assert decide_resume([abandoned], checkpoint_verified=True) == {
        "decision": "restart",
        "reason_codes": ["step_abandoned"],
    }
    assert decide_resume([], checkpoint_verified=True) == {
        "decision": "restart",
        "reason_codes": ["no_completed_steps"],
    }


# ---------------------------------------------------------------------------
# Replay after a terminal row (new attempt continues the same journal)
# ---------------------------------------------------------------------------


def test_new_attempt_appends_after_terminal_row_without_duplicate(tmp_path):
    terminal = append_bounded_step(tmp_path, _record(step_id="train"))
    replayed = append_bounded_step(
        tmp_path,
        _record(step_id="train", attempt=2, previous_step_sha256=None),
    )
    assert replayed["previous_step_sha256"] == terminal["step_sha256"]
    outcome = read_bounded_steps(tmp_path / BOUNDED_STEPS_FILENAME)
    assert [(row["attempt"], row["step_id"]) for row in outcome["rows"]] == [
        (1, "train"),
        (2, "train"),
    ]


# ---------------------------------------------------------------------------
# Status surfacing through the durable manager (warning-only)
# ---------------------------------------------------------------------------


def test_job_status_surfaces_the_bounded_step_summary(tmp_path):
    from comsol_mcp.jobs.manager import JobManager

    manager = JobManager(root=tmp_path, reconcile_on_start=False)
    job_id = manager.store.create(
        spec={"job_type": "unit_stub"},
        state={
            "schema_version": 1,
            "status": "completed",
            "attempt": 1,
            "progress": None,
            "worker_pid": None,
        },
    )
    directory = tmp_path / job_id
    append_bounded_step(directory, _record(step_id="only"))

    result = manager.status(job_id)
    assert result["success"] is True
    summary = result["bounded_steps"]
    assert summary["available"] is True
    assert summary["step_count"] == 1
    assert summary["chain_intact"] is True
    assert summary["last_step"]["step_id"] == "only"

    # A job without a journal simply carries no field at all.
    other = manager.store.create(
        spec={"job_type": "unit_stub"},
        state={"schema_version": 1, "status": "completed", "attempt": 1},
    )
    bare = manager.status(other)
    assert "bounded_steps" not in bare


def test_corrupt_bounded_step_journal_never_changes_job_disposition(tmp_path):
    from comsol_mcp.jobs.manager import JobManager

    manager = JobManager(root=tmp_path, reconcile_on_start=False)
    job_id = manager.store.create(
        spec={"job_type": "unit_stub"},
        state={"schema_version": 1, "status": "completed", "attempt": 1},
    )
    (tmp_path / job_id / BOUNDED_STEPS_FILENAME).write_bytes(b"\x00\x01 not json\n")

    result = manager.status(job_id)
    assert result["success"] is True
    summary = result["bounded_steps"]
    assert summary["available"] is True
    assert summary["journal_state"] == "corrupt"
    assert summary["chain_intact"] is False
    assert summary["last_step"] is None


# ---------------------------------------------------------------------------
# Registry membership and import hygiene
# ---------------------------------------------------------------------------


def test_schema_registry_supports_the_bounded_step_contract():
    support = check_schema_support(BOUNDED_STEP_RECEIPT_SCHEMA_NAME, BOUNDED_STEP_SCHEMA_VERSION)
    assert support["supported"] is True
    assert support["producer"] == "comsol_mcp.jobs.bounded_steps"


def test_bounded_steps_module_never_imports_solver_dependencies():
    source = Path("comsol_mcp") / "jobs" / "bounded_steps.py"
    text = source.read_text(encoding="utf-8")
    for forbidden in ("import mph", "from mph", "import jpype", "from jpype"):
        assert forbidden not in text
