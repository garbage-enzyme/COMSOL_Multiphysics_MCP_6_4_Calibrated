"""Explicit, owned per-user Windows Desktop shortcut lifecycle."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .windows_lock import path_has_linked_component

SHORTCUT_NAME = "COMSOL MCP Settings.lnk"
OWNERSHIP_DESCRIPTION = "COMSOL MCP Settings owned shortcut v1"
SHORTCUT_SCHEMA = "comsol_mcp.settings_gui_desktop_shortcut"
SHORTCUT_VERSION = "1.0.0"
MAX_SHORTCUT_BYTES = 1024 * 1024
MAX_SETTINGS_PATH_CHARS = 32767
ICON_PATH = Path(__file__).resolve().parent / "assets" / "comsol_mcp.ico"


@dataclass(frozen=True)
class ShortcutSpec:
    """Exact non-secret Windows Shell Link properties owned by this package."""

    target: Path
    arguments: str
    working_directory: Path
    icon_location: str
    description: str


class _Guid(ctypes.Structure):
    _fields_ = (
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    )


_FOLDERID_DESKTOP = _Guid(
    0xB4BFCC3A,
    0xDB2C,
    0x424C,
    (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41),
)


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _path_identity(path: Path) -> str:
    return hashlib.sha256(_normalized_path(path).encode("utf-8")).hexdigest()


def _validated_settings_path(path: Path) -> Path:
    target = Path(path).expanduser()
    if not target.is_absolute():
        raise ValueError("settings path must be absolute")
    if len(str(target)) > MAX_SETTINGS_PATH_CHARS or any(
        ord(character) < 32 for character in str(target)
    ):
        raise ValueError("settings path must be a bounded Windows path")
    target = Path(os.path.abspath(target))
    if not target.parent.is_dir():
        raise ValueError("settings parent must already exist")
    if path_has_linked_component(target.parent):
        raise ValueError("settings path must not contain a link or junction")
    if target.exists() and (not target.is_file() or target.is_symlink()):
        raise ValueError("settings path must identify a regular file")
    return target


def known_desktop_path() -> Path:
    """Resolve the per-user Desktop through the Windows known-folder API."""
    if os.name != "nt":
        raise OSError("desktop shortcuts are supported only on Windows")
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(_Guid),
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    pointer = ctypes.c_wchar_p()
    result = shell32.SHGetKnownFolderPath(
        ctypes.byref(_FOLDERID_DESKTOP),
        0,
        None,
        ctypes.byref(pointer),
    )
    if result != 0 or not pointer.value:
        raise OSError(result, "SHGetKnownFolderPath failed")
    try:
        desktop = Path(pointer.value)
    finally:
        ole32.CoTaskMemFree(ctypes.cast(pointer, ctypes.c_void_p))
    if not desktop.is_absolute() or not desktop.is_dir() or path_has_linked_component(desktop):
        raise OSError("Desktop known folder is unavailable")
    return desktop


def installed_entry_executable() -> Path:
    """Find the exact console entry generated for this Python installation."""
    candidates: list[Path] = []
    argv0 = Path(sys.argv[0])
    if argv0.name.casefold() == "comsol-mcp-settings.exe":
        candidates.append(argv0)
    executable = Path(sys.executable)
    candidates.extend(
        (
            executable.parent / "comsol-mcp-settings.exe",
            executable.parent / "Scripts" / "comsol-mcp-settings.exe",
            Path(sys.prefix) / "Scripts" / "comsol-mcp-settings.exe",
        )
    )
    discovered = shutil.which("comsol-mcp-settings.exe")
    if discovered:
        candidates.append(Path(discovered))
    seen: set[str] = set()
    for candidate in candidates:
        normalized = _normalized_path(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        if candidate.is_file() and candidate.suffix.casefold() == ".exe":
            return Path(os.path.abspath(candidate))
    raise FileNotFoundError("installed comsol-mcp-settings.exe was not found")


def _powershell_executable() -> Path:
    command = shutil.which("powershell.exe")
    if not command:
        raise FileNotFoundError("Windows PowerShell is unavailable")
    return Path(command)


def _encoded_command(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _canonical_icon_location(value: str) -> str:
    """Normalize the Shell Link's optional space before its icon index."""
    icon_path, separator, raw_index = value.rpartition(",")
    if not separator:
        return value
    try:
        index = int(raw_index.strip())
    except ValueError:
        return value
    return f"{icon_path.rstrip()},{index}"


def _run_powershell(script: str, environment: dict[str, str]) -> str:
    completed = subprocess.run(  # noqa: S603
        [
            str(_powershell_executable()),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            _encoded_command(script),
        ],
        env={**os.environ, **environment},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        raise OSError("Windows shortcut operation failed")
    return completed.stdout.decode("ascii", errors="strict").strip()


_INSPECT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
try {
    $shortcut = $shell.CreateShortcut($env:COMSOL_MCP_SHORTCUT_PATH)
    try {
        $value = [ordered]@{
            target = $shortcut.TargetPath
            arguments = $shortcut.Arguments
            working_directory = $shortcut.WorkingDirectory
            icon_location = $shortcut.IconLocation
            description = $shortcut.Description
        }
        $json = $value | ConvertTo-Json -Compress
        [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
    } finally {
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
    }
} finally {
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
}
"""

_WRITE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
try {
    $shortcut = $shell.CreateShortcut($env:COMSOL_MCP_SHORTCUT_PATH)
    try {
        $shortcut.TargetPath = $env:COMSOL_MCP_SHORTCUT_TARGET
        $shortcut.Arguments = $env:COMSOL_MCP_SHORTCUT_ARGUMENTS
        $shortcut.WorkingDirectory = $env:COMSOL_MCP_SHORTCUT_WORKING_DIRECTORY
        $shortcut.IconLocation = $env:COMSOL_MCP_SHORTCUT_ICON
        $shortcut.Description = $env:COMSOL_MCP_SHORTCUT_DESCRIPTION
        $shortcut.Save()
    } finally {
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
    }
} finally {
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
}
"""


def inspect_windows_shortcut(path: Path) -> ShortcutSpec:
    if not path.is_file() or path.is_symlink():
        raise ValueError("shortcut is not a regular file")
    encoded = _run_powershell(_INSPECT_SCRIPT, {"COMSOL_MCP_SHORTCUT_PATH": str(path)})
    try:
        value = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
        return ShortcutSpec(
            target=Path(value["target"]),
            arguments=str(value["arguments"]),
            working_directory=Path(value["working_directory"]),
            icon_location=_canonical_icon_location(str(value["icon_location"])),
            description=str(value["description"]),
        )
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("shortcut properties are invalid") from exc


def _write_windows_shortcut(path: Path, spec: ShortcutSpec) -> None:
    temporary = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.lnk")
    try:
        _run_powershell(
            _WRITE_SCRIPT,
            {
                "COMSOL_MCP_SHORTCUT_PATH": str(temporary),
                "COMSOL_MCP_SHORTCUT_TARGET": str(spec.target),
                "COMSOL_MCP_SHORTCUT_ARGUMENTS": spec.arguments,
                "COMSOL_MCP_SHORTCUT_WORKING_DIRECTORY": str(spec.working_directory),
                "COMSOL_MCP_SHORTCUT_ICON": spec.icon_location,
                "COMSOL_MCP_SHORTCUT_DESCRIPTION": spec.description,
            },
        )
        if inspect_windows_shortcut(temporary) != spec:
            raise OSError("written shortcut properties differ")
        os.rename(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _desired_spec(settings_path: Path, executable: Path, icon_path: Path) -> ShortcutSpec:
    target = _validated_settings_path(settings_path)
    executable = Path(os.path.abspath(executable))
    icon_path = Path(os.path.abspath(icon_path))
    if not executable.is_file() or executable.suffix.casefold() != ".exe":
        raise ValueError("installed Settings GUI executable is unavailable")
    if not icon_path.is_file():
        raise ValueError("packaged Settings GUI icon is unavailable")
    return ShortcutSpec(
        target=executable,
        arguments=subprocess.list2cmdline(["--settings-path", str(target)]),
        working_directory=executable.parent,
        icon_location=f"{icon_path},0",
        description=OWNERSHIP_DESCRIPTION,
    )


def _receipt(
    state: str,
    *,
    success: bool,
    settings_path: Path,
    existing_kind: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_name": SHORTCUT_SCHEMA,
        "schema_version": SHORTCUT_VERSION,
        "success": success,
        "state": state,
        "shortcut_name": SHORTCUT_NAME,
        "settings_identity_sha256": _path_identity(settings_path),
        "settings_path_included": False,
        "contains_local_path": False,
        "mcp_started": False,
        "solver_started": False,
    }
    if existing_kind is not None:
        result["existing_kind"] = existing_kind
    return result


def _resolved_inputs(
    settings_path: Path,
    desktop_path: Path | None,
    executable: Path | None,
    icon_path: Path | None,
) -> tuple[Path, Path, ShortcutSpec]:
    settings = _validated_settings_path(settings_path)
    desktop = Path(desktop_path) if desktop_path is not None else known_desktop_path()
    if not desktop.is_absolute() or not desktop.is_dir() or path_has_linked_component(desktop):
        raise ValueError("Desktop folder is unavailable")
    entry = executable if executable is not None else installed_entry_executable()
    icon = icon_path if icon_path is not None else ICON_PATH
    return settings, desktop / SHORTCUT_NAME, _desired_spec(settings, entry, icon)


def _existing_kind(existing: ShortcutSpec | None, desired: ShortcutSpec) -> str:
    if existing is None or existing.description != OWNERSHIP_DESCRIPTION:
        return "foreign"
    return "owned_current" if existing == desired else "owned_stale"


def _shortcut_identity(path: Path) -> tuple[int, int, int, str]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_SHORTCUT_BYTES:
        raise ValueError("shortcut is not a bounded regular file")
    stat = path.stat()
    return (
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_size),
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def shortcut_status(
    *,
    settings_path: Path,
    desktop_path: Path | None = None,
    executable: Path | None = None,
    icon_path: Path | None = None,
    inspect_shortcut: Callable[[Path], ShortcutSpec] = inspect_windows_shortcut,
    write_shortcut: Callable[[Path, ShortcutSpec], None] = _write_windows_shortcut,
) -> dict[str, Any]:
    del write_shortcut
    settings, shortcut, desired = _resolved_inputs(
        settings_path, desktop_path, executable, icon_path
    )
    if not shortcut.exists():
        return _receipt("not_found", success=True, settings_path=settings)
    try:
        existing = inspect_shortcut(shortcut)
    except OSError, RuntimeError, ValueError:
        existing = None
    kind = _existing_kind(existing, desired)
    state = (
        "current" if kind == "owned_current" else "stale" if kind == "owned_stale" else "foreign"
    )
    return _receipt(state, success=True, settings_path=settings, existing_kind=kind)


def create_desktop_shortcut(
    *,
    settings_path: Path,
    replace_existing: bool = False,
    desktop_path: Path | None = None,
    executable: Path | None = None,
    icon_path: Path | None = None,
    inspect_shortcut: Callable[[Path], ShortcutSpec] = inspect_windows_shortcut,
    write_shortcut: Callable[[Path, ShortcutSpec], None] = _write_windows_shortcut,
) -> dict[str, Any]:
    settings, shortcut, desired = _resolved_inputs(
        settings_path, desktop_path, executable, icon_path
    )
    if not shortcut.exists():
        write_shortcut(shortcut, desired)
        return _receipt("created", success=True, settings_path=settings)
    try:
        baseline_identity = _shortcut_identity(shortcut)
    except OSError, RuntimeError, ValueError:
        baseline_identity = None
    try:
        existing = inspect_shortcut(shortcut)
    except OSError, RuntimeError, ValueError:
        existing = None
    kind = _existing_kind(existing, desired)
    if kind == "owned_current":
        return _receipt("already_current", success=True, settings_path=settings, existing_kind=kind)
    if not replace_existing:
        return _receipt(
            "confirmation_required", success=False, settings_path=settings, existing_kind=kind
        )
    if baseline_identity is None:
        return _receipt("conflict", success=False, settings_path=settings, existing_kind=kind)
    try:
        current_identity = _shortcut_identity(shortcut)
    except OSError, RuntimeError, ValueError:
        current_identity = None
    if current_identity != baseline_identity:
        return _receipt("conflict", success=False, settings_path=settings, existing_kind=kind)
    shortcut.unlink()
    try:
        write_shortcut(shortcut, desired)
    except FileExistsError:
        return _receipt("conflict", success=False, settings_path=settings, existing_kind=kind)
    return _receipt("replaced", success=True, settings_path=settings, existing_kind=kind)


def remove_desktop_shortcut(
    *,
    settings_path: Path,
    desktop_path: Path | None = None,
    executable: Path | None = None,
    icon_path: Path | None = None,
    inspect_shortcut: Callable[[Path], ShortcutSpec] = inspect_windows_shortcut,
    write_shortcut: Callable[[Path, ShortcutSpec], None] = _write_windows_shortcut,
) -> dict[str, Any]:
    del write_shortcut
    settings, shortcut, desired = _resolved_inputs(
        settings_path, desktop_path, executable, icon_path
    )
    if not shortcut.exists():
        return _receipt("not_found", success=True, settings_path=settings)
    try:
        baseline_identity = _shortcut_identity(shortcut)
    except OSError, RuntimeError, ValueError:
        baseline_identity = None
    try:
        existing = inspect_shortcut(shortcut)
    except OSError, RuntimeError, ValueError:
        existing = None
    kind = _existing_kind(existing, desired)
    if kind == "foreign":
        return _receipt(
            "foreign_preserved", success=False, settings_path=settings, existing_kind=kind
        )
    try:
        current_identity = _shortcut_identity(shortcut)
    except OSError, RuntimeError, ValueError:
        current_identity = None
    if baseline_identity is None or current_identity != baseline_identity:
        return _receipt("conflict", success=False, settings_path=settings, existing_kind=kind)
    shortcut.unlink()
    return _receipt("removed", success=True, settings_path=settings, existing_kind=kind)


def shortcut_prerequisites(
    *,
    settings_path: Path,
) -> dict[str, bool]:
    """Check shortcut prerequisites without constructing Tk or writing anything."""
    checks = {
        "desktop_available": False,
        "entry_executable_available": False,
        "icon_available": ICON_PATH.is_file(),
        "windows_shortcut_runtime_available": False,
    }
    try:
        _validated_settings_path(settings_path)
        checks["desktop_available"] = known_desktop_path().is_dir()
        checks["entry_executable_available"] = installed_entry_executable().is_file()
        checks["windows_shortcut_runtime_available"] = _powershell_executable().is_file()
    except OSError, RuntimeError, ValueError:
        pass
    checks["ready"] = all(checks.values())
    return checks


__all__ = [
    "ICON_PATH",
    "OWNERSHIP_DESCRIPTION",
    "SHORTCUT_NAME",
    "ShortcutSpec",
    "create_desktop_shortcut",
    "inspect_windows_shortcut",
    "installed_entry_executable",
    "known_desktop_path",
    "remove_desktop_shortcut",
    "shortcut_prerequisites",
    "shortcut_status",
]
