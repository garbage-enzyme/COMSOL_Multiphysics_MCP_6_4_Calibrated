"""Host admission lock for transport selection (0.8.2 §10 startup gate).

One host per canonical ownership domain. Both the Python entry point and
the Rust shim acquire this lock before serving MCP requests. The lock is
separate from the solver lease: the shim never acquires a solver lease,
but it does acquire host admission.

File: `<runtime>/host_admission.lock`
Schema: `comsol_mcp.host_admission` version `1.0.0`
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from comsol_mcp.durable.io import atomic_write_bytes_exclusive

HOST_ADMISSION_SCHEMA = "comsol_mcp.host_admission"
HOST_ADMISSION_VERSION = "1.0.0"
CREATE_TIME_TOLERANCE_SECONDS = 0.05
LOCK_TIMEOUT_SECONDS = 2.0
LOCK_POLL_SECONDS = 0.05


class HostAdmissionError(RuntimeError):
    """Host admission was refused."""

    def __init__(self, reason_code: str, message: str, details: dict[str, Any] | None = None):
        self.reason_code = reason_code
        self.details = details or {}
        super().__init__(message)


def _process_identity(pid: int) -> dict[str, Any]:
    """Capture the birth identity of one process."""
    import hashlib

    import psutil

    proc = psutil.Process(pid)
    cmdline = proc.cmdline() or [sys.executable, *sys.argv]
    signature = hashlib.sha256("\0".join(cmdline).encode()).hexdigest()
    return {
        "pid": pid,
        "process_create_time": proc.create_time(),
        "command_signature": signature,
    }


def _read_lock(path: Path) -> tuple[dict[str, Any] | None, bytes | None, str | None]:
    """Read the admission lock. Returns (lock, raw_bytes, error)."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, None, None
    except OSError as exc:
        return None, None, str(exc)
    try:
        lock = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None, raw, "lock is not valid JSON"
    if not isinstance(lock, dict):
        return None, raw, "lock is not a JSON object"
    return lock, raw, None


def _owner_state(lock: dict[str, Any]) -> str:
    """Classify the lock owner: active, stale, or uncertain."""
    import psutil

    pid = lock.get("pid")
    create_time = lock.get("process_create_time")
    signature = lock.get("command_signature")
    if not isinstance(pid, int) or pid <= 0:
        return "uncertain"
    if not isinstance(create_time, (int, float)):
        return "uncertain"
    try:
        proc = psutil.Process(pid)
        actual_time = proc.create_time()
        if abs(actual_time - create_time) > CREATE_TIME_TOLERANCE_SECONDS:
            return "stale"
        if signature:
            import hashlib

            cmdline = proc.cmdline() or []
            actual_sig = hashlib.sha256("\0".join(cmdline).encode()).hexdigest()
            if actual_sig != signature:
                return "stale"
        return "active"
    except psutil.NoSuchProcess:
        return "stale"
    except (psutil.AccessDenied, psutil.ZombieProcess):
        return "uncertain"


def acquire(
    runtime_dir: str | Path,
    *,
    transport_mode: str,
    pid: int | None = None,
) -> dict[str, Any]:
    """Acquire host admission for one ownership domain.

    Returns the lock document on success. Raises HostAdmissionError on
    refusal (existing host, mode mismatch, uncertain state).
    """
    path = Path(runtime_dir) / "host_admission.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    identity = _process_identity(pid or os.getpid())

    lock, raw, error = _read_lock(path)
    if error:
        raise HostAdmissionError(
            "admission_lock_corrupt",
            f"host admission lock is unreadable: {error}",
            {"path": str(path)},
        )
    if lock is not None:
        state = _owner_state(lock)
        if state == "active":
            raise HostAdmissionError(
                "admission_domain_busy",
                "another host is active in this ownership domain",
                {
                    "owner_pid": lock.get("pid"),
                    "owner_transport": lock.get("transport_mode"),
                    "owner_acquired_at": lock.get("acquired_at_epoch"),
                },
            )
        if state == "uncertain":
            raise HostAdmissionError(
                "admission_state_uncertain",
                "the existing host admission lock has an uncertain owner; "
                "resolve it before starting a new host",
                {"owner_pid": lock.get("pid")},
            )
        # HI-03 fix: stale reclaim must be atomic against concurrent
        # replacement. Use unlink_if_content (opened-file compare-and-delete)
        # so a new owner that replaced the file between our read and delete
        # is preserved, not erased. If the bytes changed, another process
        # won the race; retry once.
        from comsol_mcp.durable.io import unlink_if_content

        if not unlink_if_content(path, raw or b""):
            # Another process replaced the lock between our read and delete.
            # Re-read and classify the new owner.
            new_lock, _new_raw, _new_err = _read_lock(path)
            if new_lock is not None:
                new_state = _owner_state(new_lock)
                if new_state == "active":
                    raise HostAdmissionError(
                        "admission_domain_busy",
                        "another host acquired admission concurrently",
                        {"owner_pid": new_lock.get("pid")},
                    )
            # If still stale or absent, proceed to create our lock below.
            # The exclusive create below handles the final race.

    document = {
        "schema_name": HOST_ADMISSION_SCHEMA,
        "schema_version": HOST_ADMISSION_VERSION,
        "transport_mode": transport_mode,
        "pid": identity["pid"],
        "process_create_time": identity["process_create_time"],
        "command_signature": identity["command_signature"],
        "acquired_at_epoch": time.time(),
    }
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        atomic_write_bytes_exclusive(path, payload)
    except FileExistsError:
        raise HostAdmissionError(
            "admission_domain_busy",
            "another process acquired host admission concurrently",
        ) from None
    return document


def release(runtime_dir: str | Path, *, pid: int | None = None) -> bool:
    """Release the admission lock if this process owns it."""
    path = Path(runtime_dir) / "host_admission.lock"
    lock, raw, _error = _read_lock(path)
    if lock is None:
        return True
    owner_pid = lock.get("pid")
    if owner_pid != (pid or os.getpid()):
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def status(runtime_dir: str | Path) -> dict[str, Any]:
    """Report the current admission state for diagnostics."""
    path = Path(runtime_dir) / "host_admission.lock"
    lock, _raw, error = _read_lock(path)
    if lock is None and error is None:
        return {"state": "absent", "path": str(path)}
    if lock is None:
        return {"state": "corrupt", "path": str(path), "error": error}
    return {
        "state": _owner_state(lock),
        "path": str(path),
        "transport_mode": lock.get("transport_mode"),
        "pid": lock.get("pid"),
        "acquired_at_epoch": lock.get("acquired_at_epoch"),
    }


def validate_transport_mode(
    settings: dict[str, Any],
    *,
    actual_mode: str,
    settings_source: str = "",
) -> None:
    """Refuse a host whose actual mode differs from the configured mode."""
    configured = settings.get("transport", "python")
    if configured == actual_mode:
        return
    raise HostAdmissionError(
        "transport_mode_mismatch",
        f"settings select transport '{configured}' but this host is "
        f"'{actual_mode}'. Correct the client launch command or change the "
        f"transport setting.",
        {
            "configured_mode": configured,
            "actual_mode": actual_mode,
            "settings_source": settings_source,
        },
    )
