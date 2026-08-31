"""Solver-free PID reuse / crash / partial-output / cleanup suite.

Covers the alpha7.3 observation matrix: observer timeout, early worker exit,
stale PID, PID reuse, changed command line, partial log, missing and delayed
output, exact-owner cleanup, and the resume-or-restart boundary. No COMSOL,
Java, MPh, or JPype is started; no real process is inspected or terminated.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from comsol_mcp.jobs.manager import JobManager
from comsol_mcp.jobs.observation import (
    OBSERVATION_RECEIPT_FILENAME,
    OBSERVATION_RECEIPT_SCHEMA_NAME,
    OBSERVATION_SCHEMA_VERSION,
    ObservationError,
    build_observation_receipt,
    classify_observer_outcome,
    summarize_observation,
    verify_exact_ownership,
)
from comsol_mcp.schema_registry import check_schema_support


def _identity(**overrides: Any) -> dict[str, Any]:
    identity: dict[str, Any] = {
        "pid": 4242,
        "process_create_time": 1_700_000_000.0,
        "executable": "D:/tools/comsol/6.4.0/win64/comsolmphserver.exe",
        "command_signature": "a" * 64,
        "target_files": ["**/model.mph", "**/out.csv"],
    }
    identity.update(overrides)
    return identity


def _receipt(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "job_id": "job-obs",
        "attempt": 1,
        "process_identity": _identity(),
        "observer_started_epoch": 1_700_000_000.0,
        "deadline_epoch": 1_700_000_600.0,
        "terminal_state": None,
        "terminal_identity_verified": False,
        "transport_alive": True,
        "worker_exit_observed": None,
        "observer_deadline_exceeded": False,
        "log_tail": ["step 1 ok"],
        "log_truncated": False,
        "last_output_epoch": 1_700_000_010.0,
        "cleanup_outcome": "pending",
        "checkpoint_usable": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Exact ownership
# ---------------------------------------------------------------------------


def test_stale_pid_and_pid_reuse_are_distinguished():
    # A different PID under the same expectation means the recorded process
    # is gone (or was never this one): the identity no longer binds.
    absent = verify_exact_ownership(_identity(), {**_identity(), "pid": 5150})
    assert absent["owned"] is False
    assert "pid_absent_or_changed" in absent["reason_codes"]

    # Same PID but a different creation time is a textbook PID reuse.
    reused = verify_exact_ownership(
        _identity(), {**_identity(), "process_create_time": 1_700_000_500.0}
    )
    assert reused["owned"] is False
    assert "pid_reuse" in reused["reason_codes"]
    assert "pid_absent_or_changed" not in reused["reason_codes"]


def test_command_line_drift_and_executable_mismatch_refuse_action():
    drifted = verify_exact_ownership(_identity(), {**_identity(), "command_signature": "b" * 64})
    assert "command_line_drift" in drifted["reason_codes"]

    moved = verify_exact_ownership(
        _identity(), {**_identity(), "executable": "D:/elsewhere/server.exe"}
    )
    assert "executable_mismatch" in moved["reason_codes"]

    exact = verify_exact_ownership(_identity(), _identity())
    assert exact == {"owned": True, "reason_codes": []}


def test_incomplete_identity_bindings_fail_closed():
    with pytest.raises(ObservationError):
        verify_exact_ownership({"pid": 1}, {"pid": 1})
    with pytest.raises(ObservationError):
        normalize = None
        from comsol_mcp.jobs.observation import normalize_process_identity

        normalize = normalize_process_identity({**_identity(), "pid": -5})
        assert normalize


# ---------------------------------------------------------------------------
# Observer outcome classification
# ---------------------------------------------------------------------------


def test_verified_terminal_wins_over_everything():
    outcome = classify_observer_outcome(
        terminal_state="completed",
        terminal_identity_verified=True,
        transport_alive=False,
        worker_exit_observed=True,
        deadline_exceeded=True,
    )
    assert outcome == {"outcome": "solver_terminal", "reason_codes": ["solver_terminal"]}


def test_transport_disconnect_is_distinct_from_worker_failure():
    disconnect = classify_observer_outcome(
        terminal_state=None,
        terminal_identity_verified=False,
        transport_alive=False,
        worker_exit_observed=None,
        deadline_exceeded=True,
    )
    assert disconnect["reason_codes"] == ["transport_disconnect"]

    crash = classify_observer_outcome(
        terminal_state=None,
        terminal_identity_verified=False,
        transport_alive=True,
        worker_exit_observed=True,
        deadline_exceeded=False,
    )
    assert crash["reason_codes"] == ["worker_failure"]


def test_observer_timeout_requires_an_expired_deadline():
    timeout = classify_observer_outcome(
        terminal_state=None,
        terminal_identity_verified=False,
        transport_alive=True,
        worker_exit_observed=False,
        deadline_exceeded=True,
    )
    assert timeout["reason_codes"] == ["observer_timeout"]

    running = classify_observer_outcome(
        terminal_state=None,
        terminal_identity_verified=False,
        transport_alive=True,
        worker_exit_observed=False,
        deadline_exceeded=False,
    )
    # Nothing happened yet: the classifier reports the still-running state as
    # a solver-terminal-pending observation rather than inventing an outcome.
    assert running["outcome"] == "solver_terminal"
    assert running["reason_codes"] == ["solver_terminal"]


def test_unverified_terminal_state_is_not_trusted_as_solver_terminal():
    outcome = classify_observer_outcome(
        terminal_state="completed",
        terminal_identity_verified=False,
        transport_alive=True,
        worker_exit_observed=True,
        deadline_exceeded=False,
    )
    assert outcome["reason_codes"] == ["worker_failure"]


# ---------------------------------------------------------------------------
# Receipt assembly, log bounds, resume disposition
# ---------------------------------------------------------------------------


def test_receipt_binds_all_published_fields_and_is_deterministic():
    first = build_observation_receipt(**_receipt())
    second = build_observation_receipt(**_receipt())
    for field in (
        "job_id",
        "attempt",
        "process_identity",
        "observer_started_epoch",
        "deadline_epoch",
        "log_tail",
        "terminal_state",
        "observer_outcome",
        "cleanup_outcome",
        "resume_disposition",
        "receipt_sha256",
    ):
        assert field in first, field
    assert first["process_identity"]["target_files"] == ["**/model.mph", "**/out.csv"]
    assert first == second


def test_partial_log_tail_is_bounded_and_flagged():
    receipt = build_observation_receipt(
        **_receipt(log_tail=[f"line-{index}" for index in range(200)], log_truncated=True)
    )
    assert len(receipt["log_tail"]) <= 64
    assert receipt["log_truncated"] is True


def test_missing_output_is_representable_without_a_timestamp():
    receipt = build_observation_receipt(**_receipt(last_output_epoch=None))
    assert receipt["last_output_epoch"] is None


def test_resume_disposition_requires_a_usable_checkpoint():
    crashed = dict(
        _receipt(
            worker_exit_observed=True,
            cleanup_outcome="failed",
            checkpoint_usable=None,
        )
    )
    receipt = build_observation_receipt(**crashed)
    assert receipt["resume_disposition"] == "restart"
    assert receipt["resume_reason_codes"] == ["checkpoint_unverified"]

    usable = build_observation_receipt(
        **_receipt(worker_exit_observed=True, cleanup_outcome="failed", checkpoint_usable=True)
    )
    assert usable["resume_disposition"] == "resume_from_checkpoint"

    unusable = build_observation_receipt(
        **_receipt(worker_exit_observed=True, cleanup_outcome="failed", checkpoint_usable=False)
    )
    assert unusable["resume_disposition"] == "restart"
    assert unusable["resume_reason_codes"] == ["checkpoint_not_usable"]


def test_timeout_receipt_reports_observer_timeout_with_restart(tmp_path):
    receipt = build_observation_receipt(
        **_receipt(observer_deadline_exceeded=True, last_output_epoch=1_700_000_010.0)
    )
    assert receipt["observer_outcome"] == "observer_timeout"
    assert receipt["resume_disposition"] == "restart"


def test_exact_owner_cleanup_only_when_proven():
    proven = build_observation_receipt(
        **_receipt(cleanup_outcome="proven", checkpoint_usable=False)
    )
    assert proven["cleanup_outcome"] == "proven"
    with pytest.raises(ObservationError):
        build_observation_receipt(**_receipt(cleanup_outcome="probably"))


# ---------------------------------------------------------------------------
# Status surfacing (warning-only)
# ---------------------------------------------------------------------------


def test_job_status_surfaces_the_observation_summary(tmp_path):
    manager = JobManager(root=tmp_path, reconcile_on_start=False)
    job_id = manager.store.create(
        spec={"job_type": "unit_stub"},
        state={"schema_version": 1, "status": "completed", "attempt": 1},
    )
    receipt = build_observation_receipt(**_receipt(job_id=job_id))
    (tmp_path / job_id / OBSERVATION_RECEIPT_FILENAME).write_text(
        json.dumps(receipt), encoding="utf-8"
    )

    result = manager.status(job_id)
    summary = result["observation"]
    assert summary["available"] is True
    assert summary["observer_outcome"] == receipt["observer_outcome"]
    assert summary["resume_disposition"] == receipt["resume_disposition"]

    other = manager.store.create(
        spec={"job_type": "unit_stub"},
        state={"schema_version": 1, "status": "completed", "attempt": 1},
    )
    assert "observation" not in manager.status(other)


def test_corrupt_observation_file_never_changes_job_disposition(tmp_path):
    manager = JobManager(root=tmp_path, reconcile_on_start=False)
    job_id = manager.store.create(
        spec={"job_type": "unit_stub"},
        state={"schema_version": 1, "status": "completed", "attempt": 1},
    )
    (tmp_path / job_id / OBSERVATION_RECEIPT_FILENAME).write_bytes(b"\x00\x01 broken")

    result = manager.status(job_id)
    assert result["success"] is True
    assert result["observation"] == {
        "available": False,
        "reason_code": "observation_receipt_unreadable",
    }


def test_summarize_rejects_shapes_missing_required_fields(tmp_path):
    receipt = build_observation_receipt(**_receipt())
    path = tmp_path / "observation-dir"
    path.mkdir()
    trimmed = {key: value for key, value in receipt.items() if key != "resume_disposition"}
    (path / OBSERVATION_RECEIPT_FILENAME).write_text(json.dumps(trimmed), encoding="utf-8")
    summary = summarize_observation(path)
    assert summary["available"] is False
    assert summary["reason_code"] == "observation_receipt_shape_invalid"


# ---------------------------------------------------------------------------
# Registry membership
# ---------------------------------------------------------------------------


def test_schema_registry_supports_the_published_contract():
    support = check_schema_support(OBSERVATION_RECEIPT_SCHEMA_NAME, OBSERVATION_SCHEMA_VERSION)
    assert support["supported"] is True
    assert support["producer"] == "comsol_mcp.jobs.observation"
