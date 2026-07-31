"""Acceptance tests for shared operations reusing v3 arbiter and path policy."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import threading
import time

import pytest

from src.operation_arbiter import OperationArbiter, get_operation_status, guard_tool_call
from src.path_policy import ARTIFACT_WRITE_ROOT_ENV, MODEL_READ_ROOTS_ENV, PathPolicy


@pytest.fixture
def ascii_root():
    base = Path("D:/comsol_runtime") if Path("D:/").exists() else Path(
        os.environ.get("SystemRoot", "C:/Windows")
    ) / "Temp"
    root = Path(tempfile.mkdtemp(prefix="comsol_mcp_shared_dependencies_", dir=base))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


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
        profile_name="desktop_shared",
    )
    second = guard_tool_call(
        lambda: {"success": True},
        tool_name="shared_model_mutate_fixture",
        side_effect_class="model_mutation",
        concurrency_class="comsol_bound",
        profile_name="desktop_shared",
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


def test_status_and_cancel_remain_responsive_during_shared_solve(tmp_path, monkeypatch):
    arbiter = _arbiter(tmp_path, monkeypatch)
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
        profile_name="desktop_shared",
    )
    status_tool = guard_tool_call(
        lambda: {"success": True, "operation": get_operation_status()},
        tool_name="shared_server_status_fixture",
        side_effect_class="read_only",
        concurrency_class="control_plane",
        profile_name="desktop_shared",
    )
    cancel_calls = []
    cancel_tool = guard_tool_call(
        lambda: cancel_calls.append(True) or {"success": True, "requested": True},
        tool_name="job_cancel_fixture",
        side_effect_class="job_control",
        concurrency_class="control_plane",
        profile_name="desktop_shared",
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
    try:
        assert entered.wait(1.0)
        started = time.perf_counter()
        status = status_tool()
        cancel = cancel_tool()
        elapsed = time.perf_counter() - started
    finally:
        release.set()
        worker.join(2.0)
        assert not worker.is_alive()
    if worker_errors:
        raise worker_errors[0]

    assert elapsed < 0.2
    assert status["success"] is True
    assert status["operation"]["state"] == "active"
    assert status["operation"]["active_operation"]["tool_name"] == (
        "shared_model_solve_fixture"
    )
    assert cancel == {
        "success": True,
        "requested": True,
        "path_policy": {
            **cancel["path_policy"],
        },
    }
    assert cancel_calls == [True]
    assert solve_result["success"] is True
    assert not arbiter.lock_path.exists()


def test_shared_snapshot_path_stress_has_no_external_write(tmp_path, ascii_root):
    read_root = tmp_path / "models"
    read_root.mkdir()
    write_root = ascii_root / "owned"
    policy = PathPolicy.from_environment({
        MODEL_READ_ROOTS_ENV: str(read_root),
        ARTIFACT_WRITE_ROOT_ENV: str(write_root),
    })
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
