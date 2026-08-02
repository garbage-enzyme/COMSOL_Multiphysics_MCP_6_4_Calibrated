"""Cold-process control-plane discovery and startup-budget checks."""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

MAX_CORE_DISCOVERY_BYTES = 64 * 1024
MAX_CORE_TOOL_SCHEMA_BYTES = 16 * 1024
MAX_CAPABILITIES_RESPONSE_BYTES = 64 * 1024

_CHILD_PROBE = r"""
import json
import os
import psutil
import subprocess
import sys
import time

process = psutil.Process(os.getpid())
process_launch_events = []

def record_process_launch(event, args):
    if event in {"os.system", "os.startfile", "subprocess.Popen"}:
        process_launch_events.append({
            "event": event,
            "arguments": repr(args)[:4096],
        })

sys.addaudithook(record_process_launch)
process_start_rss = process.memory_info().rss
import_started = time.perf_counter()
from src.server import create_server
import_finished = time.perf_counter()
import_rss = process.memory_info().rss
create_started = time.perf_counter()
server = create_server("cold-control-plane", profile="core")
create_finished = time.perf_counter()
tools = sorted(server._tool_manager._tools.values(), key=lambda item: item.name)
tool_records = [
    {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.parameters,
    }
    for tool in tools
]
tool_record_bytes = [
    len(json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    for item in tool_records
]
core_discovery_bytes = len(
    json.dumps(tool_records, sort_keys=True, separators=(",", ":")).encode("utf-8")
)
capabilities_response_bytes = len(
    json.dumps(
        server._tool_manager._tools["capabilities"].fn(),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
)
if os.environ.get("COMSOL_MCP_CONTROL_PLANE_AUDIT_SELF_TEST") == "1":
    subprocess.run([sys.executable, "-c", "pass"], check=True)
heavy_roots = ("mph", "jpype", "numpy", "scipy", "matplotlib")
heavy_modules = sorted(
    name for name in sys.modules
    if any(name == root or name.startswith(root + ".") for root in heavy_roots)
)
print(json.dumps({
    "import_seconds": import_finished - import_started,
    "create_seconds": create_finished - create_started,
    "rss_from_process_start_mib": (process.memory_info().rss - process_start_rss) / 1048576,
    "rss_from_server_import_mib": (process.memory_info().rss - import_rss) / 1048576,
    "heavy_modules": heavy_modules,
    "process_launch_events": process_launch_events,
    "tool_count": len(server._tool_manager._tools),
    "core_discovery_bytes": core_discovery_bytes,
    "largest_tool_schema_bytes": max(tool_record_bytes),
    "capabilities_response_bytes": capabilities_response_bytes,
}))
"""


def _run_probe(*, audit_self_test: bool = False) -> dict:
    environment = os.environ.copy()
    if audit_self_test:
        environment["COMSOL_MCP_CONTROL_PLANE_AUDIT_SELF_TEST"] = "1"
    else:
        environment.pop("COMSOL_MCP_CONTROL_PLANE_AUDIT_SELF_TEST", None)
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_PROBE],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
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
    assert sample["process_launch_events"] == []
    assert sample["tool_count"] == 46
    assert sample["create_seconds"] <= 0.75
    assert sample["core_discovery_bytes"] <= MAX_CORE_DISCOVERY_BYTES
    assert sample["largest_tool_schema_bytes"] <= MAX_CORE_TOOL_SCHEMA_BYTES
    assert sample["capabilities_response_bytes"] <= MAX_CAPABILITIES_RESPONSE_BYTES


def test_process_launch_audit_captures_a_short_lived_child():
    sample = _run_probe(audit_self_test=True)

    assert [item["event"] for item in sample["process_launch_events"]] == ["subprocess.Popen"]


def test_cold_core_discovery_budget_has_seven_raw_samples(capsys):
    samples = [_run_probe() for _ in range(7)]
    create_times = [sample["create_seconds"] for sample in samples]
    registration_rss = [sample["rss_from_server_import_mib"] for sample in samples]

    print(
        json.dumps(
            {
                "runtime": sys.version,
                "samples": samples,
                "median_create_seconds": statistics.median(create_times),
                "median_registration_rss_mib": statistics.median(registration_rss),
                "maximum_core_discovery_bytes": max(
                    sample["core_discovery_bytes"] for sample in samples
                ),
                "maximum_largest_tool_schema_bytes": max(
                    sample["largest_tool_schema_bytes"] for sample in samples
                ),
                "maximum_capabilities_response_bytes": max(
                    sample["capabilities_response_bytes"] for sample in samples
                ),
            }
        )
    )
    captured = capsys.readouterr()
    assert "median_create_seconds" in captured.out
    assert statistics.median(create_times) <= 0.75
    assert statistics.median(registration_rss) <= 50.0
    assert all(
        sample["core_discovery_bytes"] <= MAX_CORE_DISCOVERY_BYTES
        and sample["largest_tool_schema_bytes"] <= MAX_CORE_TOOL_SCHEMA_BYTES
        and sample["capabilities_response_bytes"] <= MAX_CAPABILITIES_RESPONSE_BYTES
        for sample in samples
    )
