"""Installed Settings GUI entry point and explicit shortcut commands."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from collections.abc import Callable, MutableMapping, Sequence
from pathlib import Path
from typing import Any

from comsol_mcp import __version__
from comsol_mcp.settings import SETTINGS_PATH_ENV, SettingsError, resolve_settings_location
from comsol_mcp.settings_gui_handshake import publish_handshake

from .desktop_shortcut import (
    create_desktop_shortcut,
    remove_desktop_shortcut,
    shortcut_prerequisites,
    shortcut_status,
)
from .windows_lock import path_has_linked_component

VALIDATION_SCHEMA = "comsol_mcp.settings_gui_direct_entry"
VALIDATION_VERSION = "1.0.0"
MAX_SETTINGS_PATH_CHARS = 32767


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="comsol-mcp-settings")
    parser.add_argument("--settings-path")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--validate-only", action="store_true")
    actions.add_argument("--create-desktop-shortcut", action="store_true")
    actions.add_argument("--remove-desktop-shortcut", action="store_true")
    actions.add_argument("--shortcut-status", action="store_true")
    parser.add_argument("--replace-existing-shortcut", action="store_true")
    return parser


def _settings_target(raw: str | None, environment: MutableMapping[str, str]) -> tuple[Path, bool]:
    if raw is None:
        location = resolve_settings_location(environment)
        return Path(os.path.abspath(location.writable_path)), False
    target = Path(raw).expanduser()
    if not target.is_absolute():
        raise SettingsError("--settings-path must be an absolute path")
    if len(str(target)) > MAX_SETTINGS_PATH_CHARS or any(
        ord(character) < 32 for character in str(target)
    ):
        raise SettingsError("--settings-path is not a bounded Windows path")
    target = Path(os.path.abspath(target))
    if not target.parent.is_dir():
        raise SettingsError("the settings parent directory must already exist")
    if path_has_linked_component(target.parent):
        raise SettingsError("the settings path must not contain a link or junction")
    if target.exists() and (not target.is_file() or target.is_symlink()):
        raise SettingsError("--settings-path must identify a regular file")
    return target, True


def _settings_identity(path: Path) -> str:
    normalized = os.path.normcase(os.path.abspath(path))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _validation_receipt(target: Path, *, override: bool) -> dict[str, Any]:
    prerequisites = shortcut_prerequisites(settings_path=target)
    gui_runtime_available = importlib.util.find_spec("tkinter") is not None
    parent_writable = os.access(target.parent, os.W_OK)
    ready = gui_runtime_available and parent_writable and bool(prerequisites.get("ready"))
    return {
        "schema_name": VALIDATION_SCHEMA,
        "schema_version": VALIDATION_VERSION,
        "package_version": __version__,
        "ready": ready,
        "settings_path_override": override,
        "settings_identity_sha256": _settings_identity(target),
        "settings_path_included": False,
        "contains_local_path": False,
        "settings_parent_writable": parent_writable,
        "gui_runtime_available": gui_runtime_available,
        "shortcut_prerequisites": prerequisites,
        "tkinter_imported": "tkinter" in sys.modules,
        "mcp_started": False,
        "solver_started": False,
    }


def _emit(value: dict[str, Any], output: Callable[[str], None]) -> None:
    output(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _action_failure(target: Path) -> dict[str, Any]:
    return {
        "schema_name": VALIDATION_SCHEMA,
        "schema_version": VALIDATION_VERSION,
        "success": False,
        "state": "action_failed",
        "settings_identity_sha256": _settings_identity(target),
        "settings_path_included": False,
        "contains_local_path": False,
        "mcp_started": False,
        "solver_started": False,
    }


def _launch_gui() -> int:
    try:
        from .app import run
    except ImportError, OSError, RuntimeError:
        publish_handshake("gui_runtime_unavailable")
        return 2
    try:
        return run()
    except Exception:
        publish_handshake("launch_failed")
        raise


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
    output: Callable[[str], None] = print,
) -> int:
    """Run the direct entry without constructing Tk for command-only actions."""
    try:
        arguments = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    environment = os.environ if environ is None else environ
    action_requested = any(
        (
            arguments.create_desktop_shortcut,
            arguments.remove_desktop_shortcut,
            arguments.shortcut_status,
        )
    )
    if arguments.replace_existing_shortcut and not arguments.create_desktop_shortcut:
        return 2
    if action_requested and arguments.settings_path is None:
        return 2
    try:
        target, override = _settings_target(arguments.settings_path, environment)
    except OSError, RuntimeError, SettingsError, ValueError:
        return 2
    if arguments.validate_only:
        receipt = _validation_receipt(target, override=override)
        _emit(receipt, output)
        return 0 if receipt["ready"] else 2
    if arguments.create_desktop_shortcut:
        try:
            receipt = create_desktop_shortcut(
                settings_path=target,
                replace_existing=arguments.replace_existing_shortcut,
            )
        except OSError, RuntimeError, ValueError:
            receipt = _action_failure(target)
        _emit(receipt, output)
        return 0 if receipt.get("success") is True else 3
    if arguments.remove_desktop_shortcut:
        try:
            receipt = remove_desktop_shortcut(settings_path=target)
        except OSError, RuntimeError, ValueError:
            receipt = _action_failure(target)
        _emit(receipt, output)
        return 0 if receipt.get("success") is True else 3
    if arguments.shortcut_status:
        try:
            receipt = shortcut_status(settings_path=target)
        except OSError, RuntimeError, ValueError:
            receipt = _action_failure(target)
        _emit(receipt, output)
        return 0 if receipt.get("success") is True else 3
    if override:
        environment[SETTINGS_PATH_ENV] = str(target)
    return _launch_gui()


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()


__all__ = ["main", "run_cli"]
