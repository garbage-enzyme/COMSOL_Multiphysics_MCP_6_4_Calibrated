"""Bounded path-free startup handshake shared with the Settings GUI child."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping

HANDSHAKE_ENV = "COMSOL_MCP_SETTINGS_GUI_HANDSHAKE"
HANDSHAKE_SCHEMA = "comsol_mcp.settings_gui_handshake"
HANDSHAKE_VERSION = "1.0.0"
MAX_HANDSHAKE_BYTES = 2048
_HANDSHAKE_NAME = re.compile(r"^\.settings-gui-[0-9a-f]{32}\.json$")
_STATES = frozenset(
    {
        "pending",
        "ready",
        "already_running",
        "gui_runtime_unavailable",
        "launch_failed",
        "settings_conflict",
    }
)


def handshake_payload(state: str) -> dict[str, Any]:
    if state not in _STATES:
        raise ValueError("settings GUI handshake state is invalid")
    return {
        "schema_name": HANDSHAKE_SCHEMA,
        "schema_version": HANDSHAKE_VERSION,
        "state": state,
    }


def handshake_bytes(state: str) -> bytes:
    return (json.dumps(handshake_payload(state), sort_keys=True) + "\n").encode("ascii")


def validate_handshake_path(value: str | Path) -> Path:
    path = Path(value)
    parent = path.parent
    if (
        not path.is_absolute()
        or not str(path).isascii()
        or not _HANDSHAKE_NAME.fullmatch(path.name)
        or parent.name != "settings_gui"
        or path.is_symlink()
        or getattr(path, "is_junction", lambda: False)()
        or parent.is_symlink()
        or getattr(parent, "is_junction", lambda: False)()
        or not parent.is_dir()
    ):
        raise ValueError("settings GUI handshake path is invalid")
    parent = parent.resolve(strict=True)
    if (
        not parent.is_dir()
        or parent.is_symlink()
        or getattr(parent, "is_junction", lambda: False)()
    ):
        raise ValueError("settings GUI handshake parent is invalid")
    return parent / path.name


def read_handshake(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_HANDSHAKE_BYTES:
            return None
        value = json.loads(path.read_bytes().decode("ascii"))
    except OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError:
        return None
    if not isinstance(value, dict):
        return None
    state = value.get("state")
    if not isinstance(state, str) or state not in _STATES:
        return None
    if value != handshake_payload(state):
        return None
    return value


def publish_handshake(
    state: str,
    environ: Mapping[str, str] | None = None,
) -> bool:
    environment = os.environ if environ is None else environ
    raw_path = environment.get(HANDSHAKE_ENV)
    if not raw_path:
        return False
    temporary: Path | None = None
    try:
        path = validate_handshake_path(raw_path)
        pending = read_handshake(path)
        if pending is None or pending["state"] != "pending":
            return False
        raw = handshake_bytes(state)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            if os.write(descriptor, raw) != len(raw):
                raise OSError("settings GUI handshake write was incomplete")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if read_handshake(path) != handshake_payload("pending"):
            temporary.unlink(missing_ok=True)
            return False
        os.replace(temporary, path)
        return path.read_bytes() == raw
    except OSError, RuntimeError, ValueError:
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


__all__ = [
    "HANDSHAKE_ENV",
    "HANDSHAKE_SCHEMA",
    "HANDSHAKE_VERSION",
    "MAX_HANDSHAKE_BYTES",
    "handshake_bytes",
    "handshake_payload",
    "publish_handshake",
    "read_handshake",
    "validate_handshake_path",
]
