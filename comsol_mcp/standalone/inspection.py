"""Bounded solver-free inspection of standalone launcher artifacts."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from comsol_mcp.durable.canonical import validate_finite_json
from comsol_mcp.durable.io import read_file_bytes_bounded

from .builder import (
    BUILD_SCHEMA,
    BUILD_SCHEMA_VERSION,
    EXECUTABLE_NAME,
    MANIFEST_NAME,
    MAX_EXECUTABLE_BYTES,
    _resource_bytes,
)

STATUS_SCHEMA = "comsol_mcp.standalone_status"
STATUS_SCHEMA_VERSION = "1.0.0"
RESULT_SCHEMA = "comsol_mcp.standalone_driver_event"
RESULT_SCHEMA_VERSION = "1.0.0"
MAX_STATUS_BYTES = 64 * 1024
MAX_RESULTS_BYTES = 4 * 1024 * 1024
MAX_LOG_BYTES = 4 * 1024 * 1024
MAX_RESULT_ROWS = 128
MAX_TAIL_LINES = 500
TERMINAL_SCHEMA = "comsol_mcp.standalone_terminal"
TERMINAL_SCHEMA_VERSION = "1.0.0"
_RESULT_HASH_FIELDS = (
    "driver_java_sha256",
    "driver_class_sha256",
    "process_log_sha256",
    "comsol_batch_log_sha256",
    "launcher_sha256",
    "campaign_spec_sha256",
    "comsol_compile_sha256",
    "comsol_batch_sha256",
)
_EXECUTION_HASH_FIELDS = (
    "launcher_sha256",
    "campaign_spec_sha256",
    "comsol_compile_sha256",
    "comsol_batch_sha256",
)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_sha256(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"standalone result {field} is not a SHA-256 identity")
    return value


def _validate_execution_identity(value: dict[str, Any], *, artifact: str) -> None:
    for field in _EXECUTION_HASH_FIELDS:
        _require_sha256(value.get(field), field=f"{artifact}.{field}")
    version = value.get("comsol_version")
    if not isinstance(version, str) or not version.startswith("6.4."):
        raise ValueError(f"standalone {artifact} is not bound to COMSOL 6.4")


def _campaign_root(value: str | Path) -> Path:
    root = Path(value)
    if not root.is_absolute() or not str(root).isascii():
        raise ValueError("campaign directory must be absolute and ASCII-only")
    resolved = root.resolve(strict=True)
    if root.is_symlink() or not resolved.is_dir():
        raise ValueError("campaign directory must be a regular directory")
    return resolved


def read_campaign_status(campaign_directory: str | Path) -> dict[str, Any]:
    """Read one bounded atomic status projection without starting COMSOL."""
    root = _campaign_root(campaign_directory)
    payload = read_file_bytes_bounded(
        root / "assets" / "state" / "status.json", max_bytes=MAX_STATUS_BYTES
    )
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("standalone status is not valid UTF-8 JSON") from exc
    validate_finite_json(value)
    if not isinstance(value, dict):
        raise ValueError("standalone status must be a JSON object")
    if (
        value.get("schema_name") != STATUS_SCHEMA
        or value.get("schema_version") != STATUS_SCHEMA_VERSION
    ):
        raise ValueError("standalone status schema is unsupported")
    _validate_execution_identity(value, artifact="status")
    return deepcopy(value)


def read_campaign_terminal(campaign_directory: str | Path) -> dict[str, Any]:
    """Read one bounded terminal receipt without trusting mutable status alone."""
    root = _campaign_root(campaign_directory)
    payload = read_file_bytes_bounded(
        root / "assets" / "state" / "terminal.json", max_bytes=MAX_STATUS_BYTES
    )
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("standalone terminal receipt is not valid UTF-8 JSON") from exc
    validate_finite_json(value)
    if not isinstance(value, dict):
        raise ValueError("standalone terminal receipt must be a JSON object")
    if (
        value.get("schema_name") != TERMINAL_SCHEMA
        or value.get("schema_version") != TERMINAL_SCHEMA_VERSION
    ):
        raise ValueError("standalone terminal receipt schema is unsupported")
    if value.get("status_schema_name") != STATUS_SCHEMA:
        raise ValueError("standalone terminal receipt does not bind the status schema")
    _validate_execution_identity(value, artifact="terminal")
    return deepcopy(value)


def verify_standalone_deployment(deployment_directory: str | Path) -> dict[str, Any]:
    """Bind one executable to a current-package reviewed source manifest."""
    root = _campaign_root(deployment_directory)
    manifest_bytes = read_file_bytes_bounded(root / MANIFEST_NAME, max_bytes=MAX_STATUS_BYTES)
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("standalone build manifest is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise ValueError("standalone build manifest must be a JSON object")
    if (
        manifest.get("schema_name") != BUILD_SCHEMA
        or manifest.get("schema_version") != BUILD_SCHEMA_VERSION
    ):
        raise ValueError("standalone build manifest schema is unsupported")
    if manifest.get("status") != "passed":
        raise ValueError("standalone build manifest is not passed")
    if manifest.get("python_required_at_runtime") is not False:
        raise ValueError("standalone build manifest changed its runtime boundary")
    expected_build_runtime = {
        "windows_inbox_dotnet_framework_required": True,
        "separate_dotnet_runtime_required": False,
        "separate_dotnet_sdk_required": False,
        "visual_studio_required": False,
        "network_download_required": False,
    }
    if any(
        manifest.get(field) is not expected for field, expected in expected_build_runtime.items()
    ):
        raise ValueError("standalone build manifest changed its .NET runtime boundary")
    if manifest.get("local_comsol_installation_required") is not True:
        raise ValueError("standalone build manifest changed its COMSOL boundary")

    sources = manifest.get("sources")
    if not isinstance(sources, dict):
        raise ValueError("standalone build manifest has no source identities")
    for name in ("Launcher.cs", "CapacitorPointTemplate.java"):
        expected = _resource_bytes(name)
        record = sources.get(name)
        if not isinstance(record, dict) or record.get("sha256") != _sha256(expected):
            raise ValueError("standalone build source identity does not match this package")
        if record.get("byte_count") != len(expected):
            raise ValueError("standalone build source size does not match this package")

    executable_path = root / EXECUTABLE_NAME
    if executable_path.is_symlink() or not executable_path.is_file():
        raise ValueError("standalone executable is absent or not a regular file")
    executable = read_file_bytes_bounded(executable_path, max_bytes=MAX_EXECUTABLE_BYTES)
    launcher = manifest.get("launcher")
    if not isinstance(launcher, dict) or launcher.get("name") != EXECUTABLE_NAME:
        raise ValueError("standalone launcher manifest is invalid")
    if launcher.get("sha256") != _sha256(executable) or launcher.get("byte_count") != len(
        executable
    ):
        raise ValueError("standalone executable does not match its build manifest")
    return {
        "schema_name": BUILD_SCHEMA,
        "schema_version": BUILD_SCHEMA_VERSION,
        "status": "verified",
        "launcher_sha256": launcher["sha256"],
        "launcher_byte_count": launcher["byte_count"],
        "target_os": manifest.get("target_os"),
        "target_comsol": manifest.get("target_comsol"),
        "python_required_at_runtime": False,
        **expected_build_runtime,
        "local_comsol_installation_required": True,
    }


def read_campaign_results(
    campaign_directory: str | Path, *, limit: int = MAX_RESULT_ROWS
) -> dict[str, Any]:
    """Read complete result rows; a partial crash tail is reported but never accepted."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULT_ROWS:
        raise ValueError(f"limit must be an integer from 1 through {MAX_RESULT_ROWS}")
    root = _campaign_root(campaign_directory)
    results_path = root / "assets" / "data" / "results.jsonl"
    if results_path.is_file():
        result_payload = read_file_bytes_bounded(results_path, max_bytes=MAX_RESULTS_BYTES)
        complete_end = result_payload.rfind(b"\n") + 1
        complete = result_payload[:complete_end]
        trailing = result_payload[complete_end:]
        parsed_records: list[Any] = []
        try:
            for line_number, line in enumerate(complete.splitlines(), start=1):
                if not line:
                    raise ValueError(f"empty JSONL record at line {line_number}")
                value = json.loads(line.decode("utf-8"))
                validate_finite_json(value)
                parsed_records.append(value)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            outcome = {
                "state": "corrupt",
                "records": [],
                "complete_byte_count": 0,
                "error_type": type(exc).__name__,
            }
        else:
            outcome = {
                "state": "incomplete" if trailing else "current_valid",
                "records": parsed_records,
                "complete_byte_count": complete_end,
                "trailing_byte_count": len(trailing),
            }
    else:
        result_payload = b""
        outcome = {"state": "absent", "records": [], "complete_byte_count": 0}
    raw_records = outcome.get("records", [])
    if not isinstance(raw_records, list):
        raise ValueError("standalone result snapshot records are invalid")
    records = raw_records
    if len(records) > MAX_RESULT_ROWS:
        raise ValueError("standalone result row count exceeds its bound")
    accepted: list[dict[str, Any]] = []
    point_ids: set[str] = set()
    shared_identity: tuple[str, ...] | None = None
    for value in records:
        if not isinstance(value, dict):
            raise ValueError("standalone result row must be a JSON object")
        if (
            value.get("schema_name") != RESULT_SCHEMA
            or value.get("schema_version") != RESULT_SCHEMA_VERSION
        ):
            raise ValueError("standalone result schema is unsupported")
        if value.get("event") != "point_result" or value.get("status") != "passed":
            raise ValueError("standalone result row is not an accepted point")
        point_id = value.get("point_id")
        attempt_id = value.get("attempt_id")
        comsol_version = value.get("comsol_version")
        if not isinstance(point_id, str) or not point_id or point_id in point_ids:
            raise ValueError("standalone result point identity is absent or duplicated")
        if not isinstance(attempt_id, str) or len(attempt_id) != 32:
            raise ValueError("standalone result attempt identity is invalid")
        if not isinstance(comsol_version, str) or not comsol_version.startswith("6.4."):
            raise ValueError("standalone result is not bound to COMSOL 6.4")
        for field in _RESULT_HASH_FIELDS:
            _require_sha256(value.get(field), field=field)
        identity = tuple(
            str(value[field])
            for field in (
                "launcher_sha256",
                "campaign_spec_sha256",
                "comsol_version",
                "comsol_compile_sha256",
                "comsol_batch_sha256",
            )
        )
        if shared_identity is None:
            shared_identity = identity
        elif identity != shared_identity:
            raise ValueError("standalone result rows have mixed execution identities")
        point_ids.add(point_id)
        accepted.append(json.loads(json.dumps(value)))
    journal_sha256 = _sha256(result_payload)
    result = {
        "state": outcome["state"],
        "complete_byte_count": outcome["complete_byte_count"],
        "trailing_byte_count": outcome.get("trailing_byte_count", 0),
        "total_rows": len(accepted),
        "rows": accepted[-limit:],
        "results_sha256": journal_sha256,
    }
    terminal_path = root / "assets" / "state" / "terminal.json"
    terminal = read_campaign_terminal(root) if terminal_path.is_file() else None
    if terminal is not None and terminal.get("status") == "completed":
        if terminal.get("results_sha256") != journal_sha256:
            raise ValueError("standalone terminal receipt does not bind the result journal")
    result["terminal"] = terminal
    return result


def tail_campaign_log(
    campaign_directory: str | Path,
    *,
    log_name: str = "launcher.log",
    lines: int = 100,
) -> dict[str, Any]:
    """Return a bounded UTF-8 tail from one allowlisted launcher log."""
    if log_name not in {"launcher.log", "compile.log", "current-point.log"}:
        raise ValueError("log_name is not allowlisted")
    if isinstance(lines, bool) or not isinstance(lines, int) or not 1 <= lines <= MAX_TAIL_LINES:
        raise ValueError(f"lines must be an integer from 1 through {MAX_TAIL_LINES}")
    root = _campaign_root(campaign_directory)
    path = root / "assets" / "logs" / log_name
    payload = read_file_bytes_bounded(path, max_bytes=MAX_LOG_BYTES)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("standalone log is not valid UTF-8") from exc
    all_lines = text.splitlines()
    selected = all_lines[-lines:]
    return {
        "log_name": log_name,
        "byte_count": len(payload),
        "total_lines": len(all_lines),
        "returned_lines": len(selected),
        "lines": selected,
    }


__all__ = [
    "MAX_RESULT_ROWS",
    "MAX_TAIL_LINES",
    "read_campaign_terminal",
    "read_campaign_results",
    "read_campaign_status",
    "tail_campaign_log",
    "verify_standalone_deployment",
]
