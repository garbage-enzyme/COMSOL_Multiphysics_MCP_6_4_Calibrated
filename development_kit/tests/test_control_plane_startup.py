"""Cold-process control-plane discovery and startup-budget checks."""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

MAX_CORE_DISCOVERY_BYTES = 80 * 1024
MAX_CORE_TOOL_SCHEMA_BYTES = 16 * 1024
# The discovery payload is a per-tool cost multiplied by the core surface, so the
# bound is only meaningful relative to the tool count. The original 64 KiB bound
# was set when core held 50 tools (about 1.24 KiB/tool) and the profile was
# already at 94.5% of it, leaving 3.6 KiB of headroom. alpha7.5 S10 adds the five
# bounded solver-free surrogate tools to core, which legitimately costs 9,829 B
# and cannot be recovered by trimming: even removing every pydantic ``title`` and
# the shared limits object leaves 5,684 B above the old bound. Raising the bound
# to 80 KiB keeps a real per-tool guard (about 1.45 KiB/tool at 55 tools) while
# accommodating a public surface addition, matching the documented 80 KiB
# capabilities bound below. Measured 2026-09-28: 70,978 B / 55 tools.
#
# 0.7.6 P0 changes what the capabilities response may contain rather than what it
# is allowed to weigh. It previously embedded the complete schema registry, which
# legitimately grows with each public schema (151 entries measured at 66,692 B on
# 2026-08-18, alpha7.2; 193 entries at 60,293 B on 2026-09-29), so the old bound
# had to keep absorbing registry growth that no client needed at startup.
# Progressive discovery replaces that with a compact identity/count view and
# serves entries on demand through ``catalog``, so the byte gate can now be the
# plan's real target instead of a ceiling that tracked the artifact surface.
MAX_CAPABILITIES_RESPONSE_BYTES = 80 * 1024
# The 0.7.6 release gate: default fresh-client bootstrap <= 32 KiB with a hard
# regression ceiling of 40 KiB. Measured 2026-09-30 after P0: 22,594 B.
BOOTSTRAP_TARGET_BYTES = 32 * 1024
BOOTSTRAP_CEILING_BYTES = 40 * 1024

_CHILD_PROBE = r"""
import asyncio
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
from development_kit.tests.mcp_test_support import decode_tool_result
import_finished = time.perf_counter()
import_rss = process.memory_info().rss
create_started = time.perf_counter()
server = create_server("cold-control-plane", profile="core")
create_finished = time.perf_counter()
tools = sorted(asyncio.run(server.list_tools()), key=lambda item: item.name)
tool_records = [
    {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.input_schema,
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
capabilities = decode_tool_result(asyncio.run(server.call_tool("capabilities", {})))
capabilities_response_bytes = len(
    json.dumps(
        capabilities,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
)
# 0.7.6 P0: the schema registry must be delivered progressively, so the cold
# response must not carry its entry rows at all. Size alone cannot prove that, so
# this measures the registry block and whether entries were actually sent.
capability_registry = capabilities.get("schema_registry")
schema_registry_bytes = len(
    json.dumps(capability_registry, sort_keys=True, separators=(",", ":")).encode("utf-8")
)
schema_registry_entry_rows_sent = isinstance(capability_registry, dict) and (
    "entries" in capability_registry
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
    "tool_count": len(tools),
    "core_discovery_bytes": core_discovery_bytes,
    "largest_tool_schema_bytes": max(tool_record_bytes, default=0),
    "capabilities_response_bytes": capabilities_response_bytes,
    "schema_registry_bytes": schema_registry_bytes,
    "schema_registry_entry_rows_sent": schema_registry_entry_rows_sent,
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
    assert sample["tool_count"] == 56
    assert sample["create_seconds"] <= 0.75
    assert sample["core_discovery_bytes"] <= MAX_CORE_DISCOVERY_BYTES
    assert sample["largest_tool_schema_bytes"] <= MAX_CORE_TOOL_SCHEMA_BYTES
    assert sample["capabilities_response_bytes"] <= MAX_CAPABILITIES_RESPONSE_BYTES
    # The absolute byte bound alone would let the payload grow whenever a tool is
    # added.  This per-tool average keeps the guard meaningful as the surface
    # changes, so a future tool that bloats discovery is caught even though the
    # total still fits.
    assert sample["core_discovery_bytes"] / sample["tool_count"] <= 1536
    # The 0.7.6 progressive-discovery release gate. The byte budget is only
    # meaningful if the payload actually excludes the schema registry, so the
    # exclusion is asserted here rather than inferred from the size alone.
    assert sample["capabilities_response_bytes"] <= BOOTSTRAP_TARGET_BYTES
    assert sample["capabilities_response_bytes"] <= BOOTSTRAP_CEILING_BYTES
    assert sample["schema_registry_bytes"] < 2048
    assert sample["schema_registry_entry_rows_sent"] is False


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
                "maximum_schema_registry_bytes": max(
                    sample["schema_registry_bytes"] for sample in samples
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
    # A single sample could pass by luck; the release target is asserted across
    # all of them, together with the proof that registry rows are not sent.
    assert all(
        sample["capabilities_response_bytes"] <= BOOTSTRAP_TARGET_BYTES
        and sample["schema_registry_entry_rows_sent"] is False
        for sample in samples
    )
