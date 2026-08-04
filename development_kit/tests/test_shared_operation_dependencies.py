"""Acceptance tests for shared operations reusing v3 arbiter and path policy."""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

import pytest
from src.operation_arbiter import OperationArbiter, get_operation_status, guard_tool_call
from src.path_policy import ARTIFACT_WRITE_ROOT_ENV, MODEL_READ_ROOTS_ENV, PathPolicy


@pytest.fixture
def ascii_root(ascii_tmp_path: Path):
    root = ascii_tmp_path / "shared_dependencies"
    root.mkdir()
    return root


def _arbiter(tmp_path, monkeypatch):
    value = OperationArbiter(
        tmp_path,
        pid=100,
        process_create_time=10.0,
        process_probe=lambda pid: 10.0,
    )
    monkeypatch.setattr("src.operation_arbiter.get_operation_arbiter", lambda: value)
    return value


def test_two_shared_model_mutations_cannot_overlap(tmp_path, monkeypatch):
    arbiter = _arbiter(tmp_path, monkeypatch)
    entered = threading.Event()
    release = threading.Event()

    def first_mutation():
        entered.set()
        assert release.wait(2.0)
        return {"success": True}

    first = guard_tool_call(
        first_mutation,
        tool_name="shared_model_mutate_fixture",
        side_effect_class="model_mutation",
        concurrency_class="comsol_bound",
        profile_name="core",
    )
    second = guard_tool_call(
        lambda: {"success": True},
        tool_name="shared_model_mutate_fixture",
        side_effect_class="model_mutation",
        concurrency_class="comsol_bound",
        profile_name="core",
    )
    result: dict = {}
    worker_errors = []

    def run_first():
        try:
            result.update(first())
        except BaseException as exc:
            worker_errors.append(exc)

    worker = threading.Thread(target=run_first)
    worker.start()
    try:
        assert entered.wait(1.0)
        rejected = second()
    finally:
        release.set()
        worker.join(2.0)
        assert not worker.is_alive()
    if worker_errors:
        raise worker_errors[0]

    assert rejected["success"] is False
    assert rejected["operation_gate"]["active_operation"]["tool_name"] == (
        "shared_model_mutate_fixture"
    )
    assert result["success"] is True
    assert result["operation_gate"]["release"]["verified"] is True
    assert not arbiter.lock_path.exists()


def test_status_and_cancel_remain_responsive_during_shared_solve(ascii_tmp_path, monkeypatch):
    test_root = ascii_tmp_path / "responsive-control-plane"
    test_root.mkdir()
    read_root = test_root / "models"
    write_root = test_root / "artifacts"
    read_root.mkdir()
    write_root.mkdir()
    monkeypatch.setenv(MODEL_READ_ROOTS_ENV, str(read_root))
    monkeypatch.setenv(ARTIFACT_WRITE_ROOT_ENV, str(write_root))
    arbiter = _arbiter(test_root, monkeypatch)
    entered = threading.Event()
    release = threading.Event()

    def solve():
        entered.set()
        assert release.wait(2.0)
        return {"success": True}

    solve_tool = guard_tool_call(
        solve,
        tool_name="shared_model_solve_fixture",
        side_effect_class="solver_execution",
        concurrency_class="comsol_bound",
        profile_name="core",
    )
    status_tool = guard_tool_call(
        lambda: {"success": True, "operation": get_operation_status()},
        tool_name="shared_server_status_fixture",
        side_effect_class="read_only",
        concurrency_class="control_plane",
        profile_name="core",
    )
    cancel_calls = []
    cancel_tool = guard_tool_call(
        lambda: cancel_calls.append(True) or {"success": True, "requested": True},
        tool_name="job_cancel_fixture",
        side_effect_class="job_control",
        concurrency_class="control_plane",
        profile_name="core",
    )
    solve_result: dict = {}
    worker_errors = []

    def run_solve():
        try:
            solve_result.update(solve_tool())
        except BaseException as exc:
            worker_errors.append(exc)

    worker = threading.Thread(target=run_solve)
    worker.start()

    def bounded_call(function):
        outcomes = queue.Queue(maxsize=1)

        def invoke():
            try:
                outcomes.put((True, function()))
            except BaseException as exc:
                outcomes.put((False, exc))

        started = time.monotonic()
        caller = threading.Thread(target=invoke, daemon=True)
        caller.start()
        caller.join(1.0)
        elapsed = time.monotonic() - started
        assert not caller.is_alive(), "control-plane call exceeded one second"
        succeeded, outcome = outcomes.get_nowait()
        if not succeeded:
            raise outcome
        return outcome, elapsed

    try:
        assert entered.wait(1.0)
        assert worker.is_alive()
        status, status_elapsed = bounded_call(status_tool)
        cancel, cancel_elapsed = bounded_call(cancel_tool)
        assert worker.is_alive()
    finally:
        release.set()
        worker.join(2.0)
        assert not worker.is_alive()
    if worker_errors:
        raise worker_errors[0]

    assert status["success"] is True
    assert status["operation"]["state"] == "active"
    assert status["operation"]["active_operation"]["tool_name"] == ("shared_model_solve_fixture")
    assert cancel == {
        "success": True,
        "requested": True,
        "path_policy": {
            "schema_name": "comsol_mcp.path_policy",
            "schema_version": "1.1.0",
            "enforced": True,
            "accepted": True,
            "validated_input_count": 0,
            "validated_kinds": [],
            "paths_included": False,
            "model_read_roots_configured": 1,
            "shared_source_roots_configured": 1,
            "root_ids": [],
            "artifact_write_root_ascii": True,
            "shared_snapshot_root_owned": True,
            "shared_snapshot_root_ascii": True,
            "caller_selected_overwrite_allowed": False,
        },
    }
    assert cancel_calls == [True]
    assert status_elapsed < 1.0
    assert cancel_elapsed < 1.0
    assert solve_result["success"] is True
    assert not arbiter.lock_path.exists()


def test_shared_snapshot_path_stress_has_no_external_write(tmp_path, ascii_root):
    read_root = tmp_path / "models"
    read_root.mkdir()
    write_root = ascii_root / "owned"
    policy = PathPolicy.from_environment(
        {
            MODEL_READ_ROOTS_ENV: str(read_root),
            ARTIFACT_WRITE_ROOT_ENV: str(write_root),
        }
    )
    external = ascii_root / "external"
    external.mkdir()
    sentinel = external / "sentinel.mph"
    sentinel.write_bytes(b"unchanged")
    existing = write_root / "shared_snapshots" / "Existing.mph"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(b"existing")
    candidates = [
        str(external / "new.mph"),
        str(write_root / "shared_snapshots" / "CON.mph"),
        str(write_root / "shared_snapshots" / "结果.mph"),
        r"\\?\D:\shared_snapshots\device.mph",
        str(write_root / "shared_snapshots" / "existing.mph"),
    ]

    for candidate in candidates:
        with pytest.raises(ValueError):
            policy.validate_shared_snapshot_write(candidate)

    assert sentinel.read_bytes() == b"unchanged"
    assert existing.read_bytes() == b"existing"
    assert not (external / "new.mph").exists()
