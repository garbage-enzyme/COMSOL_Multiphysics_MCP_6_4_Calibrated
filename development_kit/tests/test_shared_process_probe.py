"""Process-only tests for redacted shared Desktop/Server inventory."""

from __future__ import annotations

import sys

from src.shared_session.preflight import classify_shared_server_preflight
from src.shared_session.process_probe import collect_shared_preflight_snapshot


def _record(pid, parent, name, command, executable=None):
    return {
        "pid": pid,
        "parent_pid": parent,
        "name": name,
        "create_time": float(pid),
        "command_line": command,
        "executable": executable or f"C:/Program Files/COMSOL/{name}",
    }


def test_collector_redacts_paths_and_ignores_declared_process_children():
    records = [
        _record(10, 0, "comsol.exe", ["comsol.exe"]),
        _record(11, 10, "comsolhelper.exe", ["comsolhelper.exe"]),
        _record(20, 0, "comsolmphserver.exe", ["comsolmphserver.exe", "-port", "2036"]),
        _record(21, 20, "java.exe", ["java.exe", "comsol", "worker"]),
    ]
    snapshot = collect_shared_preflight_snapshot(
        process_provider=lambda: records,
        listener_provider=lambda: [{"host": "127.0.0.1", "port": 2036, "pid": 20}],
        window_provider=lambda: {10: {"window_count": 1, "responding": True}},
        version_provider=lambda path: "6.4.0.293",
        clock=lambda: 1000.0,
    )

    assert [item["kind"] for item in snapshot["processes"]] == [
        "comsol_desktop", "comsol_server"
    ]
    assert all("executable" not in item for item in snapshot["processes"])
    assert all("command_line" not in item for item in snapshot["processes"])
    serialized = str(snapshot)
    assert "Program Files" not in serialized
    assert "mph" not in sys.modules


def test_collector_exposes_external_mph_as_a_collision_without_version_requirement():
    records = [
        _record(10, 0, "comsol.exe", ["comsol.exe"]),
        _record(20, 0, "comsolmphserver.exe", ["comsolmphserver.exe", "-port", "2036"]),
        _record(30, 0, "python.exe", ["python.exe", "-c", "import mph; mph.Client()"]),
    ]
    snapshot = collect_shared_preflight_snapshot(
        process_provider=lambda: records,
        listener_provider=lambda: [{"host": "127.0.0.1", "port": 2036, "pid": 20}],
        window_provider=lambda: {10: {"window_count": 1, "responding": True}},
        version_provider=lambda path: "6.4.0.310",
        clock=lambda: 1000.0,
    )

    result = classify_shared_server_preflight(
        endpoint={"host": "localhost", "port": 2036},
        first_probe=snapshot,
        second_probe=snapshot,
    )

    assert [item["kind"] for item in snapshot["processes"]] == [
        "comsol_desktop", "comsol_server", "mph_client"
    ]
    assert result["state"] == "unclassified_comsol_or_mph_collision"


def test_unreadable_comsol_file_version_reaches_explicit_classifier_state():
    records = [
        _record(10, 0, "comsol.exe", ["comsol.exe"]),
        _record(20, 0, "comsolmphserver.exe", ["comsolmphserver.exe"]),
    ]
    snapshot = collect_shared_preflight_snapshot(
        process_provider=lambda: records,
        listener_provider=lambda: [{"host": "127.0.0.1", "port": 2036, "pid": 20}],
        window_provider=lambda: {10: {"window_count": 1, "responding": True}},
        version_provider=lambda path: None,
        clock=lambda: 1000.0,
    )

    result = classify_shared_server_preflight(
        endpoint={"host": "127.0.0.1", "port": 2036},
        first_probe=snapshot,
        second_probe=snapshot,
    )

    assert result["state"] == "unsupported_or_ambiguous_comsol_version"


def test_collector_excludes_current_mcp_process_identity():
    records = [
        _record(10, 0, "python.exe", ["python.exe", "-m", "src.server", "import mph"]),
    ]
    snapshot = collect_shared_preflight_snapshot(
        process_provider=lambda: records,
        listener_provider=list,
        window_provider=dict,
        version_provider=lambda path: None,
        exclude_pids={10},
        clock=lambda: 1000.0,
    )

    assert snapshot["processes"] == []
