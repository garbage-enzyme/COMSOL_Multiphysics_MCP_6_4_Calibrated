"""Dependency-free last-known session status for control-plane discovery."""

from __future__ import annotations

from threading import Lock


_LOCK = Lock()
_STATUS = {"connected": False, "starting": False}


def get_session_status() -> dict[str, bool]:
    with _LOCK:
        return dict(_STATUS)


def set_session_status(*, connected: bool, starting: bool) -> None:
    with _LOCK:
        _STATUS["connected"] = bool(connected)
        _STATUS["starting"] = bool(starting)


__all__ = ["get_session_status", "set_session_status"]
