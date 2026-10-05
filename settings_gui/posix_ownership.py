"""Advisory POSIX settings ownership with baseline conflict detection."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from comsol_mcp.posix_lock import LockBusy, PosixFileLock

from .constants import MAX_SETTINGS_BYTES
from .windows_lock import FileIdentity, SettingsConflict, file_identity, path_has_linked_component


class PosixSettingsOwnership:
    """Detect external replacement without claiming Windows handle protection."""

    def __init__(self, target: Path) -> None:
        self.target = Path(os.path.abspath(target))
        self.sidecar = self.target.with_name(f".{self.target.name}.gui-owner")
        self.lock = PosixFileLock(self.sidecar)
        self.baseline: FileIdentity | None = None

    @property
    def target_handle_held(self) -> bool:
        return False

    def acquire(self) -> PosixSettingsOwnership:
        if path_has_linked_component(self.target):
            raise SettingsConflict("settings target path must not contain symlinks")
        try:
            self.lock.acquire()
            try:
                self.baseline = file_identity(self.target)
            except SettingsConflict:
                value = self.target.lstat()
                if not self.target.is_file() or value.st_size <= MAX_SETTINGS_BYTES:
                    raise
                self.baseline = FileIdentity(
                    value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, "unbounded"
                )
            return self
        except LockBusy as exc:
            self.close()
            raise SettingsConflict("another settings editor holds the lock") from exc
        except (RuntimeError, OSError) as exc:
            self.close()
            raise SettingsConflict("settings sidecar ownership could not be acquired") from exc

    def verify_unchanged(self) -> None:
        if file_identity(self.target) != self.baseline:
            raise SettingsConflict("settings target changed outside this editor")

    def accept_current_identity(self) -> None:
        self.baseline = file_identity(self.target)

    def reacquire_target_handle(self) -> None:
        if path_has_linked_component(self.target):
            raise SettingsConflict("settings target path must not contain symlinks")

    def release_target_handle(self) -> None:
        pass

    def close(self) -> None:
        self.lock.close()

    def __enter__(self) -> PosixSettingsOwnership:
        return self.acquire()

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()
