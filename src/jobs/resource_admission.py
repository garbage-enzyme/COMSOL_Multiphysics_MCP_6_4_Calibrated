"""Pure M4 resource-policy validation and admission decisions."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
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
RESOURCE_JOURNAL_MAX_ENTRIES = 4096
RESOURCE_JOURNAL_MAX_ENTRY_BYTES = 64 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


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
    if raw.get("schema_name") == "comsol_mcp.resource_policy":
        expected_fields = {
            "schema_name",
            "schema_version",
            "rules",
            "host_defaults_applied",
            "temporary_scavenging",
            "policy_sha256",
        }
        if set(raw) != expected_fields:
            raise ValueError("normalized resource_policy has invalid fields")
        if raw.get("schema_version") != "1.0.0":
            raise ValueError("unsupported normalized resource_policy schema_version")
        normalized = normalize_resource_policy(raw.get("rules"))
        if (
            normalized is None
            or raw.get("host_defaults_applied") is not False
            or raw.get("temporary_scavenging") != "disabled"
            or raw.get("policy_sha256") != normalized["policy_sha256"]
        ):
            raise ValueError("normalized resource_policy integrity check failed")
        return normalized
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
    if raw.get("schema_name") == "comsol_mcp.resource_telemetry_sample":
        expected_fields = {
            "schema_name",
            "schema_version",
            "values",
            "unavailable",
            "sample_sha256",
        }
        if set(raw) != expected_fields:
            raise ValueError("normalized telemetry_sample has invalid fields")
        if raw.get("schema_version") != "1.0.0":
            raise ValueError("unsupported normalized telemetry_sample schema_version")
        normalized = normalize_telemetry_sample(raw.get("values"))
        if (
            raw.get("unavailable") != normalized["unavailable"]
            or raw.get("sample_sha256") != normalized["sample_sha256"]
        ):
            raise ValueError("normalized telemetry_sample integrity check failed")
        return normalized
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


def _sample_values(sample: object) -> dict[str, Any]:
    if isinstance(sample, Mapping) and sample.get("schema_name") == "comsol_mcp.resource_telemetry_sample":
        values = sample.get("values")
        if not isinstance(values, Mapping):
            raise ValueError("normalized telemetry sample has no values object")
        return normalize_telemetry_sample(dict(values))["values"]
    return normalize_telemetry_sample(sample)["values"]


def build_resource_calibration_report(
    *,
    baseline_id: str,
    baseline_status: str,
    baseline_sample: object,
    candidates: object,
) -> dict[str, Any]:
    """Compare samples to one caller-declared known-safe baseline without policy inference."""
    if not isinstance(baseline_id, str) or not baseline_id.strip() or len(baseline_id) > 128:
        raise ValueError("baseline_id must be a non-empty string of at most 128 characters")
    if baseline_status != "known_safe":
        raise ValueError("baseline_status must be exactly known_safe")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("candidates must be a non-empty list")
    baseline = _sample_values(baseline_sample)
    metric_names = (
        "mesh_elements",
        "dof",
        "worker_private_bytes",
        "worker_working_set_bytes",
        "runtime_free_bytes",
        "elapsed_wall_seconds",
    )
    fraction_names = (
        ("available_memory_fraction", "available_memory_bytes", "total_memory_bytes"),
        ("remaining_commit_fraction", "remaining_commit_bytes", "commit_limit_bytes"),
    )

    def metrics(values: dict[str, Any]) -> dict[str, float | int]:
        result = {name: values[name] for name in metric_names if name in values}
        for name, numerator, denominator in fraction_names:
            if numerator in values and denominator in values and values[denominator] > 0:
                result[name] = values[numerator] / values[denominator]
        return result

    baseline_metrics = metrics(baseline)
    comparisons = []
    seen = set()
    for item in candidates:
        if not isinstance(item, Mapping) or set(item) != {"sample_id", "telemetry"}:
            raise ValueError("each candidate requires exactly sample_id and telemetry")
        sample_id = item["sample_id"]
        if not isinstance(sample_id, str) or not sample_id.strip() or len(sample_id) > 128:
            raise ValueError("sample_id must be a non-empty string of at most 128 characters")
        if sample_id in seen or sample_id == baseline_id:
            raise ValueError("sample IDs must be unique and differ from baseline_id")
        seen.add(sample_id)
        candidate_metrics = metrics(_sample_values(item["telemetry"]))
        shared = sorted(set(baseline_metrics) & set(candidate_metrics))
        comparison = {}
        for name in shared:
            base = baseline_metrics[name]
            candidate = candidate_metrics[name]
            comparison[name] = {
                "baseline": base,
                "candidate": candidate,
                "delta": candidate - base,
                "ratio_to_baseline": candidate / base if base != 0 else None,
            }
        comparisons.append(
            {
                "sample_id": sample_id,
                "metrics": candidate_metrics,
                "comparison": comparison,
                "unavailable_comparisons": sorted(
                    (set(metric_names) | {item[0] for item in fraction_names}) - set(shared)
                ),
            }
        )
    payload = {
        "baseline": {
            "baseline_id": baseline_id,
            "status": "known_safe",
            "metrics": baseline_metrics,
        },
        "candidates": comparisons,
    }
    return {
        "schema_name": "comsol_mcp.resource_calibration_report",
        "schema_version": "1.0.0",
        **payload,
        "assessment": "calibration_only",
        "automatic_policy": None,
        "policy_scope": "project_local_only",
        "limitation": (
            "Relative telemetry comparisons do not prove solver progress, numerical convergence, "
            "or portable thresholds for another host, model, solver, or element order."
        ),
        "report_sha256": _sha256(payload),
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


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(
            f"{name} must be 1-128 ASCII identifier characters (letters, digits, . _ : -)"
        )
    return value


def _attempt(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("attempt must be a positive integer")
    return value


def _attempt_sequence(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("attempt_sequence must be a nonnegative integer")
    return value


def _journal_entry(payload: dict[str, Any]) -> dict[str, Any]:
    entry = {
        "schema_name": "comsol_mcp.resource_journal_entry",
        "schema_version": "1.0.0",
        **payload,
    }
    entry["entry_sha256"] = _sha256(entry)
    encoded = json.dumps(
        entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(encoded) > RESOURCE_JOURNAL_MAX_ENTRY_BYTES:
        raise ValueError("resource journal entry exceeds the size limit")
    return entry


def build_resource_admission_entries(
    *,
    attempt: int,
    point_id: str,
    attempt_sequence: int,
    policy: object | None,
    sample: object,
) -> list[dict[str, Any]]:
    """Build the telemetry-then-decision pair that must precede a stage transition."""
    attempt = _attempt(attempt)
    point_id = _identifier(point_id, "point_id")
    attempt_sequence = _attempt_sequence(attempt_sequence)
    telemetry = normalize_telemetry_sample(sample)
    decision = evaluate_resource_admission(policy, telemetry)
    normalized_policy = decision["policy"]
    common = {
        "attempt": attempt,
        "point_id": point_id,
        "stage": telemetry["values"]["stage"],
    }
    telemetry_entry = _journal_entry(
        {
            **common,
            "attempt_sequence": attempt_sequence,
            "entry_type": "telemetry",
            "telemetry": telemetry,
        }
    )
    admission_entry = _journal_entry(
        {
            **common,
            "attempt_sequence": attempt_sequence + 1,
            "entry_type": "admission",
            "telemetry_entry_sha256": telemetry_entry["entry_sha256"],
            "sample_sha256": telemetry["sample_sha256"],
            "policy_sha256": (
                normalized_policy["policy_sha256"] if normalized_policy is not None else None
            ),
            "state": decision["state"],
            "decision": decision["decision"],
            "evidence_codes": [item["code"] for item in decision["evidence"]],
            "start_authorized": decision["decision"] == "allow",
            "checkpoint_required": decision["decision"] in {"refuse", "require_confirmation"},
        }
    )
    return [telemetry_entry, admission_entry]


def build_resource_warning_continuation_entry(
    *,
    warning_admission: object,
    attempt_sequence: int,
    confirmation_id: str,
) -> dict[str, Any]:
    """Authorize one exact warning decision through a separate caller-confirmed transition."""
    warning = _validate_resource_journal_entry(warning_admission)
    if warning["entry_type"] != "admission":
        raise ValueError("warning_admission must be an admission entry")
    if warning["state"] != "warning" or warning["decision"] != "require_confirmation":
        raise ValueError("warning_admission must require confirmation")
    attempt_sequence = _attempt_sequence(attempt_sequence)
    if attempt_sequence <= warning["attempt_sequence"]:
        raise ValueError("warning continuation must follow its admission entry")
    confirmation_id = _identifier(confirmation_id, "confirmation_id")
    return _journal_entry(
        {
            "attempt": warning["attempt"],
            "point_id": warning["point_id"],
            "stage": warning["stage"],
            "attempt_sequence": attempt_sequence,
            "entry_type": "warning_continuation",
            "warning_admission_sha256": warning["entry_sha256"],
            "sample_sha256": warning["sample_sha256"],
            "policy_sha256": warning["policy_sha256"],
            "confirmation_id": confirmation_id,
            "state": "warning",
            "decision": "allow_with_warning",
            "start_authorized": True,
            "checkpoint_required": False,
        }
    )


def _validate_resource_journal_entry(entry: object) -> dict[str, Any]:
    raw = _mapping(entry, "resource_journal_entry")
    common = {
        "schema_name",
        "schema_version",
        "attempt",
        "point_id",
        "stage",
        "attempt_sequence",
        "entry_type",
        "entry_sha256",
    }
    fields = {
        "telemetry": common | {"telemetry"},
        "admission": common
        | {
            "telemetry_entry_sha256",
            "sample_sha256",
            "policy_sha256",
            "state",
            "decision",
            "evidence_codes",
            "start_authorized",
            "checkpoint_required",
        },
        "warning_continuation": common
        | {
            "warning_admission_sha256",
            "sample_sha256",
            "policy_sha256",
            "confirmation_id",
            "state",
            "decision",
            "start_authorized",
            "checkpoint_required",
        },
    }
    entry_type = raw.get("entry_type")
    if entry_type not in fields or set(raw) != fields[entry_type]:
        raise ValueError("resource journal entry has invalid fields or entry_type")
    if raw.get("schema_name") != "comsol_mcp.resource_journal_entry":
        raise ValueError("invalid resource journal schema_name")
    if raw.get("schema_version") != "1.0.0":
        raise ValueError("unsupported resource journal schema_version")
    raw["attempt"] = _attempt(raw.get("attempt"))
    raw["point_id"] = _identifier(raw.get("point_id"), "point_id")
    raw["attempt_sequence"] = _attempt_sequence(raw.get("attempt_sequence"))
    if raw.get("stage") not in _STAGES:
        raise ValueError("resource journal entry has an invalid stage")
    supplied_hash = raw.pop("entry_sha256")
    if not isinstance(supplied_hash, str) or supplied_hash != _sha256(raw):
        raise ValueError("resource journal entry hash mismatch")
    raw["entry_sha256"] = supplied_hash
    encoded = json.dumps(
        raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(encoded) > RESOURCE_JOURNAL_MAX_ENTRY_BYTES:
        raise ValueError("resource journal entry exceeds the size limit")
    if entry_type == "telemetry":
        raw["telemetry"] = normalize_telemetry_sample(raw["telemetry"])
        if raw["telemetry"]["values"]["stage"] != raw["stage"]:
            raise ValueError("resource telemetry entry stage mismatch")
    elif entry_type == "admission":
        if raw.get("state") not in {"disabled", "green", "warning", "red"}:
            raise ValueError("resource admission entry has an invalid state")
        expected_decision = {
            "disabled": "allow",
            "green": "allow",
            "warning": "require_confirmation",
            "red": "refuse",
        }[raw["state"]]
        if raw.get("decision") != expected_decision:
            raise ValueError("resource admission state and decision are inconsistent")
        if (
            raw["state"] == "disabled" and raw.get("policy_sha256") is not None
        ) or (
            raw["state"] != "disabled" and raw.get("policy_sha256") is None
        ):
            raise ValueError("resource admission policy identity is inconsistent")
        if not isinstance(raw.get("evidence_codes"), list) or not all(
            isinstance(item, str) and item for item in raw["evidence_codes"]
        ):
            raise ValueError("resource admission evidence_codes must be a string list")
        expected_authorized = raw["decision"] == "allow"
        expected_checkpoint = raw["decision"] in {"require_confirmation", "refuse"}
        if (
            raw.get("start_authorized") is not expected_authorized
            or raw.get("checkpoint_required") is not expected_checkpoint
        ):
            raise ValueError("resource admission transition flags are inconsistent")
    else:
        _identifier(raw.get("confirmation_id"), "confirmation_id")
        if (
            raw.get("state") != "warning"
            or raw.get("decision") != "allow_with_warning"
            or raw.get("start_authorized") is not True
            or raw.get("checkpoint_required") is not False
        ):
            raise ValueError("warning continuation flags are inconsistent")
    for name in (
        "telemetry_entry_sha256",
        "warning_admission_sha256",
        "sample_sha256",
        "policy_sha256",
    ):
        if name in raw and raw[name] is not None:
            if not isinstance(raw[name], str) or not re.fullmatch(r"[0-9a-f]{64}", raw[name]):
                raise ValueError(f"{name} must be a SHA-256 hex digest or null")
    return raw


def replay_resource_journal(
    entries: object,
    *,
    attempt: int,
    completed_point_ids: object = (),
) -> dict[str, Any]:
    """Validate journal ordering and derive fail-closed resume actions for the latest attempt."""
    attempt = _attempt(attempt)
    if not isinstance(entries, list):
        raise ValueError("resource journal entries must be a list")
    if len(entries) > RESOURCE_JOURNAL_MAX_ENTRIES:
        raise ValueError("resource journal exceeds the entry limit")
    if not isinstance(completed_point_ids, (list, tuple, set, frozenset)):
        raise ValueError("completed_point_ids must be a collection")
    completed = {_identifier(item, "completed_point_id") for item in completed_point_ids}
    normalized: list[dict[str, Any]] = []
    expected_sequence: dict[int, int] = {}
    latest_by_point: dict[tuple[int, str], dict[str, Any]] = {}
    telemetry_by_hash: dict[str, dict[str, Any]] = {}
    admission_by_hash: dict[str, dict[str, Any]] = {}
    max_attempt = 0
    last_attempt = 0
    for item in entries:
        entry = _validate_resource_journal_entry(item)
        current_attempt = entry["attempt"]
        if current_attempt < last_attempt:
            raise ValueError("resource journal attempts are not monotonic")
        last_attempt = current_attempt
        max_attempt = max(max_attempt, current_attempt)
        expected = expected_sequence.get(current_attempt, 0)
        if entry["attempt_sequence"] != expected:
            raise ValueError("resource journal attempt_sequence is not contiguous")
        expected_sequence[current_attempt] = expected + 1
        key = (current_attempt, entry["point_id"])
        if entry["entry_type"] == "telemetry":
            telemetry_by_hash[entry["entry_sha256"]] = entry
            latest_by_point[key] = entry
        elif entry["entry_type"] == "admission":
            telemetry = telemetry_by_hash.get(entry["telemetry_entry_sha256"])
            if (
                telemetry is None
                or telemetry["attempt"] != current_attempt
                or telemetry["point_id"] != entry["point_id"]
                or telemetry["stage"] != entry["stage"]
                or telemetry["telemetry"]["sample_sha256"] != entry["sample_sha256"]
                or latest_by_point.get(key, {}).get("entry_sha256")
                != telemetry["entry_sha256"]
            ):
                raise ValueError("resource admission does not match the latest telemetry entry")
            admission_by_hash[entry["entry_sha256"]] = entry
            latest_by_point[key] = entry
        else:
            admission = admission_by_hash.get(entry["warning_admission_sha256"])
            if (
                admission is None
                or admission["attempt"] != current_attempt
                or admission["point_id"] != entry["point_id"]
                or admission["stage"] != entry["stage"]
                or admission["state"] != "warning"
                or admission["decision"] != "require_confirmation"
                or admission["sample_sha256"] != entry["sample_sha256"]
                or admission["policy_sha256"] != entry["policy_sha256"]
                or latest_by_point.get(key, {}).get("entry_sha256")
                != admission["entry_sha256"]
            ):
                raise ValueError("warning continuation is stale or mismatched")
            latest_by_point[key] = entry
        normalized.append(entry)
    if normalized and attempt != max_attempt:
        raise ValueError("attempt must match the latest journal attempt")

    points: dict[str, dict[str, Any]] = {}
    for (entry_attempt, point_id), latest in sorted(latest_by_point.items()):
        if entry_attempt != attempt:
            continue
        if point_id in completed:
            action = "skip_completed"
        elif latest["entry_type"] == "telemetry":
            action = "admission_required"
        elif latest["decision"] in {"allow", "allow_with_warning"}:
            action = "start_point"
        elif latest["decision"] == "require_confirmation":
            action = "await_confirmation"
        else:
            action = "checkpoint_no_start"
        points[point_id] = {
            "stage": latest["stage"],
            "latest_entry_type": latest["entry_type"],
            "latest_entry_sha256": latest["entry_sha256"],
            "decision": latest.get("decision"),
            "start_authorized": action == "start_point",
            "action": action,
        }
    return {
        "schema_name": "comsol_mcp.resource_journal_replay",
        "schema_version": "1.0.0",
        "attempt": attempt,
        "next_attempt_sequence": expected_sequence.get(attempt, 0),
        "entry_count": len(normalized),
        "points": points,
        "completed_point_ids": sorted(completed),
        "duplicate_valid_rows_authorized": False,
        "temporary_scavenging": "disabled",
    }


__all__ = [
    "RESOURCE_JOURNAL_MAX_ENTRIES",
    "build_resource_admission_entries",
    "build_resource_calibration_report",
    "build_resource_warning_continuation_entry",
    "collect_resource_telemetry",
    "evaluate_resource_admission",
    "normalize_resource_policy",
    "normalize_telemetry_sample",
    "replay_resource_journal",
]
