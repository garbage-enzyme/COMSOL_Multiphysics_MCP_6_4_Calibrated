"""Stdlib-only parent client and exact-child manager for the H4 worker."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time
from typing import Any
import uuid

from .semantic_contracts import PUBLIC_LIMITS, WORKER_PROTOCOL_SCHEMA_VERSION


CREATE_TIME_TOLERANCE_SECONDS = 0.05


def _command_signature(command: list[str]) -> str:
    return hashlib.sha256("\0".join(command).encode("utf-8", errors="replace")).hexdigest()


def _windows_process_create_time(handle: int) -> float | None:
    if os.name != "nt":
        return None
    created = wintypes.FILETIME()
    exited = wintypes.FILETIME()
    kernel = wintypes.FILETIME()
    user = wintypes.FILETIME()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME))
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    if not kernel32.GetProcessTimes(wintypes.HANDLE(handle), ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
        return None
    ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
    return ticks / 10_000_000.0 - 11_644_473_600.0


class _KillOnCloseJob:
    def __init__(self, handle: int):
        self.handle = handle

    @classmethod
    def assign(cls, process_handle: int) -> "_KillOnCloseJob | None":
        if os.name != "nt":
            return None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD)
        kernel32.SetInformationJobObject.restype = wintypes.BOOL

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None
        limits = Extended()
        limits.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel32.AssignProcessToJobObject(handle, wintypes.HANDLE(process_handle)):
            kernel32.CloseHandle(handle)
            return None
        return cls(int(handle))

    def close(self) -> None:
        if self.handle and os.name == "nt":
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(wintypes.HANDLE(self.handle))
            self.handle = 0


class SemanticWorkerManager:
    """Own exactly one localhost semantic worker and never retries a query."""

    def __init__(self, *, python_executable: str | None = None, startup_deadline: float = 5.0, query_deadline: float = PUBLIC_LIMITS["query_deadline_seconds"], idle_ttl: float = 300.0, fault: str | None = None, query_delay: float = 0.0, forced_port: int = 0, backend: str = "fake", deployment_root: str | None = None, lexical_index: str | None = None, model_path: str | None = None):
        self.python_executable = python_executable or sys.executable
        self.startup_deadline = float(startup_deadline)
        self.query_deadline = float(query_deadline)
        self.idle_ttl = float(idle_ttl)
        self.fault = fault
        self.query_delay = float(query_delay)
        self.forced_port = int(forced_port)
        if backend not in {"fake", "hybrid"}:
            raise ValueError("backend must be fake or hybrid")
        if backend == "hybrid" and not all((deployment_root, lexical_index, model_path)):
            raise ValueError("hybrid backend requires deployment_root, lexical_index, and model_path")
        self.backend = backend
        self.deployment_root = deployment_root
        self.lexical_index = lexical_index
        self.model_path = model_path
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._identity: dict[str, Any] | None = None
        self._job: _KillOnCloseJob | None = None
        self._token: str | None = None
        self._port: int | None = None
        self._last_activity: float | None = None
        self._last_error: dict[str, Any] | None = None

    def _command(self) -> list[str]:
        command = [self.python_executable, "-m", "src.knowledge.semantic_worker", "--serve", "--port", str(self.forced_port)]
        command.extend(["--backend", self.backend])
        if self.backend == "hybrid":
            command.extend([
                "--deployment-root", str(self.deployment_root),
                "--lexical-index", str(self.lexical_index),
                "--model-path", str(self.model_path),
            ])
        if self.fault:
            command.extend(["--fault", self.fault])
        if self.query_delay:
            command.extend(["--query-delay", str(self.query_delay)])
        return command

    def _identity_state(self) -> str:
        if self._process is None or self._identity is None:
            return "stopped"
        if self._process.poll() is not None:
            return "stale"
        if self._process.pid != self._identity.get("pid"):
            return "stale"
        if self._identity.get("command_signature") != _command_signature(self._command()):
            return "stale"
        actual = _windows_process_create_time(int(self._process._handle)) if os.name == "nt" else self._identity.get("process_create_time")
        if actual is None or abs(float(actual) - float(self._identity.get("process_create_time", -1))) > CREATE_TIME_TOLERANCE_SECONDS:
            return "uncertain"
        return "active"

    def start(self) -> dict[str, Any]:
        with self._lock:
            self._expire_idle()
            if self._identity_state() == "active":
                return {"success": True, "started": False, "identity": dict(self._identity or {}), "port": self._port}
            if self._process is not None:
                cleanup = self._terminate_owned("replace_nonactive_worker")
                if cleanup.get("refused"):
                    error = {
                        "code": "worker_identity_uncertain",
                        "message": "refusing to replace a live worker whose exact identity does not match",
                    }
                    self._last_error = error
                    return {"success": False, "error": error, "cleanup": cleanup}
            token = secrets.token_hex(32)
            command = self._command()
            environment = os.environ.copy()
            environment["COMSOL_SEMANTIC_SESSION_TOKEN"] = token
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, creationflags=creationflags)
            self._process = process
            created = _windows_process_create_time(int(process._handle)) if os.name == "nt" else time.time()
            self._identity = {"pid": process.pid, "process_create_time": created, "command_signature": _command_signature(command)}
            self._job = _KillOnCloseJob.assign(int(process._handle)) if os.name == "nt" else None
            self._token = token
            line_queue: queue.Queue[bytes] = queue.Queue(maxsize=1)
            assert process.stdout is not None
            threading.Thread(target=lambda: line_queue.put(process.stdout.readline()), daemon=True).start()
            try:
                line = line_queue.get(timeout=self.startup_deadline)
                ready = json.loads(line.decode("utf-8"))
                if ready.get("schema_version") != WORKER_PROTOCOL_SCHEMA_VERSION or ready.get("event") != "ready" or ready.get("pid") != process.pid or ready.get("host") != "127.0.0.1":
                    raise RuntimeError("invalid worker startup handshake")
                self._port = int(ready["port"])
                self._last_activity = time.monotonic()
                return {"success": True, "started": True, "identity": dict(self._identity), "port": self._port, "job_object_contained": self._job is not None}
            except (queue.Empty, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError, RuntimeError) as exc:
                error = {"code": "startup_failed", "message": str(exc) or "startup deadline exceeded"}
                self._last_error = error
                cleanup = self._terminate_owned("startup_failure")
                return {"success": False, "error": error, "cleanup": cleanup}

    def _expire_idle(self) -> None:
        if self._last_activity is not None and self.idle_ttl >= 0 and time.monotonic() - self._last_activity >= self.idle_ttl:
            self._terminate_owned("idle_ttl")

    def _terminate_owned(self, reason: str) -> dict[str, Any]:
        state = self._identity_state()
        identity = dict(self._identity or {})
        acted = False
        if self._process is not None and self._process.poll() is None and state != "active":
            return {
                "reason": reason,
                "identity": identity,
                "identity_state": state,
                "acted": False,
                "absent": False,
                "refused": True,
            }
        if self._process is not None and state == "active":
            if self._job is not None:
                self._job.close()
                acted = True
            else:
                self._process.terminate()
                acted = True
            try:
                self._process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                if self._identity_state() == "active":
                    self._process.kill()
                    self._process.wait(timeout=2.0)
        elif self._process is not None and self._process.poll() is not None:
            acted = False
        absent = self._process is None or self._process.poll() is not None
        if self._job is not None:
            self._job.close()
        self._process = None
        self._identity = None
        self._job = None
        self._token = None
        self._port = None
        self._last_activity = None
        return {"reason": reason, "identity": identity, "identity_state": state, "acted": acted, "absent": absent}

    def reset(self) -> dict[str, Any]:
        with self._lock:
            result = self._terminate_owned("explicit_reset")
            return {"success": not result.get("refused", False), "reset": result}

    def _request(self, operation: str, fields: dict[str, Any], deadline: float) -> dict[str, Any]:
        started = self.start()
        if not started.get("success"):
            return started
        request_id = f"semantic-{uuid.uuid4().hex}"
        request = {"schema_version": WORKER_PROTOCOL_SCHEMA_VERSION, "request_id": request_id, "token": self._token, "operation": operation, **fields}
        encoded = json.dumps(request, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            with socket.create_connection(("127.0.0.1", int(self._port)), timeout=deadline) as connection:
                connection.settimeout(deadline)
                connection.sendall(encoded)
                chunks = bytearray()
                while not chunks.endswith(b"\n"):
                    block = connection.recv(min(4096, PUBLIC_LIMITS["maximum_response_bytes"] + 1 - len(chunks)))
                    if not block:
                        raise RuntimeError("worker closed connection before a complete response")
                    chunks.extend(block)
                    if len(chunks) > PUBLIC_LIMITS["maximum_response_bytes"]:
                        raise RuntimeError("worker response exceeds maximum_response_bytes")
            response = json.loads(bytes(chunks).decode("utf-8"))
            if not isinstance(response, dict) or response.get("schema_version") != WORKER_PROTOCOL_SCHEMA_VERSION or response.get("request_id") != request_id:
                raise RuntimeError("worker response identity or schema mismatch")
            self._last_activity = time.monotonic()
            return response
        except (OSError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as exc:
            error = {"code": "worker_protocol_failure", "message": f"{type(exc).__name__}: {exc}"}
            self._last_error = error
            cleanup = self._terminate_owned("protocol_failure")
            return {"success": False, "error": error, "cleanup": cleanup, "request_id": request_id, "retried": False}

    def query(self, query: str, *, limit: int = 5, filters: dict[str, Any] | None = None, retrieval_mode: str = "hybrid") -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip() or len(query) > PUBLIC_LIMITS["maximum_query_characters"]:
            return {"success": False, "error": {"code": "invalid_query", "message": "query violates public limits"}}
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= PUBLIC_LIMITS["maximum_results"]:
            return {"success": False, "error": {"code": "invalid_limit", "message": "limit violates public limits"}}
        if filters is not None and not isinstance(filters, dict):
            return {"success": False, "error": {"code": "invalid_filters", "message": "filters must be an object"}}
        if retrieval_mode not in {"hybrid", "vector", "lexical"}:
            return {"success": False, "error": {"code": "invalid_retrieval_mode", "message": "retrieval_mode is unsupported"}}
        with self._lock:
            return self._request("query", {"query": query.strip(), "limit": limit, "filters": filters, "retrieval_mode": retrieval_mode}, self.query_deadline)

    def health(self) -> dict[str, Any]:
        with self._lock:
            return self._request("health", {}, PUBLIC_LIMITS["status_deadline_seconds"])

    def status(self, *, probe: bool = True) -> dict[str, Any]:
        with self._lock:
            self._expire_idle()
            state = self._identity_state()
            base = {"state": state, "identity": dict(self._identity or {}), "port": self._port, "last_error": self._last_error}
            if state != "active" or not probe:
                return base
            return {**base, "health": self._request("status", {}, PUBLIC_LIMITS["status_deadline_seconds"])}

    def __enter__(self) -> "SemanticWorkerManager":
        return self

    def __exit__(self, *_args: object) -> None:
        self.reset()


__all__ = ["SemanticWorkerManager"]
