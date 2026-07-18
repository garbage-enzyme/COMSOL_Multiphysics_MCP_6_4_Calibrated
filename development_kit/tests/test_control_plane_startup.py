"""Cold-process control-plane discovery and startup-budget checks."""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
from pathlib import Path


_CHILD_PROBE = r'''
import json
import os
import psutil
import sys
import time

process = psutil.Process(os.getpid())
before_pids = {item.pid for item in psutil.process_iter()}
process_start_rss = process.memory_info().rss
import_started = time.perf_counter()
from src.server import create_server
import_finished = time.perf_counter()
import_rss = process.memory_info().rss
create_started = time.perf_counter()
server = create_server("cold-control-plane", profile="core")
create_finished = time.perf_counter()
after_pids = {item.pid for item in psutil.process_iter()}
heavy_roots = ("mph", "jpype", "numpy", "scipy", "matplotlib")
heavy_modules = sorted(
    name for name in sys.modules
    if any(name == root or name.startswith(root + ".") for root in heavy_roots)
)
new_external_processes = []
for pid in sorted(after_pids - before_pids):
    try:
        item = psutil.Process(pid)
        command = " ".join(item.cmdline()).casefold()
        name = item.name().casefold()
    except (psutil.Error, OSError):
        continue
    if any(token in (name + " " + command) for token in ("comsol", "mphserver")):
        new_external_processes.append(pid)
print(json.dumps({
    "import_seconds": import_finished - import_started,
    "create_seconds": create_finished - create_started,
    "rss_from_process_start_mib": (process.memory_info().rss - process_start_rss) / 1048576,
    "rss_from_server_import_mib": (process.memory_info().rss - import_rss) / 1048576,
    "heavy_modules": heavy_modules,
    "new_external_processes": new_external_processes,
    "tool_count": len(server._tool_manager._tools),
}))
'''


def _run_probe() -> dict:
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_PROBE],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, result.stderr
    return json.loads(lines[-1])


def test_fresh_core_discovery_is_solver_free():
    sample = _run_probe()

    assert sample["heavy_modules"] == []
    assert sample["new_external_processes"] == []
    assert sample["tool_count"] == 43
    assert sample["create_seconds"] <= 0.75


def test_cold_core_discovery_budget_has_seven_raw_samples(capsys):
    samples = [_run_probe() for _ in range(7)]
    create_times = [sample["create_seconds"] for sample in samples]
    registration_rss = [sample["rss_from_server_import_mib"] for sample in samples]

    print(json.dumps({
        "runtime": sys.version,
        "samples": samples,
        "median_create_seconds": statistics.median(create_times),
        "median_registration_rss_mib": statistics.median(registration_rss),
    }))
    captured = capsys.readouterr()
    assert "median_create_seconds" in captured.out
    assert statistics.median(create_times) <= 0.75
    assert statistics.median(registration_rss) <= 50.0
