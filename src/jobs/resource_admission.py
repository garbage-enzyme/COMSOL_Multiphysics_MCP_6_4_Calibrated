"""Pure M4 resource-policy validation and admission decisions."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import time
from typing import Any, Mapping

import psutil


_FRACTION_RULES = (
    ("available_memory", "available_memory_bytes", "total_memory_bytes"),
    ("remaining_commit", "remaining_commit_bytes", "commit_limit_bytes"),
)
_POLICY_FIELDS = frozenset(
    {
        "available_memory_warn_fraction",
        "available_memory_refuse_fraction",
        "remaining_commit_warn_fraction",
        "remaining_commit_refuse_fraction",
        "runtime_free_space_warn_bytes",
        "runtime_free_space_refuse_bytes",
        "max_mesh_elements",
        "max_dof",
        "wall_time_budget_seconds",
        "minimum_next_point_seconds",
    }
)
_SAMPLE_FIELDS = frozenset(
    {
        "stage",
        "observed_at_epoch",
        "available_memory_bytes",
        "total_memory_bytes",
        "remaining_commit_bytes",
        "commit_limit_bytes",
        "runtime_free_bytes",
        "mesh_elements",
        "dof",
        "elapsed_wall_seconds",
        "worker_private_bytes",
        "worker_working_set_bytes",
        "cpu_progress_proxy",
        "disk_io_bytes",
        "pagefile_io_bytes",
        "durable_result_epoch",
    }
)
_STAGES = frozenset({"pre_mesh", "post_mesh", "pre_solve", "post_solve", "recovery"})


def _sha256(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return {str(key): item for key, item in value.items()}


def _finite_number(value: object, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or number < 0 or (positive and number <= 0):
        qualifier = "positive and finite" if positive else "nonnegative and finite"
        raise ValueError(f"{name} must be {qualifier}")
    return number


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def normalize_resource_policy(policy: object | None) -> dict[str, Any] | None:
    """Validate one caller-supplied policy without adding host defaults."""
    if policy is None:
        return None
    raw = _mapping(policy, "resource_policy")
    unknown = sorted(set(raw) - _POLICY_FIELDS)
    if unknown:
        raise ValueError(f"unknown resource_policy fields: {', '.join(unknown)}")
    if not raw:
        raise ValueError("resource_policy must contain at least one explicit rule")
    normalized: dict[str, Any] = {}
    for prefix, _numerator, _denominator in _FRACTION_RULES:
        warn_name = f"{prefix}_warn_fraction"
        refuse_name = f"{prefix}_refuse_fraction"
        for name in (warn_name, refuse_name):
            if name in raw:
                value = _finite_number(raw[name], name)
                if value > 1:
                    raise ValueError(f"{name} must be between 0 and 1")
                normalized[name] = value
        if warn_name in normalized and refuse_name in normalized:
            if normalized[refuse_name] > normalized[warn_name]:
                raise ValueError(f"{refuse_name} must not exceed {warn_name}")
    for name in ("runtime_free_space_warn_bytes", "runtime_free_space_refuse_bytes"):
        if name in raw:
            normalized[name] = _positive_integer(raw[name], name)
    if {
        "runtime_free_space_warn_bytes",
        "runtime_free_space_refuse_bytes",
    } <= set(normalized):
        if (
            normalized["runtime_free_space_refuse_bytes"]
            > normalized["runtime_free_space_warn_bytes"]
        ):
            raise ValueError(
                "runtime_free_space_refuse_bytes must not exceed runtime_free_space_warn_bytes"
            )
    for name in ("max_mesh_elements", "max_dof"):
        if name in raw:
            normalized[name] = _positive_integer(raw[name], name)
    wall_fields = {"wall_time_budget_seconds", "minimum_next_point_seconds"}
    if bool(wall_fields & set(raw)) and not wall_fields <= set(raw):
        raise ValueError(
            "wall_time_budget_seconds and minimum_next_point_seconds must be declared together"
        )
    for name in wall_fields:
        if name in raw:
            normalized[name] = _finite_number(raw[name], name, positive=True)
    if wall_fields <= set(normalized):
        if normalized["minimum_next_point_seconds"] > normalized["wall_time_budget_seconds"]:
            raise ValueError("minimum_next_point_seconds must not exceed wall_time_budget_seconds")
    return {
        "schema_name": "comsol_mcp.resource_policy",
        "schema_version": "1.0.0",
        "rules": normalized,
        "host_defaults_applied": False,
        "temporary_scavenging": "disabled",
        "policy_sha256": _sha256(normalized),
    }


def normalize_telemetry_sample(sample: object) -> dict[str, Any]:
    """Normalize one bounded telemetry observation without inventing missing metrics."""
    raw = _mapping(sample, "telemetry_sample")
    unknown = sorted(set(raw) - _SAMPLE_FIELDS)
    if unknown:
        raise ValueError(f"unknown telemetry_sample fields: {', '.join(unknown)}")
    stage = raw.get("stage")
    if stage not in _STAGES:
        raise ValueError(f"stage must be one of: {', '.join(sorted(_STAGES))}")
    normalized: dict[str, Any] = {"stage": stage}
    integer_fields = {
        "available_memory_bytes",
        "total_memory_bytes",
        "remaining_commit_bytes",
        "commit_limit_bytes",
        "runtime_free_bytes",
        "mesh_elements",
        "dof",
        "worker_private_bytes",
        "worker_working_set_bytes",
        "disk_io_bytes",
        "pagefile_io_bytes",
    }
    for name, value in raw.items():
        if name == "stage" or value is None:
            continue
        if name in integer_fields:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer or null")
            normalized[name] = value
        else:
            normalized[name] = _finite_number(value, name)
    for numerator, denominator in (
        ("available_memory_bytes", "total_memory_bytes"),
        ("remaining_commit_bytes", "commit_limit_bytes"),
    ):
        if numerator in normalized and denominator in normalized:
            if normalized[denominator] <= 0:
                raise ValueError(f"{denominator} must be positive when its fraction is derived")
            if normalized[numerator] > normalized[denominator]:
                raise ValueError(f"{numerator} must not exceed {denominator}")
    unavailable = sorted(name for name in _SAMPLE_FIELDS - {"stage"} if name not in normalized)
    return {
        "schema_name": "comsol_mcp.resource_telemetry_sample",
        "schema_version": "1.0.0",
        "values": normalized,
        "unavailable": unavailable,
        "sample_sha256": _sha256(normalized),
    }


def _windows_commit_bytes() -> tuple[int, int]:
    if os.name != "nt":
        raise OSError("Windows commit telemetry is unavailable on this platform")
    import ctypes

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError(ctypes.get_last_error(), "GlobalMemoryStatusEx failed")
    return int(status.ullAvailPageFile), int(status.ullTotalPageFile)


def collect_resource_telemetry(
    *,
    stage: str,
    runtime_path: str | Path,
    process_id: int | None = None,
    mesh_elements: int | None = None,
    dof: int | None = None,
    elapsed_wall_seconds: float | None = None,
    durable_result_epoch: float | None = None,
) -> dict[str, Any]:
    """Collect one bounded, solver-free host/process/runtime observation."""
    root = Path(runtime_path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("runtime_path must name an existing directory")
    pid = os.getpid() if process_id is None else process_id
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("process_id must be a positive integer")
    values: dict[str, Any] = {
        "stage": stage,
        "observed_at_epoch": time.time(),
    }
    optional = {
        "mesh_elements": mesh_elements,
        "dof": dof,
        "elapsed_wall_seconds": elapsed_wall_seconds,
        "durable_result_epoch": durable_result_epoch,
    }
    values.update({name: value for name, value in optional.items() if value is not None})
    errors: list[dict[str, str]] = []

    try:
        memory = psutil.virtual_memory()
        values["available_memory_bytes"] = int(memory.available)
        values["total_memory_bytes"] = int(memory.total)
    except Exception as exc:
        errors.append({"code": "physical_memory_unavailable", "error": str(exc)[:200]})
    try:
        remaining_commit, commit_limit = _windows_commit_bytes()
        values["remaining_commit_bytes"] = remaining_commit
        values["commit_limit_bytes"] = commit_limit
    except Exception as exc:
        errors.append({"code": "commit_unavailable", "error": str(exc)[:200]})
    try:
        values["runtime_free_bytes"] = int(shutil.disk_usage(root).free)
    except Exception as exc:
        errors.append({"code": "runtime_free_space_unavailable", "error": str(exc)[:200]})
    try:
        process = psutil.Process(pid)
        memory_info = process.memory_info()
        values["worker_working_set_bytes"] = int(memory_info.rss)
        private = getattr(memory_info, "private", None)
        if private is not None:
            values["worker_private_bytes"] = int(private)
        cpu = process.cpu_times()
        values["cpu_progress_proxy"] = float(cpu.user + cpu.system)
    except Exception as exc:
        errors.append({"code": "process_metrics_unavailable", "error": str(exc)[:200]})
    try:
        disk = psutil.disk_io_counters()
        if disk is not None:
            values["disk_io_bytes"] = int(disk.read_bytes + disk.write_bytes)
    except Exception as exc:
        errors.append({"code": "disk_io_unavailable", "error": str(exc)[:200]})
    try:
        swap = psutil.swap_memory()
        values["pagefile_io_bytes"] = int(swap.sin + swap.sout)
    except Exception as exc:
        errors.append({"code": "pagefile_io_unavailable", "error": str(exc)[:200]})

    telemetry = normalize_telemetry_sample(values)
    return {
        **telemetry,
        "process_id": pid,
        "metric_sources": {
            "physical_memory": "psutil.virtual_memory",
            "remaining_commit": "GlobalMemoryStatusEx.ullAvailPageFile",
            "commit_limit": "GlobalMemoryStatusEx.ullTotalPageFile",
            "runtime_free_space": "shutil.disk_usage",
            "process_memory_cpu": "psutil.Process",
            "disk_io": "psutil.disk_io_counters",
            "pagefile_io": "psutil.swap_memory",
        },
        "runtime_volume": {
            "path_class": "ascii" if str(root).isascii() else "non_ascii",
            "absolute_path_redacted": True,
        },
        "collection_errors": errors[:10],
        "solver_started": False,
    }


def _rule(
    evidence: list[dict[str, Any]],
    *,
    code: str,
    actual: float | int | None,
    threshold: float | int,
    comparison: str,
    outcome: str,
) -> None:
    evidence.append(
        {
            "code": code,
            "actual": actual,
            "threshold": threshold,
            "comparison": comparison,
            "outcome": outcome,
        }
    )


def evaluate_resource_admission(
    policy: object | None,
    sample: object,
    *,
    continue_on_warning: bool = False,
) -> dict[str, Any]:
    """Return allow/confirmation/refuse from explicit policy and one sample."""
    normalized_policy = normalize_resource_policy(policy)
    telemetry = normalize_telemetry_sample(sample)
    if normalized_policy is None:
        return {
            "success": True,
            "state": "disabled",
            "decision": "allow",
            "policy": None,
            "telemetry": telemetry,
            "evidence": [],
            "temporary_scavenging": "disabled",
            "cleanup_action": "none",
        }
    rules = normalized_policy["rules"]
    values = telemetry["values"]
    evidence: list[dict[str, Any]] = []
    red = False
    warning = False

    for prefix, numerator, denominator in _FRACTION_RULES:
        configured = {
            name: rules[name]
            for name in (f"{prefix}_warn_fraction", f"{prefix}_refuse_fraction")
            if name in rules
        }
        if not configured:
            continue
        fraction = None
        if numerator in values and denominator in values and values[denominator] > 0:
            fraction = values[numerator] / values[denominator]
        if fraction is None:
            red = True
            _rule(
                evidence,
                code=f"{prefix}_unavailable",
                actual=None,
                threshold=min(configured.values()),
                comparison="required telemetry",
                outcome="refuse",
            )
            continue
        refuse = rules.get(f"{prefix}_refuse_fraction")
        warn = rules.get(f"{prefix}_warn_fraction")
        if refuse is not None and fraction < refuse:
            red = True
            _rule(evidence, code=f"{prefix}_refuse", actual=fraction, threshold=refuse, comparison="<", outcome="refuse")
        elif warn is not None and fraction < warn:
            warning = True
            _rule(evidence, code=f"{prefix}_warning", actual=fraction, threshold=warn, comparison="<", outcome="warning")
        else:
            _rule(evidence, code=f"{prefix}_ok", actual=fraction, threshold=warn if warn is not None else refuse, comparison=">=", outcome="ok")

    disk_rules = {
        name: rules[name]
        for name in ("runtime_free_space_warn_bytes", "runtime_free_space_refuse_bytes")
        if name in rules
    }
    if disk_rules:
        actual = values.get("runtime_free_bytes")
        if actual is None:
            red = True
            _rule(evidence, code="runtime_free_space_unavailable", actual=None, threshold=min(disk_rules.values()), comparison="required telemetry", outcome="refuse")
        elif "runtime_free_space_refuse_bytes" in rules and actual < rules["runtime_free_space_refuse_bytes"]:
            red = True
            _rule(evidence, code="runtime_free_space_refuse", actual=actual, threshold=rules["runtime_free_space_refuse_bytes"], comparison="<", outcome="refuse")
        elif "runtime_free_space_warn_bytes" in rules and actual < rules["runtime_free_space_warn_bytes"]:
            warning = True
            _rule(evidence, code="runtime_free_space_warning", actual=actual, threshold=rules["runtime_free_space_warn_bytes"], comparison="<", outcome="warning")
        else:
            threshold = rules.get("runtime_free_space_warn_bytes", rules.get("runtime_free_space_refuse_bytes"))
            _rule(evidence, code="runtime_free_space_ok", actual=actual, threshold=threshold, comparison=">=", outcome="ok")

    for policy_name, sample_name, code in (
        ("max_mesh_elements", "mesh_elements", "mesh_elements"),
        ("max_dof", "dof", "dof"),
    ):
        if policy_name not in rules:
            continue
        actual = values.get(sample_name)
        if actual is None:
            red = True
            _rule(evidence, code=f"{code}_unavailable", actual=None, threshold=rules[policy_name], comparison="required telemetry", outcome="refuse")
        elif actual > rules[policy_name]:
            red = True
            _rule(evidence, code=f"{code}_refuse", actual=actual, threshold=rules[policy_name], comparison=">", outcome="refuse")
        else:
            _rule(evidence, code=f"{code}_ok", actual=actual, threshold=rules[policy_name], comparison="<=", outcome="ok")

    if "wall_time_budget_seconds" in rules:
        elapsed = values.get("elapsed_wall_seconds")
        if elapsed is None:
            red = True
            _rule(evidence, code="wall_time_unavailable", actual=None, threshold=rules["minimum_next_point_seconds"], comparison="required telemetry", outcome="refuse")
        else:
            remaining = max(0.0, rules["wall_time_budget_seconds"] - elapsed)
            if remaining < rules["minimum_next_point_seconds"]:
                red = True
                _rule(evidence, code="wall_time_refuse", actual=remaining, threshold=rules["minimum_next_point_seconds"], comparison="<", outcome="refuse")
            else:
                _rule(evidence, code="wall_time_ok", actual=remaining, threshold=rules["minimum_next_point_seconds"], comparison=">=", outcome="ok")

    if red:
        state, decision = "red", "refuse"
    elif warning and continue_on_warning:
        state, decision = "warning", "allow_with_warning"
    elif warning:
        state, decision = "warning", "require_confirmation"
    else:
        state, decision = "green", "allow"
    return {
        "success": True,
        "state": state,
        "decision": decision,
        "continue_on_warning": bool(continue_on_warning),
        "policy": normalized_policy,
        "telemetry": telemetry,
        "evidence": evidence,
        "temporary_scavenging": "disabled",
        "cleanup_action": "none",
    }


__all__ = [
    "collect_resource_telemetry",
    "evaluate_resource_admission",
    "normalize_resource_policy",
    "normalize_telemetry_sample",
]
