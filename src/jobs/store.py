"""Crash-durable job artifacts, state transitions, and process-safe locking."""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Iterator
import uuid

import psutil


JOB_SCHEMA_VERSION = "1"
CREATE_TIME_TOLERANCE_SECONDS = 0.05
ACTIVE_STATES = {
    "submitted",
    "starting",
    "smoke_running",
    "smoke_validated",
    "running",
    "cancel_requested",
}
TERMINAL_STATES = {"completed", "failed", "interrupted"}
TRANSITIONS = {
    "submitted": {"starting", "failed", "interrupted", "cancel_requested"},
    "starting": {"smoke_running", "failed", "interrupted", "cancel_requested"},
    "smoke_running": {"smoke_validated", "failed", "interrupted", "cancel_requested"},
    "smoke_validated": {"running", "completed", "failed", "interrupted", "cancel_requested"},
    "running": {"completed", "failed", "interrupted", "cancel_requested"},
    "cancel_requested": {"interrupted", "failed"},
    "failed": {"starting"},
    "interrupted": {"starting"},
    "completed": set(),
}


def default_jobs_root() -> Path:
    configured = os.environ.get("COMSOL_MCP_JOBS_DIR")
    if configured:
        return Path(configured)
    if os.name == "nt" and Path("D:/").exists():
        return Path("D:/comsol_runtime/jobs")
    return Path(tempfile.gettempdir()) / "comsol_runtime" / "jobs"


def _require_ascii_path(path: Path) -> None:
    try:
        str(path.resolve()).encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("Durable job runtime paths must contain ASCII characters only") from exc


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        deadline = time.monotonic() + 1.0
        while True:
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                # Windows file scanners can briefly hold a just-flushed JSON
                # file open.  Keep the same complete temp file and retry the
                # atomic replacement; never fall back to in-place truncation.
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read durable job artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Durable job artifact must contain a JSON object: {path}")
    return value


def process_identity(pid: int) -> dict[str, Any]:
    process = psutil.Process(int(pid))
    with process.oneshot():
        command = list(process.cmdline())
        return {
            "pid": process.pid,
            "process_create_time": process.create_time(),
            "command_signature": hashlib.sha256(
                "\0".join(command).encode("utf-8", errors="replace")
            ).hexdigest(),
        }


def process_identity_state(identity: dict[str, Any]) -> tuple[str, str]:
    try:
        actual = process_identity(int(identity["pid"]))
    except (KeyError, TypeError, ValueError):
        return "uncertain", "process identity fields are missing or invalid"
    except psutil.NoSuchProcess:
        return "stale", "worker PID no longer exists"
    except (psutil.AccessDenied, psutil.ZombieProcess, OSError) as exc:
        return "uncertain", f"worker identity cannot be inspected: {exc}"
    expected_time = identity.get("process_create_time")
    if expected_time is None:
        return "uncertain", "worker creation time is missing"
    if abs(float(actual["process_create_time"]) - float(expected_time)) > CREATE_TIME_TOLERANCE_SECONDS:
        return "stale", "worker PID was reused"
    expected_signature = identity.get("command_signature")
    if expected_signature and actual["command_signature"] != expected_signature:
        return "stale", "worker command line no longer matches"
    return "active", "worker PID, creation time, and command line match"


class JobLock:
    """Exclusive file lock that removes only locks proven stale by process identity."""

    def __init__(self, path: Path, timeout: float = 5.0, poll_interval: float = 0.025):
        self.path = path
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        self.identity = process_identity(os.getpid())
        self.identity["created_at_epoch"] = time.time()
        self._owned_bytes: bytes | None = None

    def acquire(self) -> None:
        deadline = time.monotonic() + self.timeout
        payload = _json_bytes(self.identity)
        while True:
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            except FileExistsError:
                try:
                    observed = self.path.read_bytes()
                    existing = json.loads(observed.decode("utf-8"))
                    state, _ = process_identity_state(existing)
                    if state == "stale" and self.path.read_bytes() == observed:
                        self._unlink_with_retry(expected=observed)
                        continue
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for durable job lock: {self.path}")
                time.sleep(self.poll_interval)
                continue
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self._owned_bytes = payload
            return

    def _unlink_with_retry(self, *, expected: bytes) -> bool:
        deadline = time.monotonic() + 2.0
        while True:
            try:
                if self.path.read_bytes() != expected:
                    return False
                self.path.unlink()
                return True
            except FileNotFoundError:
                return True
            except PermissionError:
                # A polling process or file scanner can momentarily hold the
                # lock file without owning it on Windows.  Revalidate its exact
                # bytes on every retry so a replaced/foreign lock is never removed.
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)

    def release(self) -> None:
        if self._owned_bytes is None:
            return
        try:
            self._unlink_with_retry(expected=self._owned_bytes)
        finally:
            self._owned_bytes = None

    def __enter__(self) -> "JobLock":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


class JobStore:
    """Own the stable on-disk contract for all durable jobs."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root is not None else default_jobs_root()
        _require_ascii_path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    def job_dir(self, job_id: str) -> Path:
        if not job_id or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in job_id):
            raise ValueError("Invalid job ID")
        path = self.root / job_id
        if path.parent.resolve() != self.root.resolve():
            raise ValueError("Job path escapes the runtime root")
        return path

    def create(self, spec: dict[str, Any], state: dict[str, Any], job_id: str | None = None) -> str:
        job_id = job_id or f"job-{uuid.uuid4().hex}"
        directory = self.job_dir(job_id)
        directory.mkdir(parents=False, exist_ok=False)
        atomic_write_json(directory / "spec.json", spec)
        atomic_write_json(directory / "state.json", state)
        atomic_write_json(directory / "control.json", {"schema_version": JOB_SCHEMA_VERSION})
        (directory / "events.jsonl").touch(exist_ok=False)
        (directory / "worker.log").touch(exist_ok=False)
        _fsync_directory(directory)
        return job_id

    def read_spec(self, job_id: str) -> dict[str, Any]:
        return read_json(self.job_dir(job_id) / "spec.json")

    def read_state(self, job_id: str) -> dict[str, Any]:
        return read_json(self.job_dir(job_id) / "state.json")

    def read_control(self, job_id: str) -> dict[str, Any]:
        return read_json(self.job_dir(job_id) / "control.json")

    def write_control(self, job_id: str, request: str | None = None) -> dict[str, Any]:
        control = {"schema_version": JOB_SCHEMA_VERSION, "request": request, "updated_at_epoch": time.time()}
        atomic_write_json(self.job_dir(job_id) / "control.json", control)
        return control

    @contextmanager
    def lock(self, job_id: str, timeout: float = 5.0) -> Iterator[None]:
        with JobLock(self.job_dir(job_id) / ".state.lock", timeout=timeout):
            yield

    def update_state(
        self,
        job_id: str,
        new_status: str | None = None,
        *,
        patch: dict[str, Any] | None = None,
        event: str | None = None,
        event_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.lock(job_id):
            state = self.read_state(job_id)
            current = str(state.get("status"))
            if new_status is not None and new_status != current:
                if new_status not in TRANSITIONS.get(current, set()):
                    raise ValueError(f"Invalid job state transition: {current} -> {new_status}")
                state["status"] = new_status
            if current == "completed" and patch:
                raise ValueError("Completed job state is immutable")
            state.update(patch or {})
            state["updated_at_epoch"] = time.time()
            atomic_write_json(self.job_dir(job_id) / "state.json", state)
            if event:
                self._append_event_unlocked(job_id, event, event_data or {}, state["status"])
            return state

    def append_event(self, job_id: str, event: str, data: dict[str, Any] | None = None) -> None:
        with self.lock(job_id):
            state = self.read_state(job_id)
            self._append_event_unlocked(job_id, event, data or {}, str(state["status"]))

    def _append_event_unlocked(
        self, job_id: str, event: str, data: dict[str, Any], status: str
    ) -> None:
        record = {
            "schema_version": JOB_SCHEMA_VERSION,
            "timestamp_epoch": time.time(),
            "event": event,
            "status": status,
            "data": data,
        }
        with (self.job_dir(job_id) / "events.jsonl").open("ab") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())

    def tail(self, job_id: str, n: int = 20) -> dict[str, Any]:
        count = max(1, min(int(n), 200))
        directory = self.job_dir(job_id)
        events: deque[str] = deque(maxlen=count)
        with (directory / "events.jsonl").open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                events.append(line.rstrip("\r\n"))
        logs: deque[str] = deque(maxlen=count)
        with (directory / "worker.log").open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                logs.append(line.rstrip("\r\n"))
        return {"job_id": job_id, "limit": count, "events": list(events), "worker_log": list(logs)}
