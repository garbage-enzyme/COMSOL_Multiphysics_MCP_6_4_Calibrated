"""Pure two-probe classification for local shared Desktop/Server state."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Mapping

from .contracts import normalize_shared_server_endpoint


SHARED_SERVER_PREFLIGHT_SCHEMA = "comsol_mcp.shared_server_preflight"
SHARED_SERVER_PREFLIGHT_VERSION = "1.0.0"
ACCEPTED_RELEASE_LINE = (6, 4, 0)
MAX_INVENTORY_PROCESSES = 128
MAX_INVENTORY_LISTENERS = 128

_PROCESS_FIELDS = frozenset(
    {
        "pid",
        "parent_pid",
        "kind",
        "create_time",
        "command_signature",
        "file_version",
        "window_count",
        "responding",
    }
)
_LISTENER_FIELDS = frozenset({"host", "port", "pid"})
_SNAPSHOT_FIELDS = frozenset(
    {"inventory_complete", "observed_at_epoch", "processes", "listeners"}
)
_PROCESS_KINDS = frozenset(
    {"comsol_desktop", "comsol_server", "mph_client", "other_comsol"}
)
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_VERSION = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)\.(\d+)(?!\d)")


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _exact_mapping(value: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise ValueError(f"{label} must be an object with string keys")
    actual = set(value)
    if actual != fields:
        raise ValueError(
            f"{label} fields are invalid; "
            f"missing={sorted(fields - actual)}, unknown={sorted(actual - fields)}"
        )
    return dict(value)


def _positive_integer(value: Any, label: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "nonnegative" if allow_zero else "positive"
        raise ValueError(f"{label} must be a {qualifier} integer")
    return value


def _finite(value: Any, label: str, *, positive: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be finite")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError(f"{label} must be positive and finite")
    return number


def _normalize_version(value: Any) -> tuple[str, tuple[int, int, int, int]]:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("COMSOL file version must be a bounded string")
    match = _VERSION.search(value)
    if match is None:
        raise ValueError("COMSOL file version is unreadable")
    parts = tuple(int(item) for item in match.groups())
    return ".".join(str(item) for item in parts), parts


def _normalize_process(value: Any, index: int) -> dict[str, Any]:
    label = f"processes[{index}]"
    raw = _exact_mapping(value, _PROCESS_FIELDS, label)
    kind = raw["kind"]
    if kind not in _PROCESS_KINDS:
        raise ValueError(f"{label}.kind is unsupported")
    signature = raw["command_signature"]
    if not isinstance(signature, str) or not _HEX64.fullmatch(signature):
        raise ValueError(f"{label}.command_signature must be a SHA-256")
    version, version_parts = _normalize_version(raw["file_version"])
    if not isinstance(raw["responding"], bool):
        raise ValueError(f"{label}.responding must be boolean")
    body = {
        "pid": _positive_integer(raw["pid"], f"{label}.pid"),
        "parent_pid": _positive_integer(
            raw["parent_pid"], f"{label}.parent_pid", allow_zero=True
        ),
        "kind": kind,
        "create_time": _finite(raw["create_time"], f"{label}.create_time"),
        "command_signature": signature.casefold(),
        "file_version": version,
        "version_parts": version_parts,
        "window_count": _positive_integer(
            raw["window_count"], f"{label}.window_count", allow_zero=True
        ),
        "responding": raw["responding"],
    }
    body["identity_sha256"] = _canonical_sha256(
        {
            key: body[key]
            for key in (
                "pid", "kind", "create_time", "command_signature", "file_version"
            )
        }
    )
    return body


def _normalize_listener(value: Any, index: int) -> dict[str, Any]:
    label = f"listeners[{index}]"
    raw = _exact_mapping(value, _LISTENER_FIELDS, label)
    endpoint = normalize_shared_server_endpoint(
        {"host": raw["host"], "port": raw["port"]}
    )
    return {
        "host": endpoint.host,
        "port": endpoint.port,
        "pid": _positive_integer(raw["pid"], f"{label}.pid"),
    }


def normalize_shared_preflight_snapshot(value: Any) -> dict[str, Any]:
    """Normalize one bounded complete process/listener observation."""
    raw = _exact_mapping(value, _SNAPSHOT_FIELDS, "preflight snapshot")
    if raw["inventory_complete"] is not True:
        raise ValueError("preflight process/listener inventory must be complete")
    processes = raw["processes"]
    listeners = raw["listeners"]
    if not isinstance(processes, list) or len(processes) > MAX_INVENTORY_PROCESSES:
        raise ValueError("preflight processes must be a bounded list")
    if not isinstance(listeners, list) or len(listeners) > MAX_INVENTORY_LISTENERS:
        raise ValueError("preflight listeners must be a bounded list")
    normalized_processes = [
        _normalize_process(item, index) for index, item in enumerate(processes)
    ]
    pids = [item["pid"] for item in normalized_processes]
    if len(pids) != len(set(pids)):
        raise ValueError("preflight process inventory contains duplicate PIDs")
    return {
        "inventory_complete": True,
        "observed_at_epoch": _finite(
            raw["observed_at_epoch"], "preflight observation time"
        ),
        "processes": normalized_processes,
        "listeners": [
            _normalize_listener(item, index) for index, item in enumerate(listeners)
        ],
    }


def _public_processes(processes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "kind": item["kind"],
            "identity_sha256": item["identity_sha256"],
            "file_version": item["file_version"],
            "window_count": item["window_count"],
            "responding": item["responding"],
        }
        for item in sorted(processes, key=lambda item: (item["kind"], item["pid"]))
    ]


def classify_shared_server_preflight(
    *,
    endpoint: Mapping[str, Any],
    first_probe: Mapping[str, Any],
    second_probe: Mapping[str, Any],
) -> dict[str, Any]:
    """Classify stable local shared-session readiness without importing MPh."""
    declared = normalize_shared_server_endpoint(endpoint)
    first = normalize_shared_preflight_snapshot(first_probe)
    second = normalize_shared_preflight_snapshot(second_probe)
    processes = second["processes"]
    desktops = [item for item in processes if item["kind"] == "comsol_desktop"]
    servers = [item for item in processes if item["kind"] == "comsol_server"]
    collisions = [
        item for item in processes if item["kind"] in {"mph_client", "other_comsol"}
    ]
    first_by_pid = {item["pid"]: item for item in first["processes"]}
    changed = [
        item
        for item in processes
        if item["pid"] not in first_by_pid
        or item["identity_sha256"] != first_by_pid[item["pid"]]["identity_sha256"]
    ]
    first_listener = [
        item
        for item in first["listeners"]
        if item["host"] == declared.host and item["port"] == declared.port
    ]
    second_listener = [
        item
        for item in second["listeners"]
        if item["host"] == declared.host and item["port"] == declared.port
    ]
    violations: list[str] = []
    warnings: list[str] = []
    state = "ready_for_attach"
    retryable = False

    all_versions = [item["version_parts"] for item in processes]
    if any(parts[:3] != ACCEPTED_RELEASE_LINE for parts in all_versions):
        violations.append("unsupported_or_ambiguous_comsol_version")
        state = "unsupported_or_ambiguous_comsol_version"
    elif changed:
        violations.append("process_identity_changed_between_probes")
        state = "process_identity_changed_between_probes"
    elif collisions:
        violations.append("unclassified_comsol_or_mph_collision")
        state = "unclassified_comsol_or_mph_collision"
    elif len(desktops) > 1 or sum(item["window_count"] for item in desktops) > 1:
        violations.append("ambiguous_gui_clients")
        state = "ambiguous_gui_clients"
    elif not desktops and not second_listener:
        violations.append("desktop_and_server_absent")
        state = "desktop_and_server_absent"
        retryable = True
    elif not desktops:
        violations.append("desktop_absent")
        state = "desktop_absent"
        retryable = True
    elif (
        desktops[0]["window_count"] == 0
        or not desktops[0]["responding"]
        or len(first_listener) != 1
        or len(second_listener) != 1
    ):
        violations.append("desktop_or_server_starting")
        state = "desktop_or_server_starting"
        retryable = True
    elif first_listener[0]["pid"] != second_listener[0]["pid"]:
        violations.append("listener_owner_changed_between_probes")
        state = "listener_owner_changed_between_probes"
    else:
        owner_pid = second_listener[0]["pid"]
        owner = next((item for item in servers if item["pid"] == owner_pid), None)
        if owner is None or len(servers) != 1:
            violations.append("unknown_or_multiple_candidate_servers")
            state = "unknown_or_multiple_candidate_servers"
        else:
            builds = {item["file_version"] for item in (*desktops, owner)}
            if len(builds) > 1:
                warnings.append("same_accepted_release_line_build_difference")

    public_processes = _public_processes(processes)
    return {
        "schema_name": SHARED_SERVER_PREFLIGHT_SCHEMA,
        "schema_version": SHARED_SERVER_PREFLIGHT_VERSION,
        "success": state == "ready_for_attach",
        "state": state,
        "retryable": retryable,
        "endpoint": declared.to_dict(),
        "desktop_count": len(desktops),
        "desktop_window_count": sum(item["window_count"] for item in desktops),
        "server_count": len(servers),
        "accepted_release_line": "6.4.0.*",
        "processes": public_processes,
        "process_inventory_sha256": _canonical_sha256(public_processes),
        "violations": violations,
        "warnings": warnings,
        "paths_included": False,
        "mph_imported": False,
        "client_constructed": False,
        "lease_acquired": False,
    }


__all__ = [
    "ACCEPTED_RELEASE_LINE",
    "SHARED_SERVER_PREFLIGHT_SCHEMA",
    "SHARED_SERVER_PREFLIGHT_VERSION",
    "classify_shared_server_preflight",
    "normalize_shared_preflight_snapshot",
]
