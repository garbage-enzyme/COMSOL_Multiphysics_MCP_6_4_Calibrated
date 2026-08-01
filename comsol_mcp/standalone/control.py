"""Bounded Windows control plane for reviewed standalone COMSOL campaigns."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import psutil

from comsol_mcp.jobs.store import process_identity

from .builder import EXECUTABLE_NAME
from .inspection import (
    MAX_STATUS_BYTES,
    read_campaign_results,
    read_campaign_status,
    tail_campaign_log,
    verify_standalone_deployment,
)

CONTROL_TIMEOUT_SECONDS = 5.0
MAX_MCP_LAUNCH_RECORDS = 8
_PROCESS_LOCK = threading.Lock()
_PROCESS_WAKE = threading.Event()
_PROCESSES: set[Any] = set()
_REAPER_STARTED = False


def _reaper() -> None:
    while True:
        _PROCESS_WAKE.wait()
        with _PROCESS_LOCK:
            completed = {process for process in _PROCESSES if process.poll() is not None}
            _PROCESSES.difference_update(completed)
            active = bool(_PROCESSES)
            if not active:
                _PROCESS_WAKE.clear()
        if active:
            time.sleep(0.05)


def _track(process: Any) -> None:
    global _REAPER_STARTED
    with _PROCESS_LOCK:
        _PROCESSES.add(process)
        if not _REAPER_STARTED:
            threading.Thread(
                target=_reaper,
                name="comsol-standalone-process-reaper",
                daemon=True,
            ).start()
            _REAPER_STARTED = True
    _PROCESS_WAKE.set()


def _validate_comsol_root(value: str | Path) -> Path:
    text = str(value)
    if not text or len(text) > 4096 or not text.isascii():
        raise ValueError("COMSOL root must be a bounded ASCII path")
    root = Path(text)
    if not root.is_absolute() or str(root).startswith("\\\\"):
        raise ValueError("COMSOL root must be an absolute local path")
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("COMSOL root cannot be resolved") from exc
    is_junction = getattr(root, "is_junction", lambda: False)
    if root.is_symlink() or is_junction() or not resolved.is_dir():
        raise ValueError("COMSOL root must be a regular local directory")
    required = (
        resolved / "bin" / "win64" / "comsolcompile.exe",
        resolved / "bin" / "win64" / "comsolbatch.exe",
        resolved / "java" / "win64" / "jre" / "bin" / "java.exe",
    )
    if any(path.is_symlink() or not path.is_file() for path in required):
        raise ValueError("COMSOL 6.4 installation is incomplete")
    return resolved


def _decode_json_output(payload: bytes, *, contract: str) -> dict[str, Any]:
    if len(payload) > MAX_STATUS_BYTES:
        raise RuntimeError(f"{contract} output exceeded its bound")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{contract} output was not valid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{contract} output was not a JSON object")
    return value


def _run_control(deployment_directory: str | Path, command: str) -> dict[str, Any]:
    deployment = Path(deployment_directory)
    identity = verify_standalone_deployment(deployment)
    completed = subprocess.run(  # noqa: S603
        [str(deployment / EXECUTABLE_NAME), command],
        cwd=deployment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=CONTROL_TIMEOUT_SECONDS,
        check=False,
        creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
    )
    if len(completed.stdout) + len(completed.stderr) > MAX_STATUS_BYTES:
        raise RuntimeError("standalone control output exceeded its bound")
    if completed.returncode != 0:
        raise RuntimeError("standalone control command failed")
    value = _decode_json_output(completed.stdout, contract="standalone control")
    value["deployment_identity"] = identity
    return value


def standalone_status(deployment_directory: str | Path) -> dict[str, Any]:
    """Return live owner evidence plus the independently readable status projection."""
    live = _run_control(deployment_directory, "status")
    stored = read_campaign_status(deployment_directory)
    if live.get("launcher_sha256") != stored.get("launcher_sha256"):
        raise RuntimeError("live and stored standalone identities disagree")
    if live.get("launcher_sha256") != live["deployment_identity"]["launcher_sha256"]:
        raise RuntimeError("standalone status does not match the reviewed deployment")
    return {"success": True, **live}


def request_standalone_pause(deployment_directory: str | Path) -> dict[str, Any]:
    """Write one attempt-bound point-boundary pause request through the reviewed EXE."""
    value = _run_control(deployment_directory, "pause")
    if value.get("schema_name") != "comsol_mcp.standalone_pause_request":
        raise RuntimeError("standalone pause acknowledgement contract failed")
    return {"success": True, **value}


def launch_standalone_campaign(
    deployment_directory: str | Path,
    comsol_root: str | Path,
    *,
    resume: bool = False,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    identity_provider: Callable[[int], dict[str, Any]] = process_identity,
) -> dict[str, Any]:
    """Detach one reviewed launcher without routing its output through MCP transport."""
    deployment = Path(deployment_directory)
    build = verify_standalone_deployment(deployment)
    comsol = _validate_comsol_root(comsol_root)
    launch_root = deployment / "assets" / "mcp-launches"
    launch_root.mkdir(parents=True, exist_ok=True)
    existing_launch_ids = {
        path.name.split(".", 1)[0] for path in launch_root.iterdir() if path.is_file()
    }
    if len(existing_launch_ids) >= MAX_MCP_LAUNCH_RECORDS:
        raise RuntimeError("standalone MCP launch record limit reached")
    launch_id = uuid.uuid4().hex
    stdout_path = launch_root / f"{launch_id}.stdout.log"
    stderr_path = launch_root / f"{launch_id}.stderr.log"
    record_path = launch_root / f"{launch_id}.json"
    command = [
        str(deployment / EXECUTABLE_NAME),
        "resume" if resume else "run",
        "--comsol-path",
        str(comsol),
    ]
    flags = 0
    if os.name == "nt":
        flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    with (
        stdout_path.open("xb", buffering=0) as stdout,
        stderr_path.open("xb", buffering=0) as stderr,
    ):
        process = popen_factory(
            command,
            cwd=deployment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            close_fds=True,
            creationflags=flags,
            start_new_session=(os.name != "nt"),
        )
    _track(process)
    deadline = time.monotonic() + 2.0
    while True:
        try:
            owner = identity_provider(int(process.pid))
            break
        except psutil.NoSuchProcess:
            if time.monotonic() >= deadline:
                raise RuntimeError("standalone launcher exited before identity capture")
            time.sleep(0.01)
    record = {
        "schema_name": "comsol_mcp.standalone_mcp_launch",
        "schema_version": "1.0.0",
        "launch_id": launch_id,
        "operation": "resume" if resume else "run",
        "owner": owner,
        "launcher_sha256": build["launcher_sha256"],
        "comsol_root_included": False,
    }
    from comsol_mcp.durable.io import atomic_write_json

    atomic_write_json(record_path, record)
    return {
        "success": True,
        "state": "launch_requested",
        "launch_id": launch_id,
        "operation": record["operation"],
        "owner": owner,
        "deployment_identity": build,
        "comsol_root_included": False,
    }


def read_standalone_results(deployment_directory: str | Path, *, limit: int) -> dict[str, Any]:
    deployment = verify_standalone_deployment(deployment_directory)
    result = read_campaign_results(deployment_directory, limit=limit)
    rows = result["rows"]
    if any(row.get("launcher_sha256") != deployment["launcher_sha256"] for row in rows):
        raise RuntimeError("standalone results do not match the reviewed deployment")
    terminal = result.get("terminal")
    if terminal is not None and terminal.get("launcher_sha256") != deployment["launcher_sha256"]:
        raise RuntimeError("standalone terminal does not match the reviewed deployment")
    if terminal is not None and rows:
        row = rows[0]
        for field in (
            "launcher_sha256",
            "campaign_spec_sha256",
            "comsol_version",
            "comsol_compile_sha256",
            "comsol_batch_sha256",
        ):
            if terminal.get(field) != row.get(field):
                raise RuntimeError("standalone terminal and result identities disagree")
    return {"success": True, **result}


def tail_standalone_log(
    deployment_directory: str | Path, *, log_name: str, lines: int
) -> dict[str, Any]:
    verify_standalone_deployment(deployment_directory)
    return {
        "success": True,
        **tail_campaign_log(deployment_directory, log_name=log_name, lines=lines),
    }


__all__ = [
    "launch_standalone_campaign",
    "read_standalone_results",
    "request_standalone_pause",
    "standalone_status",
    "tail_standalone_log",
]
