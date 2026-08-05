"""Exact Windows ownership and target-file protection for settings edits."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from comsol_mcp.settings_gui_launcher import settings_mutex_name

from .constants import MAX_SETTINGS_BYTES

_HELD_MUTEXES: set[str] = set()
_HELD_MUTEXES_GUARD = threading.RLock()


class SettingsConflict(RuntimeError):
    """Raised when another editor or writer owns or changed the settings file."""


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int
    sha256: str


def file_identity(path: Path) -> FileIdentity | None:
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise SettingsConflict("settings target must not be a link or junction")
    if not path.exists():
        return None
    if not path.is_file():
        raise SettingsConflict("settings target must be a regular file")
    before = path.lstat()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        stat = os.fstat(descriptor)
        if (stat.st_dev, stat.st_ino) != (before.st_dev, before.st_ino):
            raise SettingsConflict("settings target changed during identity read")
        if stat.st_size > MAX_SETTINGS_BYTES:
            raise SettingsConflict("settings target exceeds the bounded size")
        remaining = MAX_SETTINGS_BYTES + 1
        chunks = bytearray()
        while remaining:
            block = os.read(descriptor, min(65_536, remaining))
            if not block:
                break
            chunks.extend(block)
            remaining -= len(block)
        raw = bytes(chunks)
        if len(raw) != stat.st_size or len(raw) > MAX_SETTINGS_BYTES:
            raise SettingsConflict("settings target changed during identity read")
    finally:
        os.close(descriptor)
    return FileIdentity(
        device=int(stat.st_dev),
        inode=int(stat.st_ino),
        size=int(stat.st_size),
        modified_ns=int(stat.st_mtime_ns),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def path_has_linked_component(path: Path) -> bool:
    return any(
        candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)()
        for candidate in (path, *path.parents)
    )


class SettingsOwnership:
    """Hold the named mutex, sidecar, and delete-denying target handle."""

    def __init__(self, target: Path) -> None:
        if os.name != "nt":
            raise RuntimeError("settings ownership is supported only on Windows")
        self.target = Path(os.path.abspath(target))
        self.sidecar = self.target.with_name(f".{self.target.name}.gui-owner")
        self.mutex_name = settings_mutex_name(self.target)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._mutex: Any = None
        self._sidecar_fd: int | None = None
        self._target_handle: Any = None
        self._mutex_acquired = False
        self._registered = False
        self.baseline: FileIdentity | None = None

    @property
    def target_handle_held(self) -> bool:
        """Report whether the current target is protected from replacement."""
        return self._target_handle is not None

    def acquire(self) -> "SettingsOwnership":
        if path_has_linked_component(self.target):
            raise SettingsConflict("settings target parent must not contain a link or junction")
        if not self.target.parent.is_dir():
            raise SettingsConflict("settings target parent does not exist")
        if os.path.lexists(self.sidecar) and (
            self.sidecar.is_symlink()
            or getattr(self.sidecar, "is_junction", lambda: False)()
        ):
            raise SettingsConflict("settings ownership sidecar must not be a link or junction")
        self._configure_kernel32()
        with _HELD_MUTEXES_GUARD:
            if self.mutex_name in _HELD_MUTEXES:
                raise SettingsConflict("another settings editor is active")
            _HELD_MUTEXES.add(self.mutex_name)
            self._registered = True
        try:
            mutex = self._kernel32.CreateMutexW(None, False, self.mutex_name)
            if not mutex:
                raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
            self._mutex = mutex
            wait = self._kernel32.WaitForSingleObject(mutex, 0)
            if wait not in (0x00000000, 0x00000080):
                raise SettingsConflict("another settings editor is active")
            self._mutex_acquired = True
            self._sidecar_fd = os.open(
                self.sidecar,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0),
                0o600,
            )
            payload = json.dumps({"pid": os.getpid(), "mutex": self.mutex_name}).encode("ascii")
            os.ftruncate(self._sidecar_fd, 0)
            os.write(self._sidecar_fd, payload)
            os.fsync(self._sidecar_fd)
            self.reacquire_target_handle()
            try:
                self.baseline = file_identity(self.target)
            except SettingsConflict:
                stat = self.target.lstat()
                if (
                    self.target.is_symlink()
                    or getattr(self.target, "is_junction", lambda: False)()
                    or not self.target.is_file()
                    or stat.st_size <= MAX_SETTINGS_BYTES
                ):
                    raise
                self.baseline = FileIdentity(
                    device=int(stat.st_dev),
                    inode=int(stat.st_ino),
                    size=int(stat.st_size),
                    modified_ns=int(stat.st_mtime_ns),
                    sha256="unbounded",
                )
            return self
        except Exception as exc:
            try:
                self.close()
            except Exception as cleanup_error:
                exc.add_note(
                    f"settings ownership cleanup failed: {type(cleanup_error).__name__}"
                )
            raise

    def _configure_kernel32(self) -> None:
        from ctypes import wintypes

        self._kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self._kernel32.CreateMutexW.restype = wintypes.HANDLE
        self._kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self._kernel32.ReleaseMutex.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self._kernel32.CreateFileW.restype = wintypes.HANDLE

    def reacquire_target_handle(self) -> None:
        self.release_target_handle()
        if not self.target.exists():
            return
        handle = self._kernel32.CreateFileW(
            str(self.target),
            0x80000000,
            0x00000001,
            None,
            3,
            0x00000080,
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle == invalid:
            raise SettingsConflict("settings target could not be protected")
        self._target_handle = handle

    def release_target_handle(self) -> None:
        if self._target_handle is not None:
            self._kernel32.CloseHandle(self._target_handle)
            self._target_handle = None

    def verify_unchanged(self) -> None:
        if file_identity(self.target) != self.baseline:
            raise SettingsConflict("settings target changed outside this editor")

    def accept_current_identity(self) -> None:
        self.baseline = file_identity(self.target)

    def close(self) -> None:
        errors: list[Exception] = []
        try:
            self.release_target_handle()
        except Exception as exc:
            errors.append(exc)
        if self._sidecar_fd is not None:
            try:
                os.close(self._sidecar_fd)
            except Exception as exc:
                errors.append(exc)
            self._sidecar_fd = None
            try:
                self.sidecar.unlink()
            except FileNotFoundError:
                pass
            except Exception as exc:
                errors.append(exc)
        if self._mutex is not None:
            try:
                if self._mutex_acquired:
                    self._kernel32.ReleaseMutex(self._mutex)
                self._kernel32.CloseHandle(self._mutex)
            except Exception as exc:
                errors.append(exc)
            self._mutex = None
            self._mutex_acquired = False
        if self._registered:
            with _HELD_MUTEXES_GUARD:
                _HELD_MUTEXES.discard(self.mutex_name)
            self._registered = False
        if errors:
            raise errors[0]

    def __enter__(self) -> "SettingsOwnership":
        return self.acquire()

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


__all__ = [
    "FileIdentity",
    "SettingsConflict",
    "SettingsOwnership",
    "file_identity",
    "path_has_linked_component",
]
