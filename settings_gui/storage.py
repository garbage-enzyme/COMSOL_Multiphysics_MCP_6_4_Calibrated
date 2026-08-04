"""Strict loading, recovery, and atomic settings persistence."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from comsol_mcp.settings import (
    MAX_SETTINGS_BYTES,
    SettingsError,
    default_settings_document,
    normalize_settings_document,
    serialize_settings_document,
)

from .constants import SAVE_RETRY_INTERVAL_SECONDS, SAVE_RETRY_SECONDS
from .windows_lock import (
    SettingsConflict,
    SettingsOwnership,
    file_identity,
    path_has_linked_component,
)


class DamagedSettings(SettingsError):
    """Raised when existing settings bytes cannot be represented safely."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DamagedSettings(
                "settings contain a duplicate JSON key",
                reason_code="settings_json_invalid",
            )
        result[key] = value
    return result


def decode_settings_bytes(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_SETTINGS_BYTES:
        raise DamagedSettings(
            f"settings must contain 1..{MAX_SETTINGS_BYTES} bytes",
            reason_code="settings_size_invalid",
        )
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except UnicodeDecodeError as exc:
        raise DamagedSettings(
            "settings must be UTF-8",
            reason_code="settings_encoding_invalid",
        ) from exc
    except (json.JSONDecodeError, RecursionError) as exc:
        raise DamagedSettings(
            "settings contain invalid JSON",
            reason_code="settings_json_invalid",
        ) from exc
    if not isinstance(value, dict):
        raise DamagedSettings(
            "settings must contain a JSON object",
            reason_code="settings_json_invalid",
        )
    return value


def load_raw_document(path: Path) -> dict[str, Any]:
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise DamagedSettings("settings target must not be a link or junction")
    if not path.is_file():
        raise FileNotFoundError(path.name)
    return decode_settings_bytes(path.read_bytes())


def _sharing_error(error: OSError) -> bool:
    return getattr(error, "winerror", None) in {5, 32, 33}


def _write_all(descriptor: int, raw: bytes) -> None:
    remaining = memoryview(raw)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("settings write made no progress")
        remaining = remaining[written:]


class SettingsStore:
    """Own one settings target and publish only exact verified bytes."""

    def __init__(
        self,
        target: Path,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.target = Path(os.path.abspath(target))
        self.ownership = SettingsOwnership(self.target)
        self._clock = clock
        self._sleeper = sleeper

    def open(self) -> "SettingsStore":
        self.ownership.acquire()
        return self

    def load(self) -> dict[str, Any]:
        return load_raw_document(self.target)

    def validate(self, document: Mapping[str, Any]) -> dict[str, Any]:
        report = normalize_settings_document(document)
        if report["errors"]:
            raise SettingsError(
                "settings contain invalid values",
                reason_code="settings_value_invalid",
            )
        return report["settings"]

    def save(self, document: Mapping[str, Any]) -> str:
        raw = serialize_settings_document(document)
        self.ownership.verify_unchanged()
        temporary = self.target.with_name(f".{self.target.name}.{os.getpid()}.{time.time_ns()}.tmp")
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                0o600,
            )
            _write_all(descriptor, raw)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            if temporary.read_bytes() != raw:
                raise OSError("temporary settings verification failed")
            temporary_identity = file_identity(temporary)
            if temporary_identity is None:
                raise OSError("temporary settings identity is unavailable")

            deadline = self._clock() + SAVE_RETRY_SECONDS
            while True:
                self.ownership.reacquire_target_handle()
                self.ownership.verify_unchanged()
                self.ownership.release_target_handle()
                try:
                    os.replace(temporary, self.target)
                    break
                except OSError as exc:
                    if not _sharing_error(exc) or self._clock() >= deadline:
                        raise
                    self._sleeper(SAVE_RETRY_INTERVAL_SECONDS)
            saved_identity = file_identity(self.target)
            if (
                saved_identity is None
                or saved_identity.device != temporary_identity.device
                or saved_identity.inode != temporary_identity.inode
                or saved_identity.size != temporary_identity.size
                or saved_identity.sha256 != temporary_identity.sha256
                or self.target.read_bytes() != raw
            ):
                raise SettingsConflict("saved settings bytes do not match the request")
            self.ownership.baseline = saved_identity
            self.ownership.reacquire_target_handle()
            return hashlib.sha256(raw).hexdigest()
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if not self.ownership.target_handle_held and self.target.exists():
                self.ownership.reacquire_target_handle()
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def preserve_damaged_copy(self) -> Path:
        raw = self.target.read_bytes()
        if not raw or len(raw) > MAX_SETTINGS_BYTES:
            raise DamagedSettings("damaged settings cannot be preserved within the bound")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        digest = hashlib.sha256(raw).hexdigest()
        backup = self.target.with_name(f"{self.target.stem}.damaged-{stamp}-{digest[:12]}.json")
        descriptor = os.open(
            backup,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            _write_all(descriptor, raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if backup.read_bytes() != raw:
            raise OSError("damaged settings backup verification failed")
        return backup

    def rebuild(self) -> str:
        if self.target.exists():
            self.preserve_damaged_copy()
        ensure_default_directories(self.target.parent)
        return self.save(default_settings_document(user_root=self.target.parent))

    def close(self) -> None:
        self.ownership.close()

    def __enter__(self) -> "SettingsStore":
        return self.open()

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


def ensure_settings_parent(target: Path) -> None:
    parent = Path(os.path.abspath(target)).parent
    if path_has_linked_component(parent):
        raise SettingsConflict("settings target parent must not contain a link or junction")
    parent.mkdir(parents=True, exist_ok=True)
    if path_has_linked_component(parent):
        raise SettingsConflict("settings target parent must not contain a link or junction")


def ensure_default_directories(
    user_root: Path,
    *,
    program_root: Path | str | None = None,
) -> None:
    root = Path(os.path.abspath(user_root))
    document = default_settings_document(user_root=root, program_root=program_root)
    targets = (
        Path(document["paths"]["model_read_roots"][0]),
        Path(document["runtime"]["directory"]),
        Path(document["paths"]["artifact_write_root"]),
    )
    for target in targets:
        if path_has_linked_component(target):
            raise SettingsConflict("default directory must not contain a link or junction")
        target.mkdir(parents=True, exist_ok=True)
        if not target.is_dir() or path_has_linked_component(target):
            raise SettingsConflict("default directory could not be prepared safely")


__all__ = [
    "DamagedSettings",
    "SettingsStore",
    "decode_settings_bytes",
    "ensure_default_directories",
    "ensure_settings_parent",
    "load_raw_document",
]
