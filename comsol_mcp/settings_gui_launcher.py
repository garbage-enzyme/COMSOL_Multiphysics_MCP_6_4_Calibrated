"""Detached Windows Settings GUI launch without importing Tk in the MCP host."""

from __future__ import annotations

import ctypes
import hashlib
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from types import TracebackType
from typing import Any

from comsol_mcp.settings import resolve_settings_location
from comsol_mcp.utils.runtime_paths import default_runtime_dir
from settings_gui import GUI_RELEASE

from .settings_gui_handshake import (
    HANDSHAKE_ENV,
    handshake_bytes,
    read_handshake,
    validate_handshake_path,
)

HANDSHAKE_TIMEOUT_SECONDS = 5.0
HANDSHAKE_POLL_SECONDS = 0.05


class GuiAlreadyRunning(RuntimeError):
    """Raised when the settings mutex is owned by another process."""


def settings_mutex_name(target: Path) -> str:
    normalized = os.path.normcase(os.path.abspath(target))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"Local\\COMSOLMCPSettings-{digest}"


class SettingsGuiInstanceLock:
    """Own the GUI instance mutex before any first-run modal is displayed."""

    def __init__(self, target: Path) -> None:
        if os.name != "nt":
            raise RuntimeError("Settings GUI is supported only on Windows")
        self.name = settings_mutex_name(target)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._handle: Any = None
        self._acquired = False
        self._configure()

    def _configure(self) -> None:
        from ctypes import wintypes

        self._kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self._kernel32.CreateMutexW.restype = wintypes.HANDLE
        self._kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self._kernel32.ReleaseMutex.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    def acquire(self) -> "SettingsGuiInstanceLock":
        handle = self._kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        self._handle = handle
        wait = self._kernel32.WaitForSingleObject(handle, 0)
        if wait not in (0x00000000, 0x00000080):
            self.close()
            raise GuiAlreadyRunning("settings GUI is already running")
        self._acquired = True
        return self

    def close(self) -> None:
        if self._handle is None:
            return
        if self._acquired:
            self._kernel32.ReleaseMutex(self._handle)
        self._kernel32.CloseHandle(self._handle)
        self._handle = None
        self._acquired = False

    def __enter__(self) -> "SettingsGuiInstanceLock":
        return self.acquire()

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()


def settings_gui_is_running(target: Path) -> bool:
    try:
        lock = SettingsGuiInstanceLock(target).acquire()
    except GuiAlreadyRunning:
        return True
    lock.close()
    return False


def _track_process(process: Any) -> None:
    def wait() -> None:
        try:
            process.wait()
        except Exception:
            return

    threading.Thread(target=wait, name="settings-gui-reaper", daemon=True).start()


def _result(state: str) -> dict[str, Any]:
    success = state in {"launched", "already_running"}
    result: dict[str, Any] = {
        "success": success,
        "state": state,
        "gui_release": GUI_RELEASE,
        "restart_required_after_change": True,
        "message_code": {
            "launched": "settings_gui_opened",
            "already_running": "settings_gui_already_open",
            "gui_runtime_unavailable": "settings_gui_runtime_unavailable",
            "launch_failed": "settings_gui_launch_failed",
            "settings_conflict": "settings_gui_conflict",
        }[state],
        "contains_local_path": False,
    }
    if success:
        result["agent_action_required"] = "pause_for_user"
    return result


def _create_handshake(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        raw = handshake_bytes("pending")
        if os.write(descriptor, raw) != len(raw):
            raise OSError("settings GUI handshake write was incomplete")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def launch_settings_gui(
    *,
    environ: Mapping[str, str] | None = None,
    runtime_dir: Path | None = None,
    executable: Path | None = None,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    token_factory: Callable[[], Any] = uuid.uuid4,
    timeout_seconds: float = HANDSHAKE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if os.name != "nt":
        return _result("gui_runtime_unavailable")
    handshake: Path | None = None
    try:
        target = resolve_settings_location(environ).writable_path
        if settings_gui_is_running(target):
            return _result("already_running")
        root = (runtime_dir or default_runtime_dir(dict(environ or {}))) / "settings_gui"
        if not root.is_absolute() or not str(root).isascii():
            return _result("gui_runtime_unavailable")
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
            return _result("gui_runtime_unavailable")
        token = token_factory().hex
        if not re_full_hex(token):
            return _result("launch_failed")
        handshake = validate_handshake_path(root / f".settings-gui-{token}.json")
        _create_handshake(handshake)
    except AttributeError, OSError, RuntimeError, TypeError, ValueError:
        if handshake is not None:
            try:
                handshake.unlink()
            except FileNotFoundError:
                pass
        return _result("launch_failed")

    environment = dict(os.environ if environ is None else environ)
    environment[HANDSHAKE_ENV] = str(handshake)
    interpreter = (executable or Path(sys.executable)).resolve(strict=False)
    pythonw = interpreter.with_name("pythonw.exe")
    if pythonw.is_file():
        interpreter = pythonw
    command = [str(interpreter), "-m", "settings_gui", "--settings-path", str(target)]
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
        subprocess, "DETACHED_PROCESS", 0
    )
    try:
        process = popen_factory(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            close_fds=True,
            creationflags=flags,
        )
        _track_process(process)
        deadline = clock() + max(0.1, min(timeout_seconds, 10.0))
        child_state = None
        while clock() < deadline:
            payload = read_handshake(handshake)
            if payload is not None and payload["state"] != "pending":
                child_state = payload["state"]
                break
            if process.poll() is not None:
                break
            sleeper(HANDSHAKE_POLL_SECONDS)
        if child_state == "ready":
            return _result("launched")
        if child_state in {
            "already_running",
            "gui_runtime_unavailable",
            "settings_conflict",
        }:
            return _result(child_state)
        return _result("launch_failed")
    except OSError, RuntimeError, ValueError:
        return _result("launch_failed")
    finally:
        if handshake is not None:
            try:
                handshake.unlink()
            except FileNotFoundError:
                pass


def re_full_hex(value: str) -> bool:
    return len(value) == 32 and all(character in "0123456789abcdef" for character in value)


__all__ = [
    "GUI_RELEASE",
    "GuiAlreadyRunning",
    "SettingsGuiInstanceLock",
    "launch_settings_gui",
    "settings_gui_is_running",
    "settings_mutex_name",
]
