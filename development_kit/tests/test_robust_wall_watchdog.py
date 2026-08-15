"""Deterministic tests for the licensed robust wall watchdog."""

from __future__ import annotations

import os

import pytest

from comsol_mcp.jobs import manager as manager_module
from comsol_mcp.jobs.manager import JobManager
from comsol_mcp.jobs.robust_shape_worker import _await_licensed_wall_watchdog
from comsol_mcp.jobs.robust_wall_watchdog import run
from comsol_mcp.jobs.store import JobStore, atomic_write_json, process_identity, read_json


class _Clock:
    def __init__(self, value: float = 100.0):
        self.value = value

    def time(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def _prepare(
    root,
    *,
    state_attempt: int = 1,
    artifact_attempt: int = 1,
    status: str = "running",
    budget: int = 5,
    deadline: float = 105.0,
):
    store = JobStore(root)
    worker = process_identity(os.getpid())
    spec = {
        "job_type": "robust_shape_optimization",
        "synthetic_mode": False,
        "native_optimizer": {"budget": {"max_wall_time_seconds": budget}},
    }
    state = {
        "schema_version": "2",
        "status": status,
        "attempt": state_attempt,
        "worker_pid": worker["pid"],
        "worker_process_create_time": worker["process_create_time"],
        "worker_command_signature": worker["command_signature"],
    }
    job_id = store.create(spec, state)
    atomic_write_json(
        store.job_dir(job_id) / "wall-watchdog.json",
        {
            "schema_name": "comsol_mcp.robust_wall_watchdog",
            "schema_version": "1.0.0",
            "job_id": job_id,
            "attempt": artifact_attempt,
            "budget_seconds": budget,
            "deadline_epoch": deadline,
            "status": "armed",
        },
    )
    return store, job_id


def test_terminal_before_deadline_exits_without_cancellation(ascii_tmp_path):
    clock = _Clock()
    store, job_id = _prepare(ascii_tmp_path / "jobs", status="completed")

    assert (
        run(
            store.root,
            job_id,
            1,
            105.0,
            clock=clock.time,
            sleep=clock.sleep,
            manager_factory=lambda *_args, **_kwargs: pytest.fail("cancel was called"),
        )
        == 0
    )
    receipt = read_json(store.job_dir(job_id) / "wall-watchdog.json")
    assert receipt["status"] == "terminal_before_deadline"
    assert receipt["terminal_job_status"] == "completed"


def test_stale_attempt_is_refused_without_touching_new_attempt(ascii_tmp_path):
    clock = _Clock()
    store, job_id = _prepare(ascii_tmp_path / "jobs", state_attempt=2)

    assert (
        run(
            store.root,
            job_id,
            1,
            105.0,
            clock=clock.time,
            sleep=clock.sleep,
            manager_factory=lambda *_args, **_kwargs: pytest.fail("cancel was called"),
        )
        == 0
    )
    receipt = read_json(store.job_dir(job_id) / "wall-watchdog.json")
    assert receipt["status"] == "stale_attempt_refused"
    assert receipt["observed_attempt"] == 2
    assert store.read_state(job_id)["status"] == "running"


def test_deadline_requests_cancellation_for_exact_attempt(ascii_tmp_path):
    clock = _Clock()
    store, job_id = _prepare(ascii_tmp_path / "jobs")
    observed = []

    class _Manager:
        def cancel(self, target_job_id, *, expected_attempt=None):
            observed.append((target_job_id, expected_attempt))
            return {
                "success": True,
                "job_id": target_job_id,
                "status": "cancel_requested",
                "request_id": "cancel-watchdog",
                "target_attempt": expected_attempt,
            }

    assert (
        run(
            store.root,
            job_id,
            1,
            105.0,
            clock=clock.time,
            sleep=clock.sleep,
            manager_factory=lambda *_args, **_kwargs: _Manager(),
        )
        == 0
    )
    assert observed == [(job_id, 1)]
    receipt = read_json(store.job_dir(job_id) / "wall-watchdog.json")
    assert receipt["status"] == "cancellation_requested"
    assert receipt["deadline_reached_at_epoch"] == 105.0
    assert receipt["cancellation"]["target_attempt"] == 1


def test_store_refuses_expected_attempt_mismatch_atomically(ascii_tmp_path):
    store, job_id = _prepare(ascii_tmp_path / "jobs", state_attempt=2)

    result = store.request_cancel(
        job_id,
        requester_identity=process_identity(os.getpid()),
        expected_attempt=1,
    )

    assert result["accepted"] is False
    assert result["reason"] == "attempt_mismatch"
    assert store.read_state(job_id)["status"] == "running"
    assert store.read_control(job_id)["request"] is None


def test_watchdog_launch_failure_is_recorded_durably(ascii_tmp_path, monkeypatch):
    store, job_id = _prepare(ascii_tmp_path / "jobs", status="submitted")
    manager = JobManager(store.root, reconcile_on_start=False)
    spec = store.read_spec(job_id)
    worker = process_identity(os.getpid())
    monkeypatch.setattr(
        manager_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("launch refused")),
    )

    with pytest.raises(OSError, match="launch refused"):
        manager._arm_robust_wall_watchdog_if_required(
            job_id,
            spec,
            attempt=1,
            worker_identity=worker,
        )

    receipt = read_json(store.job_dir(job_id) / "wall-watchdog.json")
    assert receipt["status"] == "launch_failed"
    assert receipt["launch_error"] == {
        "type": "OSError",
        "message": "launch refused",
    }


def test_synthetic_job_does_not_launch_watchdog(ascii_tmp_path, monkeypatch):
    store, job_id = _prepare(ascii_tmp_path / "jobs", status="submitted")
    manager = JobManager(store.root, reconcile_on_start=False)
    spec = store.read_spec(job_id)
    spec["synthetic_mode"] = True
    monkeypatch.setattr(
        manager_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("watchdog process was launched"),
    )

    assert (
        manager._arm_robust_wall_watchdog_if_required(
            job_id,
            spec,
            attempt=1,
            worker_identity=process_identity(os.getpid()),
        )
        is None
    )


def test_licensed_worker_refuses_startup_without_armed_watchdog(ascii_tmp_path):
    store, job_id = _prepare(ascii_tmp_path / "jobs", status="starting")
    (store.job_dir(job_id) / "wall-watchdog.json").unlink()
    clock = _Clock()

    with pytest.raises(RuntimeError, match="was not armed"):
        _await_licensed_wall_watchdog(
            store,
            job_id,
            store.read_spec(job_id),
            1,
            timeout_seconds=0.1,
            monotonic=clock.time,
            sleep=clock.sleep,
            wall_clock=clock.time,
        )
