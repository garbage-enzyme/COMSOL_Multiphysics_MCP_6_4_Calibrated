from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

REQUEST_SCHEMA = "durable_launcher.pause_request.v1"
ACK_SCHEMA = "durable_launcher.pause_ack.v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
_LOGGER = logging.getLogger(__name__)


class _ForeignRequest(RuntimeError):
    """A valid request owned by another job or specification."""


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _replace_durable(temporary, path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _replace_durable(source: Path, destination: Path) -> None:
    """Replace one file and durably publish its directory entry."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move_file = kernel32.MoveFileExW
        move_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
        move_file.restype = wintypes.BOOL
        replace_existing = 0x1
        write_through = 0x8
        if not move_file(str(source), str(destination), replace_existing | write_through):
            error = ctypes.get_last_error()
            raise OSError(error, "Cannot durably replace control file", str(destination))
        return
    os.replace(source, destination)
    descriptor = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validated_request(path: Path, job_id: str, spec_id: str) -> dict[str, Any]:
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unreadable durable control request: {path}") from exc
    if not isinstance(request, dict):
        raise RuntimeError(f"Unreadable durable control request: {path}")
    if request.get("schema_name") != REQUEST_SCHEMA:
        raise RuntimeError(f"Unknown durable control schema: {path}")
    if request.get("action") != "pause_after_current_point":
        raise RuntimeError(f"Unknown durable control action: {path}")
    request_id = request.get("request_id")
    if not isinstance(request_id, str) or not _SAFE_ID.fullmatch(request_id):
        raise RuntimeError(f"Invalid durable control request ID: {path}")
    if request.get("job_id") != job_id:
        raise _ForeignRequest(f"Foreign durable control job identity: {path}")
    expected_spec_id = request.get("expected_spec_id")
    if expected_spec_id is not None and expected_spec_id != "" and expected_spec_id != spec_id:
        raise _ForeignRequest(f"Foreign durable control spec identity: {path}")
    return request


def _ack_matches(path: Path, request: dict[str, Any], job_id: str, spec_id: str) -> bool:
    try:
        ack = json.loads(path.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        return False
    return bool(
        isinstance(ack, dict)
        and ack.get("schema_name") == ACK_SCHEMA
        and ack.get("request_id") == request["request_id"]
        and ack.get("job_id") == job_id
        and ack.get("spec_id") == spec_id
    )


def pending_pause_request(control_dir: Path, *, job_id: str, spec_id: str) -> dict[str, Any] | None:
    requests_dir = control_dir / "requests"
    acks_dir = control_dir / "acks"
    if not requests_dir.is_dir():
        return None
    for path in sorted(requests_dir.glob("*.json"), key=lambda item: item.name):
        try:
            request = _validated_request(path, job_id, spec_id)
        except _ForeignRequest:
            continue
        except RuntimeError as exc:
            _LOGGER.warning("Ignoring malformed durable control request: %s", exc)
            continue
        ack_path = acks_dir / f"{request['request_id']}.json"
        if ack_path.is_file() and _ack_matches(ack_path, request, job_id, spec_id):
            continue
        request["request_path"] = str(path)
        return request
    return None


def acknowledge_pause(
    control_dir: Path,
    request: dict[str, Any],
    *,
    job_id: str,
    spec_id: str,
    completed: int,
    planned: int,
    latest_point_id: str | None,
) -> Path:
    request_id = str(request["request_id"])
    if not _SAFE_ID.fullmatch(request_id):
        raise RuntimeError("Cannot acknowledge an invalid durable control request ID")
    ack_path = control_dir / "acks" / f"{request_id}.json"
    payload = {
        "schema_name": ACK_SCHEMA,
        "schema_version": 1,
        "status": "paused_after_point",
        "action": "pause_after_current_point",
        "request_id": request_id,
        "request_path": request.get("request_path"),
        "job_id": job_id,
        "spec_id": spec_id,
        "completed": int(completed),
        "planned": int(planned),
        "latest_point_id": latest_point_id,
        "acknowledged_at_epoch": time.time(),
        "worker_pid": os.getpid(),
    }
    atomic_json(ack_path, payload)
    return ack_path
