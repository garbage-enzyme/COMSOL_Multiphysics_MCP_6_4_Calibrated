"""Process-only tests for solver ownership and collision detection."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

import src.tools.ownership as ownership_module
from src.tools.ownership import SolverOwnership, _command_signature


@pytest.fixture()
def runtime_dir():
    path = Path("D:/comsol_runtime_test") / uuid.uuid4().hex
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def process(pid: int, created: float, command: list[str], parent_pid: int = 0):
    return {
        "pid": pid,
        "parent_pid": parent_pid,
        "name": Path(command[0]).name,
        "create_time": created,
        "command_line": command,
        "executable": command[0],
    }


def owner(runtime_dir: Path, pid: int, created: float, command: list[str], records):
    return SolverOwnership(
        runtime_dir,
        process_provider=lambda: list(records),
        pid=pid,
        parent_pid=0,
        create_time=created,
        command_line=command,
        owner=f"test-{pid}",
    )


def test_external_mph_client_blocks_acquisition(runtime_dir):
    own = process(10, 100.0, ["python.exe", "-m", "src.server"])
    external = process(20, 200.0, ["python.exe", "-c", "import mph; mph.Client()"])
    manager = owner(runtime_dir, 10, 100.0, own["command_line"], [own, external])

    status = manager.status()
    claim = manager.acquire(mode="local-client")

    assert status["collision"] is True
    assert status["external_solver_processes"][0]["pid"] == 20
    assert claim["success"] is False
    assert not manager.lease_path.exists()


def test_parent_control_script_is_not_an_external_solver(runtime_dir):
    parent = process(9, 90.0, ["python.exe", "probe_that_mentions_mph.Client.py"])
    own = process(10, 100.0, ["python.exe", "-m", "src.server"], parent_pid=9)
    manager = owner(runtime_dir, 10, 100.0, own["command_line"], [parent, own])

    assert manager.status()["external_solver_processes"] == []


def test_two_simultaneous_claims_produce_one_owner(runtime_dir):
    first_process = process(11, 101.0, ["python.exe", "server-a"])
    second_process = process(12, 102.0, ["python.exe", "server-b"])
    records = [first_process, second_process]
    first = owner(runtime_dir, 11, 101.0, first_process["command_line"], records)
    second = owner(runtime_dir, 12, 102.0, second_process["command_line"], records)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda manager: manager.acquire(mode="local-client"), [first, second]))

    assert sum(result["success"] for result in results) == 1
    assert sum(result.get("acquired", False) for result in results) == 1


def test_pid_reuse_is_stale_and_requires_explicit_recovery(runtime_dir):
    original_command = ["python.exe", "owner"]
    original = process(21, 201.0, original_command)
    first = owner(runtime_dir, 21, 201.0, original_command, [original])
    assert first.acquire(mode="local-client")["success"] is True

    reused = process(21, 999.0, ["python.exe", "unrelated"])
    observer_process = process(22, 202.0, ["python.exe", "observer"])
    observer = owner(
        runtime_dir,
        22,
        202.0,
        observer_process["command_line"],
        [observer_process, reused],
    )

    assert observer.status()["lease"]["state"] == "stale"
    assert observer.acquire(mode="local-client")["success"] is False
    recovered = observer.recover_stale()
    assert recovered["success"] is True
    assert recovered["recovered"] is True
    assert not observer.lease_path.exists()


def test_recovery_refuses_active_foreign_lease(runtime_dir):
    foreign_command = ["python.exe", "foreign-server"]
    foreign_process = process(31, 301.0, foreign_command)
    foreign = owner(runtime_dir, 31, 301.0, foreign_command, [foreign_process])
    assert foreign.acquire(mode="local-client")["success"] is True

    observer_process = process(32, 302.0, ["python.exe", "observer"])
    observer = owner(
        runtime_dir,
        32,
        302.0,
        observer_process["command_line"],
        [foreign_process, observer_process],
    )
    result = observer.recover_stale()

    assert result["success"] is False
    assert "only a proven stale lease" in result["error"]
    assert observer.lease_path.exists()


def test_lease_is_active_for_same_os_process_identity(runtime_dir):
    command = ["python.exe", "-m", "src.server"]
    own_process = process(41, 401.0, command)
    manager = owner(runtime_dir, 41, 401.0, command, [own_process])

    assert manager.acquire(mode="local-client")["success"] is True
    lease = manager.status()["lease"]

    assert lease["state"] == "active"
    assert lease["owned_by_current_process"] is True
    assert lease["lease"]["command_signature"] == _command_signature(command)


def test_heartbeat_records_owned_comsol_server_pid(runtime_dir):
    command = ["python.exe", "-m", "src.server"]
    own_process = process(51, 501.0, command)
    server_process = process(
        52,
        502.0,
        ["comsolmphserver.exe", "-port", "2036"],
        parent_pid=51,
    )
    records = [own_process, server_process]
    manager = owner(runtime_dir, 51, 501.0, command, records)
    assert manager.acquire(mode="local-client")["success"] is True

    assert manager.heartbeat(refresh_server_processes=True) is True
    lease = manager.status()["lease"]["lease"]

    assert lease["comsol_server_pids"] == [52]
    assert lease["comsol_server_processes"] == [
        {
            "pid": 52,
            "process_create_time": 502.0,
            "command_signature": _command_signature(["comsolmphserver.exe", "-port", "2036"]),
        }
    ]


def test_lease_read_retries_transient_windows_sharing_violation(runtime_dir, monkeypatch):
    command = ["python.exe", "-m", "src.server"]
    own_process = process(61, 601.0, command)
    manager = owner(runtime_dir, 61, 601.0, command, [own_process])
    assert manager.acquire(mode="local-client")["success"] is True
    original_read_bytes = Path.read_bytes
    attempts = 0

    def flaky_read_bytes(path):
        nonlocal attempts
        if path == manager.lease_path and attempts < 2:
            attempts += 1
            raise PermissionError("simulated lease reader sharing violation")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", flaky_read_bytes)
    status = manager.status()

    assert status["lease"]["state"] == "active"
    assert attempts == 2


def test_persistent_lease_read_failure_is_bounded_and_fails_closed(runtime_dir, monkeypatch):
    command = ["python.exe", "-m", "src.server"]
    own_process = process(611, 601.1, command)
    manager = owner(runtime_dir, 611, 601.1, command, [own_process])
    assert manager.acquire(mode="local-client")["success"] is True
    original_read_bytes = Path.read_bytes

    def blocked_read_bytes(path):
        if path == manager.lease_path:
            raise PermissionError("persistent simulated lease reader sharing violation")
        return original_read_bytes(path)

    monkeypatch.setattr(ownership_module, "LEASE_IO_TIMEOUT_SECONDS", 0.08)
    monkeypatch.setattr(ownership_module, "LEASE_IO_POLL_SECONDS", 0.005)
    monkeypatch.setattr(Path, "read_bytes", blocked_read_bytes)
    started = time.monotonic()
    status = manager.status()

    assert time.monotonic() - started < 0.5
    assert status["lease"]["state"] == "uncertain"
    assert status["collision"] is True
    assert "Cannot read solver lease" in status["lease"]["reason"]


def test_heartbeat_retries_atomic_replace_and_leaves_no_temp(runtime_dir, monkeypatch):
    command = ["python.exe", "-m", "src.server"]
    own_process = process(62, 602.0, command)
    manager = owner(runtime_dir, 62, 602.0, command, [own_process])
    assert manager.acquire(mode="local-client")["success"] is True
    original_replace = ownership_module.os.replace
    attempts = 0

    def flaky_replace(source, destination):
        nonlocal attempts
        if destination == manager.lease_path and attempts < 2:
            attempts += 1
            raise PermissionError("simulated lease replace sharing violation")
        return original_replace(source, destination)

    monkeypatch.setattr(ownership_module.os, "replace", flaky_replace)

    assert manager.heartbeat() is True
    assert attempts == 2
    assert not list(runtime_dir.glob(".solver_owner.json.*.tmp"))


def test_heartbeat_never_overwrites_lease_changed_during_replace_retry(runtime_dir, monkeypatch):
    command = ["python.exe", "-m", "src.server"]
    own_process = process(63, 603.0, command)
    manager = owner(runtime_dir, 63, 603.0, command, [own_process])
    assert manager.acquire(mode="local-client")["success"] is True
    original_replace = ownership_module.os.replace
    changed = False

    def competing_replace(source, destination):
        nonlocal changed
        if destination == manager.lease_path and not changed:
            changed = True
            payload = json.loads(destination.read_text(encoding="utf-8"))
            payload["acquisition_id"] = "competing-owner"
            destination.write_text(json.dumps(payload), encoding="utf-8")
            raise PermissionError("simulated collision with a competing heartbeat")
        return original_replace(source, destination)

    monkeypatch.setattr(ownership_module.os, "replace", competing_replace)

    assert manager.heartbeat() is False
    assert json.loads(manager.lease_path.read_text(encoding="utf-8"))["acquisition_id"] == "competing-owner"
    assert not list(runtime_dir.glob(".solver_owner.json.*.tmp"))


def test_release_retries_unlink_but_refuses_changed_lease(runtime_dir, monkeypatch):
    command = ["python.exe", "-m", "src.server"]
    own_process = process(64, 604.0, command)
    manager = owner(runtime_dir, 64, 604.0, command, [own_process])
    assert manager.acquire(mode="local-client")["success"] is True
    original_unlink = Path.unlink
    attempts = 0

    def flaky_unlink(path, missing_ok=False):
        nonlocal attempts
        if path == manager.lease_path and attempts < 2:
            attempts += 1
            raise PermissionError("simulated lease unlink sharing violation")
        return original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    assert manager.release() == {"success": True, "released": True}
    assert attempts == 2

    assert manager.acquire(mode="local-client")["success"] is True
    changed = False

    def competing_unlink(path, missing_ok=False):
        nonlocal changed
        if path == manager.lease_path and not changed:
            changed = True
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["acquisition_id"] = "competing-owner"
            path.write_text(json.dumps(payload), encoding="utf-8")
            raise PermissionError("simulated collision with a competing release")
        return original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", competing_unlink)
    refused = manager.release()

    assert refused["success"] is False
    assert "changed before release" in refused["error"]
    assert manager.lease_path.exists()


def test_stale_recovery_retries_exact_lease_unlink(runtime_dir, monkeypatch):
    original_command = ["python.exe", "owner"]
    original_process = process(66, 606.0, original_command)
    first = owner(runtime_dir, 66, 606.0, original_command, [original_process])
    assert first.acquire(mode="local-client")["success"] is True
    observer_process = process(67, 607.0, ["python.exe", "observer"])
    reused = process(66, 999.0, ["python.exe", "unrelated"])
    observer = owner(
        runtime_dir,
        67,
        607.0,
        observer_process["command_line"],
        [observer_process, reused],
    )
    original_unlink = Path.unlink
    attempts = 0

    def flaky_unlink(path, missing_ok=False):
        nonlocal attempts
        if path == observer.lease_path and attempts < 2:
            attempts += 1
            raise PermissionError("simulated stale-lease unlink sharing violation")
        return original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    recovered = observer.recover_stale()

    assert recovered["success"] is True
    assert recovered["recovered"] is True
    assert attempts == 2
    assert not observer.lease_path.exists()


def test_persistent_replace_failure_is_bounded_and_temp_is_cleaned(runtime_dir, monkeypatch):
    command = ["python.exe", "-m", "src.server"]
    own_process = process(65, 605.0, command)
    manager = owner(runtime_dir, 65, 605.0, command, [own_process])
    assert manager.acquire(mode="local-client")["success"] is True
    original_unlink = Path.unlink
    cleanup_attempts = 0

    def blocked_replace(source, destination):
        raise PermissionError("persistent simulated replace sharing violation")

    def flaky_temp_unlink(path, missing_ok=False):
        nonlocal cleanup_attempts
        if path.name.startswith(".solver_owner.json.") and cleanup_attempts < 2:
            cleanup_attempts += 1
            raise PermissionError("transient simulated temp cleanup sharing violation")
        return original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(ownership_module, "LEASE_IO_TIMEOUT_SECONDS", 0.08)
    monkeypatch.setattr(ownership_module, "LEASE_IO_POLL_SECONDS", 0.005)
    monkeypatch.setattr(ownership_module.os, "replace", blocked_replace)
    monkeypatch.setattr(Path, "unlink", flaky_temp_unlink)
    started = time.monotonic()

    assert manager.heartbeat() is False

    assert time.monotonic() - started < 0.5
    assert cleanup_attempts == 2
    assert manager.lease_path.exists()
    assert not list(runtime_dir.glob(".solver_owner.json.*.tmp"))


def test_real_process_evidence_refuses_known_external_client(runtime_dir):
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; marker='mph.Client'; time.sleep(30)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        # Host-wide process inventory can be slow while an unrelated COMSOL
        # factorization is active. Keep the marker alive beyond the bounded
        # discovery window so scheduling delay cannot erase the evidence.
        deadline = time.monotonic() + 10
        detected = None
        manager = SolverOwnership(
            runtime_dir,
            pid=os.getpid() + 100000,
            parent_pid=0,
            create_time=1.0,
            command_line=["python.exe", "-m", "src.server"],
            owner="independent-mcp-observer",
        )
        while time.monotonic() < deadline:
            detected = manager.status()
            if any(item["pid"] == child.pid for item in detected["external_solver_processes"]):
                break
            time.sleep(0.05)
        assert detected is not None
        assert any(item["pid"] == child.pid for item in detected["external_solver_processes"])
        assert manager.acquire(mode="local-client")["success"] is False
    finally:
        child.terminate()
        child.wait(timeout=5)
