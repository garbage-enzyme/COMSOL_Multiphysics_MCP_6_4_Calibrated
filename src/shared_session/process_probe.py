"""Read-only Windows process, window, listener, and file-version inventory."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import platform
import time
from typing import Any, Callable, Iterable

import psutil


MAX_COMMAND_PARTS = 64
MAX_PROCESS_RECORDS = 4096


def _command_signature(command_line: Iterable[Any]) -> str:
    canonical = "\0".join(str(part) for part in list(command_line)[:MAX_COMMAND_PARTS])
    return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()


def _process_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for process in psutil.process_iter():
        try:
            with process.oneshot():
                try:
                    command_line = list(process.cmdline())[:MAX_COMMAND_PARTS]
                except (psutil.AccessDenied, psutil.ZombieProcess):
                    command_line = []
                try:
                    executable = process.exe()
                except (psutil.AccessDenied, psutil.ZombieProcess):
                    executable = None
                records.append({
                    "pid": process.pid,
                    "parent_pid": process.ppid(),
                    "name": process.name(),
                    "create_time": process.create_time(),
                    "command_line": command_line,
                    "executable": executable,
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        if len(records) >= MAX_PROCESS_RECORDS:
            raise RuntimeError("process inventory exceeds the bounded maximum")
    return records


def _listener_records() -> list[dict[str, Any]]:
    listeners: list[dict[str, Any]] = []
    for connection in psutil.net_connections(kind="tcp"):
        if connection.status != psutil.CONN_LISTEN or connection.pid is None:
            continue
        host = getattr(connection.laddr, "ip", None)
        port = getattr(connection.laddr, "port", None)
        if host is None or port is None:
            continue
        if host not in {"127.0.0.1", "::1"}:
            continue
        listeners.append({"host": host, "port": int(port), "pid": int(connection.pid)})
    return listeners


def _window_state_by_pid() -> dict[int, dict[str, Any]]:
    if platform.system() != "Windows":
        return {}
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    states: dict[int, dict[str, Any]] = {}
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(window, _parameter):
        if not user32.IsWindowVisible(window):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
        if not pid.value:
            return True
        state = states.setdefault(
            int(pid.value), {"window_count": 0, "responding": True}
        )
        state["window_count"] += 1
        if user32.IsHungAppWindow(window):
            state["responding"] = False
        return True

    if not user32.EnumWindows(visit, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return states


class _VS_FIXEDFILEINFO(ctypes.Structure):
    _fields_ = [
        ("dwSignature", wintypes.DWORD),
        ("dwStrucVersion", wintypes.DWORD),
        ("dwFileVersionMS", wintypes.DWORD),
        ("dwFileVersionLS", wintypes.DWORD),
        ("dwProductVersionMS", wintypes.DWORD),
        ("dwProductVersionLS", wintypes.DWORD),
        ("dwFileFlagsMask", wintypes.DWORD),
        ("dwFileFlags", wintypes.DWORD),
        ("dwFileOS", wintypes.DWORD),
        ("dwFileType", wintypes.DWORD),
        ("dwFileSubtype", wintypes.DWORD),
        ("dwFileDateMS", wintypes.DWORD),
        ("dwFileDateLS", wintypes.DWORD),
    ]


def _windows_file_version(executable: str | None) -> str | None:
    if platform.system() != "Windows" or not executable:
        return None
    version = ctypes.WinDLL("version", use_last_error=True)
    size = version.GetFileVersionInfoSizeW(str(executable), None)
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(executable), 0, size, buffer):
        return None
    pointer = ctypes.c_void_p()
    length = wintypes.UINT()
    if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        return None
    fixed = ctypes.cast(pointer, ctypes.POINTER(_VS_FIXEDFILEINFO)).contents
    parts = (
        fixed.dwFileVersionMS >> 16,
        fixed.dwFileVersionMS & 0xFFFF,
        fixed.dwFileVersionLS >> 16,
        fixed.dwFileVersionLS & 0xFFFF,
    )
    return ".".join(str(part) for part in parts)


def _is_descendant(pid: int, parent_map: dict[int, int], ancestors: set[int]) -> bool:
    seen: set[int] = set()
    current = pid
    while current and current not in seen:
        if current in ancestors:
            return True
        seen.add(current)
        current = parent_map.get(current, 0)
    return False


def _kind(record: dict[str, Any], window_count: int) -> str | None:
    name = str(record.get("name") or "").casefold()
    command = " ".join(str(part) for part in record.get("command_line") or []).casefold()
    if "mphserver" in name or (
        name in {"java", "java.exe"} and "comsol" in command and "server" in command
    ):
        return "comsol_server"
    if any(pattern in command for pattern in ("mph.client", "import mph", "from mph", "-m mph")):
        return "mph_client"
    if window_count > 0 and ("comsol" in name or "comsol" in command):
        return "comsol_desktop"
    if "comsol" in name or "comsol" in command:
        return "other_comsol"
    return None


def collect_shared_preflight_snapshot(
    *,
    process_provider: Callable[[], list[dict[str, Any]]] = _process_records,
    listener_provider: Callable[[], list[dict[str, Any]]] = _listener_records,
    window_provider: Callable[[], dict[int, dict[str, Any]]] = _window_state_by_pid,
    version_provider: Callable[[str | None], str | None] = _windows_file_version,
    clock: Callable[[], float] = time.time,
    exclude_pids: Iterable[int] = (),
) -> dict[str, Any]:
    """Collect one bounded redacted snapshot without importing or starting MPh."""
    records = process_provider()
    listeners = listener_provider()
    windows = window_provider()
    excluded = {int(pid) for pid in exclude_pids}
    parent_map = {
        int(record["pid"]): int(record.get("parent_pid") or 0)
        for record in records
        if record.get("pid") is not None
    }
    preliminary: list[tuple[dict[str, Any], str]] = []
    allowed_roots: set[int] = set()
    for record in records:
        pid = int(record["pid"])
        state = windows.get(pid, {"window_count": 0, "responding": True})
        kind = _kind(record, int(state["window_count"]))
        if kind is None or pid in excluded:
            continue
        preliminary.append((record, kind))
        if kind in {"comsol_desktop", "comsol_server"}:
            allowed_roots.add(pid)

    candidates: list[dict[str, Any]] = []
    for record, kind in preliminary:
        pid = int(record["pid"])
        if kind == "other_comsol" and _is_descendant(pid, parent_map, allowed_roots):
            continue
        state = windows.get(pid, {"window_count": 0, "responding": True})
        version = None if kind == "mph_client" else version_provider(record.get("executable"))
        candidates.append({
            "pid": pid,
            "parent_pid": int(record.get("parent_pid") or 0),
            "kind": kind,
            "create_time": float(record["create_time"]),
            "command_signature": _command_signature(record.get("command_line") or []),
            "file_version": version or "unreadable",
            "window_count": int(state["window_count"]),
            "responding": bool(state["responding"]),
        })
    return {
        "inventory_complete": True,
        "observed_at_epoch": float(clock()),
        "processes": sorted(candidates, key=lambda item: item["pid"]),
        "listeners": sorted(
            listeners, key=lambda item: (str(item["host"]), int(item["port"]), int(item["pid"]))
        ),
    }


__all__ = ["collect_shared_preflight_snapshot"]
