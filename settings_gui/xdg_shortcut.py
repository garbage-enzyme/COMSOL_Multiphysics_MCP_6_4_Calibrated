"""Owned XDG launchers without a shell or foreign-file overwrite."""

from __future__ import annotations

import hashlib
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from comsol_mcp.durable.io import atomic_write_bytes_exclusive, unlink_if_content
from comsol_mcp.posix_lock import PosixFileLock
from comsol_mcp.xdg_paths import project_path

from .desktop_shortcut import _receipt as _shared_receipt
from .desktop_shortcut import _validated_settings_path, encode_settings_path_token
from .windows_lock import path_has_linked_component

NAME = "comsol-mcp-settings.desktop"
OWNER = "X-COMSOL-MCP-Owner=owned-launcher-v1\n"
DIGEST = "X-COMSOL-MCP-Content-SHA256="


def _receipt(state: str, *, success: bool, settings_path: Path, **details: Any) -> dict[str, Any]:
    result = dict(_shared_receipt(state, success=success, settings_path=settings_path))
    result["shortcut_name"] = NAME
    result.update(details)
    return result


def _quote_executable(value: str) -> str:
    if not value.isascii() or any(ord(c) < 32 for c in value) or "=" in value or "%" in value:
        raise ValueError("launcher executable must be bounded ASCII without field codes")
    quoted = "".join("\\" + c if c in '\\"`$' else c for c in value)
    return '"' + quoted.replace("\\", "\\\\") + '"'


def _inputs(
    settings_path: Path, desktop_path: Path | None, executable: Path | None
) -> tuple[Path, Path, bytes]:
    settings = _validated_settings_path(settings_path)
    folder = desktop_path or project_path("data", os.environ).parent / "applications"
    if not folder.is_absolute() or path_has_linked_component(folder):
        raise ValueError("launcher directory must be an absolute non-link path")
    entry = executable or Path(sys.executable).parent / "comsol-mcp-settings"
    if not entry.is_absolute() or not entry.is_file() or not os.access(entry, os.X_OK):
        raise ValueError("installed GUI entry is unavailable")
    command = (
        _quote_executable(str(entry))
        + " --settings-path-token "
        + encode_settings_path_token(settings)
    )
    body = (
        "[Desktop Entry]\nType=Application\nName=COMSOL MCP Settings\n"
        f"Exec={command}\nTerminal=false\nCategories=Science;Settings;\n"
        f"Icon={Path(__file__).resolve().parent / 'assets/comsol_mcp.png'}\n" + OWNER
    ).encode("utf-8")
    payload = body + (DIGEST + hashlib.sha256(body).hexdigest() + "\n").encode("ascii")
    return settings, folder / NAME, payload


def _existing(path: Path) -> bytes | None:
    if not os.path.lexists(path):
        return None
    if path_has_linked_component(path) or not path.is_file():
        raise ValueError("launcher must be a non-link regular file")
    before = path.stat()
    if before.st_size > 65536:
        raise ValueError("launcher exceeds its bound")
    raw = path.read_bytes()
    after = path.stat()
    if (before.st_ino, before.st_dev, before.st_mtime_ns, before.st_size) != (
        after.st_ino,
        after.st_dev,
        after.st_mtime_ns,
        after.st_size,
    ):
        raise ValueError("launcher changed during read")
    return raw


def _owned(raw: bytes) -> bool:
    body, separator, digest = raw.rpartition(DIGEST.encode("ascii"))
    return bool(
        separator
        and OWNER.encode("ascii") in body
        and digest == (hashlib.sha256(body).hexdigest() + "\n").encode("ascii")
    )


def shortcut_status(
    *,
    settings_path: Path,
    desktop_path: Path | None = None,
    executable: Path | None = None,
    **_kwargs: Any,
) -> dict[str, Any]:
    settings, path, desired = _inputs(settings_path, desktop_path, executable)
    raw = _existing(path)
    state = (
        "not_found"
        if raw is None
        else "current"
        if raw == desired
        else "stale"
        if _owned(raw)
        else "foreign"
    )
    return _receipt(state, success=True, settings_path=settings)


def create_desktop_shortcut(
    *,
    settings_path: Path,
    replace_existing: bool = False,
    desktop_path: Path | None = None,
    executable: Path | None = None,
    **_kwargs: Any,
) -> dict[str, Any]:
    settings, path, desired = _inputs(settings_path, desktop_path, executable)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = PosixFileLock(path.with_name("." + path.name + ".lock"))
    lock.acquire()
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        old = _existing(path)
        if old == desired:
            return _receipt("already_current", success=True, settings_path=settings)
        if old is not None and (not replace_existing or not _owned(old)):
            return _receipt(
                "conflict",
                success=False,
                settings_path=settings,
                existing_kind="foreign" if not _owned(old) else "owned_stale",
            )
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(desired)
            stream.flush()
            os.fsync(stream.fileno())
        if _existing(path) != old:
            return _receipt("conflict", success=False, settings_path=settings)
        if old is not None and not unlink_if_content(path, old):
            return _receipt("conflict", success=False, settings_path=settings)
        # Publish without overwrite even after removal of an owned old entry.
        # A noncooperating writer can create a new file between these actions.
        try:
            os.link(temporary, path)
        except FileExistsError:
            return _receipt("conflict", success=False, settings_path=settings)
        except OSError:
            restored = False
            if old is not None:
                try:
                    atomic_write_bytes_exclusive(path, old)
                    restored = True
                except FileExistsError:
                    pass  # Preserve a new foreign entry without overwrite.
                except OSError as exc:
                    raise RuntimeError("owned launcher restoration is uncertain") from exc
            return _receipt(
                "conflict",
                success=False,
                settings_path=settings,
                reason_code="launcher_publication_failed",
                owned_launcher_restored=restored,
            )
        directory = os.open(path.parent, os.O_RDONLY | int(getattr(os, "O_DIRECTORY")))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return _receipt(
            "created" if old is None else "replaced", success=True, settings_path=settings
        )
    finally:
        temporary.unlink(missing_ok=True)
        lock.close()


def remove_desktop_shortcut(
    *,
    settings_path: Path,
    desktop_path: Path | None = None,
    executable: Path | None = None,
    **_kwargs: Any,
) -> dict[str, Any]:
    settings, path, _desired = _inputs(settings_path, desktop_path, executable)
    if not path.parent.exists():
        return _receipt("not_found", success=True, settings_path=settings)
    lock = PosixFileLock(path.with_name("." + path.name + ".lock"))
    lock.acquire()
    try:
        raw = _existing(path)
        if raw is None:
            return _receipt("not_found", success=True, settings_path=settings)
        if not _owned(raw) or _existing(path) != raw:
            return _receipt(
                "conflict", success=False, settings_path=settings, existing_kind="foreign"
            )
        if not unlink_if_content(path, raw):
            return _receipt("conflict", success=False, settings_path=settings)
        return _receipt("removed", success=True, settings_path=settings)
    finally:
        lock.close()


def shortcut_prerequisites(*, settings_path: Path) -> dict[str, bool]:
    ready = False
    try:
        _inputs(settings_path, None, None)
        ready = True
    except OSError, RuntimeError, ValueError:
        pass
    return {"ready": ready, "xdg_launcher_runtime_available": ready}
