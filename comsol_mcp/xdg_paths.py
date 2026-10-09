"""Absolute user-scoped Linux paths without filesystem side effects."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

_ROOTS = {
    "config": ("XDG_CONFIG_HOME", ".config"),
    "state": ("XDG_STATE_HOME", ".local/state"),
    "data": ("XDG_DATA_HOME", ".local/share"),
    "cache": ("XDG_CACHE_HOME", ".cache"),
}


def project_path(kind: str, environment: Mapping[str, str]) -> Path:
    """Resolve a validated XDG root while retaining case-sensitive path identity."""
    variable, fallback = _ROOTS[kind]
    home = Path(environment.get("HOME") or str(Path.home()))
    root = Path(environment.get(variable) or home / fallback).expanduser()
    if not root.is_absolute():
        raise ValueError(f"{variable} must be an absolute path")
    return root / "comsol-mcp"
