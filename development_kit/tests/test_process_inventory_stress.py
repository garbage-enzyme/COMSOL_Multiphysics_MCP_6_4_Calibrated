"""Host inventory and PID-churn stress without starting COMSOL."""

from __future__ import annotations

import math
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path

import psutil
import pytest
import src.tools.ownership as ownership_module
from src.tools.ownership import SolverOwnership


@pytest.fixture()
def runtime_dir():
    path = Path("D:/comsol_runtime_test/p4_inventory") / uuid.uuid4().hex
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _record(pid: int, created: float, command: list[str], parent_pid: int = 0) -> dict:
    return {
        "pid": pid,
        "parent_pid": parent_pid,
        "name": Path(command[0]).name,
        "create_time": created,
        "command_line": command,
        "executable": command[0],
    }


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def test_synthetic_large_inventory_preserves_external_owner_and_pid_identity(runtime_dir):
    own = _record(900_001, 1000.0, ["python.exe", "-m", "src.server"])
    external = _record(
        900_002,
        1001.0,
        ["python.exe", "-c", "import mph; mph.Client()"],
    )
    ordinary = [
        _record(910_000 + index, 2000.0 + index, [f"worker-{index}.exe"]) for index in range(5_000)
    ]
    inventory = [own, external, *ordinary]
    manager = SolverOwnership(
        runtime_dir,
        process_provider=lambda: list(inventory),
        pid=own["pid"],
        parent_pid=0,
        create_time=own["create_time"],
        command_line=own["command_line"],
        owner="process-inventory-synthetic",
    )
    latencies = []
    for _ in range(40):
        started = time.monotonic()
        status = manager.status()
        latencies.append(time.monotonic() - started)
        assert status["collision"] is True
        assert [item["pid"] for item in status["external_solver_processes"]] == [external["pid"]]
        assert status["external_solver_processes"][0]["process_create_time"] == 1001.0
        assert status["external_solver_processes"][0]["command_line"] == external["command_line"]

    assert statistics.median(latencies) < 0.25
    assert _percentile(latencies, 0.95) < 0.5
    assert max(latencies) < 1.0


def test_reused_lease_pid_fails_closed_without_acting_on_reused_process(runtime_dir):
    original_command = ["python.exe", "owned-server"]
    original = _record(920_001, 3000.0, original_command)
    owner = SolverOwnership(
        runtime_dir,
        process_provider=lambda: [original],
        pid=original["pid"],
        parent_pid=0,
        create_time=original["create_time"],
        command_line=original_command,
        owner="process-inventory-original",
    )
    assert owner.acquire(mode="process-inventory-stress")["success"] is True

    reused = _record(920_001, 3999.0, ["python.exe", "unrelated-process"])
    observer_process = _record(920_002, 3001.0, ["python.exe", "observer"])
    external = _record(920_003, 3002.0, ["python.exe", "-c", "from mph import Client"])
    snapshots = [[observer_process, reused, external], [observer_process, reused, external]]
    provider_calls = 0

    def provider():
        nonlocal provider_calls
        snapshot = snapshots[min(provider_calls, len(snapshots) - 1)]
        provider_calls += 1
        return list(snapshot)

    observer = SolverOwnership(
        runtime_dir,
        process_provider=provider,
        pid=observer_process["pid"],
        parent_pid=0,
        create_time=observer_process["create_time"],
        command_line=observer_process["command_line"],
        owner="process-inventory-observer",
    )
    status = observer.status()

    assert status["lease"]["state"] == "stale"
    assert "PID was reused" in status["lease"]["reason"]
    assert status["collision"] is True
    assert [item["pid"] for item in status["external_solver_processes"]] == [external["pid"]]
    assert observer.acquire(mode="must-refuse")["success"] is False
    assert reused in snapshots[0]
    assert observer.lease_path.exists()


def test_bounded_inventory_timeout_fails_closed_and_cache_cannot_authorize_acquire(
    runtime_dir, monkeypatch
):
    def slow_collision_free_inventory():
        time.sleep(0.2)
        return []

    monkeypatch.setattr(ownership_module, "_system_processes", slow_collision_free_inventory)
    monkeypatch.setattr(ownership_module, "PROCESS_INVENTORY_STATUS_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(ownership_module, "PROCESS_INVENTORY_MUTATION_TIMEOUT_SECONDS", 0.05)
    manager = SolverOwnership(
        runtime_dir,
        pid=os.getpid() + 2_000_000,
        parent_pid=0,
        create_time=1.0,
        command_line=["python.exe", "-m", "src.server"],
        owner="inventory-timeout-timeout-observer",
    )
    started = time.monotonic()
    status = manager.status()

    assert time.monotonic() - started < 0.15
    assert status["process_inventory"]["complete"] is False
    assert status["process_inventory"]["source"] == "unavailable_after_timeout"
    assert status["collision"] is True
    assert manager.acquire(mode="must-require-fresh")["success"] is False
    assert not manager.lease_path.exists()

    time.sleep(0.25)
    cached = manager.status()
    assert cached["process_inventory"]["complete"] is True
    assert cached["process_inventory"]["fresh"] is False
    assert cached["process_inventory"]["source"] == "recent_complete_cache"
    assert manager.acquire(mode="cache-must-not-authorize")["success"] is False
    preflight = manager.preflight()
    assert preflight["ready"] is False
    assert "host process inventory is incomplete" in preflight["blockers"]
    assert not manager.lease_path.exists()


def test_bounded_inventory_rejects_scan_completed_after_request_deadline(monkeypatch):
    original_start = threading.Thread.start

    def delayed_return_from_start(thread):
        original_start(thread)
        time.sleep(0.1)

    def scan_finishing_after_deadline():
        time.sleep(0.08)
        return []

    monkeypatch.setattr(threading.Thread, "start", delayed_return_from_start)
    inventory = ownership_module._BoundedProcessInventory(scan_finishing_after_deadline)

    records, evidence = inventory.collect(require_fresh=True, timeout=0.05)

    assert records == []
    assert evidence["complete"] is False
    assert evidence["fresh"] is False
    assert evidence["source"] == "stale_cache_after_timeout"


def test_read_only_recheck_accepts_scan_that_was_already_in_flight(runtime_dir, monkeypatch):
    def briefly_slow_inventory():
        time.sleep(0.08)
        return []

    monkeypatch.setattr(ownership_module, "_system_processes", briefly_slow_inventory)
    manager = SolverOwnership(
        runtime_dir,
        pid=os.getpid() + 2_000_001,
        parent_pid=0,
        create_time=1.0,
        command_line=["python.exe", "-m", "src.server"],
        owner="inventory-inflight-observer",
    )

    first = manager.status(inventory_timeout=0.05)
    second = manager.status(inventory_timeout=0.05)

    assert first["process_inventory"]["complete"] is False
    assert first["process_inventory"]["source"] == "unavailable_after_timeout"
    assert second["process_inventory"]["complete"] is True
    assert second["process_inventory"]["fresh"] is False
    assert second["process_inventory"]["source"] == "completed_inflight_scan"
    assert second["collision"] is False


def test_host_inventory_skips_expensive_metadata_for_unrelated_processes(monkeypatch):
    class UnrelatedProcess:
        pid = 77

        def oneshot(self):
            return nullcontext()

        def name(self):
            return "unrelated-gui.exe"

        def ppid(self):
            return 7

        def create_time(self):
            return 70.0

        def cmdline(self):
            pytest.fail("unrelated command lines must not be queried")

        def exe(self):
            pytest.fail("executables are not required for collision classification")

    assert ownership_module._process_record(UnrelatedProcess()) == {
        "pid": 77,
        "parent_pid": 7,
        "name": "unrelated-gui.exe",
        "create_time": 70.0,
        "command_line": [],
        "executable": None,
    }


def test_host_inventory_fails_closed_when_solver_process_is_inaccessible(monkeypatch):
    class InaccessibleProcess:
        pid = 88

        def cmdline(self):
            raise psutil.AccessDenied(pid=self.pid)

        def create_time(self):
            return 88.0

    monkeypatch.setattr(
        ownership_module,
        "_windows_process_table",
        lambda: [(88, 0, "comsolmphserver.exe")],
    )
    monkeypatch.setattr(ownership_module.psutil, "Process", lambda _pid: InaccessibleProcess())

    with pytest.raises(RuntimeError, match="inspection failed for PID 88"):
        ownership_module._system_processes()


def test_independent_cold_observers_prove_stale_lease_without_full_inventory(
    runtime_dir, monkeypatch
):
    original = _record(920_101, 3000.0, ["python.exe", "owned-server"])
    owner = SolverOwnership(
        runtime_dir,
        process_provider=lambda: [original],
        pid=original["pid"],
        parent_pid=0,
        create_time=original["create_time"],
        command_line=original["command_line"],
        owner="cold-start-original",
    )
    assert owner.acquire(mode="cold-start-test")["success"] is True

    def slow_inventory():
        time.sleep(0.2)
        return []

    monkeypatch.setattr(ownership_module, "_system_processes", slow_inventory)
    monkeypatch.setattr(ownership_module, "PROCESS_INVENTORY_STATUS_TIMEOUT_SECONDS", 0.05)

    observations = []
    for index in range(5):
        observer = SolverOwnership(
            runtime_dir,
            pid=930_000 + index,
            parent_pid=0,
            create_time=4000.0 + index,
            command_line=["python.exe", f"observer-{index}"],
            owner=f"cold-start-observer-{index}",
        )
        status = observer.status()
        observations.append(status)

    assert all(status["process_inventory"]["complete"] is False for status in observations)
    assert all(status["lease"]["state"] == "stale" for status in observations)
    assert all(
        status["lease"]["identity_source"] == "targeted_process_probe" for status in observations
    )
    assert all(status["collision"] is True for status in observations)
    time.sleep(0.25)


def test_real_host_inventory_retains_marker_during_short_process_churn(runtime_dir):
    marker = subprocess.Popen(
        [sys.executable, "-c", "import time; marker='mph.Client'; time.sleep(30)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    created = psutil.Process(marker.pid).create_time()
    churned: list[subprocess.Popen] = []

    def churn() -> None:
        for _ in range(20):
            process = subprocess.Popen(
                [sys.executable, "-c", "pass"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            churned.append(process)
            process.wait(timeout=10)

    manager = SolverOwnership(
        runtime_dir,
        pid=os.getpid() + 1_000_000,
        parent_pid=0,
        create_time=1.0,
        command_line=["python.exe", "-m", "src.server"],
        owner="process-inventory-real-observer",
    )
    latencies = []
    complete_scans = 0
    incomplete_scans = 0
    marker_observations = 0
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            churn_future = executor.submit(churn)
            for _ in range(12):
                started = time.monotonic()
                status = manager.status()
                latencies.append(time.monotonic() - started)
                matches = [
                    item
                    for item in status["external_solver_processes"]
                    if item["pid"] == marker.pid
                ]
                assert status["collision"] is True
                if status["process_inventory"]["complete"]:
                    complete_scans += 1
                    assert len(matches) == 1
                    marker_observations += 1
                    assert abs(matches[0]["process_create_time"] - created) < 0.05
                    assert any("mph.Client" in part for part in matches[0]["command_line"])
                else:
                    incomplete_scans += 1
                    assert status["process_inventory"]["source"] in {
                        "unavailable_after_timeout",
                        "stale_cache_after_timeout",
                    }
            churn_future.result(timeout=30)
    finally:
        marker.terminate()
        marker.wait(timeout=10)
        for process in churned:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)

    assert statistics.median(latencies) < 1.0
    assert _percentile(latencies, 0.95) < 3.0
    assert max(latencies) < 5.0
    assert complete_scans >= 1
    assert marker_observations == complete_scans
    assert incomplete_scans >= 0
    assert not manager.lease_path.exists()
