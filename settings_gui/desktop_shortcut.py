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
MAX_SETTINGS_PATH_TOKEN_CHARS = 30000
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


def encode_settings_path_token(path: Path) -> str:
    """Encode one Unicode settings path as bounded ASCII shortcut transport."""
    token = base64.urlsafe_b64encode(str(path).encode("utf-8")).decode("ascii").rstrip("=")
    if not token or len(token) > MAX_SETTINGS_PATH_TOKEN_CHARS:
        raise ValueError("settings path is too long for a Windows shortcut")
    return token


def decode_settings_path_token(token: str) -> str:
    """Decode strict ASCII shortcut transport without touching the filesystem."""
    if (
        not isinstance(token, str)
        or not token
        or len(token) > MAX_SETTINGS_PATH_TOKEN_CHARS
        or not token.isascii()
        or any(not (character.isalnum() or character in "-_") for character in token)
    ):
        raise ValueError("settings path token is invalid")
    padding = "=" * (-len(token) % 4)
    try:
        decoded = base64.b64decode(
            token + padding,
            altchars=b"-_",
            validate=True,
        ).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("settings path token is invalid") from exc
    if not decoded:
        raise ValueError("settings path token is invalid")
    return decoded


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


def _installed_named_entry(name: str) -> Path:
    """Find one exact generated entry for this Python installation."""
    candidates: list[Path] = []
    argv0 = Path(sys.argv[0])
    if argv0.name.casefold() == name.casefold():
        candidates.append(argv0)
    executable = Path(sys.executable)
    candidates.extend(
        (
            executable.parent / name,
            executable.parent / "Scripts" / name,
            Path(sys.prefix) / "Scripts" / name,
        )
    )
    discovered = shutil.which(name)
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
    raise FileNotFoundError(f"installed {name} was not found")


def installed_entry_executable() -> Path:
    """Find the console entry used for explicit command-line actions."""
    return _installed_named_entry("comsol-mcp-settings.exe")


def installed_gui_entry_executable() -> Path:
    """Find the windowed entry used by owned Desktop shortcuts."""
    return _installed_named_entry("comsol-mcp-settings-gui.exe")


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


def _icon_location_identity(value: str) -> tuple[str, str | None]:
    canonical = _canonical_icon_location(value)
    icon_path, separator, raw_index = canonical.rpartition(",")
    if not separator or not icon_path:
        return canonical, None
    return _normalized_path(Path(icon_path)), raw_index


def _windows_argument_tokens(value: str) -> tuple[str, ...]:
    """Parse a Shell Link argument string with the Windows command-line contract."""
    if os.name != "nt":
        return (value,)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    count = ctypes.c_int()
    arguments = shell32.CommandLineToArgvW(
        f"comsol-mcp-settings.exe {value}",
        ctypes.byref(count),
    )
    if not arguments:
        return (value,)
    try:
        return tuple(arguments[index] for index in range(1, count.value))
    finally:
        kernel32.LocalFree(ctypes.cast(arguments, ctypes.c_void_p))


def _arguments_mismatch_field(observed: str, expected: str) -> str | None:
    observed_tokens = _windows_argument_tokens(observed)
    expected_tokens = _windows_argument_tokens(expected)
    if observed_tokens == expected_tokens:
        return None
    if len(observed_tokens) != len(expected_tokens):
        return "arguments_token_count"
    if not observed_tokens or observed_tokens[0] != expected_tokens[0]:
        return "arguments_option"
    if len(observed_tokens) == 2 and expected_tokens[0] == "--settings-path-token":
        try:
            if decode_settings_path_token(observed_tokens[1]) != decode_settings_path_token(
                expected_tokens[1]
            ):
                return "arguments_settings_path"
        except ValueError:
            return "arguments"
    return "arguments"


def _shortcut_spec_mismatch_fields(
    observed: ShortcutSpec,
    expected: ShortcutSpec,
) -> tuple[str, ...]:
    """Return path-free names for Shell Link properties that differ semantically."""
    mismatches: list[str] = []
    if _normalized_path(observed.target) != _normalized_path(expected.target):
        mismatches.append("target")
    arguments_mismatch = _arguments_mismatch_field(observed.arguments, expected.arguments)
    if arguments_mismatch is not None:
        mismatches.append(arguments_mismatch)
    if _normalized_path(observed.working_directory) != _normalized_path(expected.working_directory):
        mismatches.append("working_directory")
    if _icon_location_identity(observed.icon_location) != _icon_location_identity(
        expected.icon_location
    ):
        mismatches.append("icon_location")
    if observed.description != expected.description:
        mismatches.append("description")
    return tuple(mismatches)


def _run_powershell(script: str, environment: dict[str, str]) -> str:
    try:
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
    except subprocess.TimeoutExpired as exc:
        raise OSError("Windows shortcut operation timed out") from exc
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
        mismatches = _shortcut_spec_mismatch_fields(
            inspect_windows_shortcut(temporary),
            spec,
        )
        if mismatches:
            raise OSError("written shortcut properties differ: " + ",".join(mismatches))
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
        arguments=subprocess.list2cmdline(
            ["--settings-path-token", encode_settings_path_token(target)]
        ),
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
    entry = executable if executable is not None else installed_gui_entry_executable()
    icon = icon_path if icon_path is not None else ICON_PATH
    return settings, desktop / SHORTCUT_NAME, _desired_spec(settings, entry, icon)


def _resolved_lifecycle_inputs(
    settings_path: Path,
    desktop_path: Path | None,
) -> tuple[Path, Path]:
    settings = Path(settings_path).expanduser()
    if (
        not settings.is_absolute()
        or len(str(settings)) > MAX_SETTINGS_PATH_CHARS
        or any(ord(character) < 32 for character in str(settings))
    ):
        raise ValueError("settings path must be a bounded absolute path")
    settings = Path(os.path.abspath(settings))
    desktop = Path(desktop_path) if desktop_path is not None else known_desktop_path()
    if not desktop.is_absolute() or not desktop.is_dir() or path_has_linked_component(desktop):
        raise ValueError("Desktop folder is unavailable")
    return settings, desktop / SHORTCUT_NAME


def _existing_kind(existing: ShortcutSpec | None, desired: ShortcutSpec) -> str:
    if existing is None or existing.description != OWNERSHIP_DESCRIPTION:
        return "foreign"
    return (
        "owned_current" if not _shortcut_spec_mismatch_fields(existing, desired) else "owned_stale"
    )


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
    settings, shortcut = _resolved_lifecycle_inputs(settings_path, desktop_path)
    if not shortcut.exists():
        return _receipt("not_found", success=True, settings_path=settings)
    try:
        existing = inspect_shortcut(shortcut)
    except OSError, RuntimeError, ValueError:
        existing = None
    try:
        entry = executable if executable is not None else installed_gui_entry_executable()
        icon = icon_path if icon_path is not None else ICON_PATH
        desired = _desired_spec(settings, entry, icon)
    except OSError, RuntimeError, ValueError:
        desired = None
    kind = (
        "foreign"
        if existing is None or existing.description != OWNERSHIP_DESCRIPTION
        else "owned_stale"
        if desired is None
        else _existing_kind(existing, desired)
    )
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
        candidate = shortcut.with_name(f".{shortcut.stem}.{uuid.uuid4().hex}.tmp{shortcut.suffix}")
        try:
            write_shortcut(candidate, desired)
            try:
                os.rename(candidate, shortcut)
            except FileExistsError:
                return _receipt(
                    "conflict",
                    success=False,
                    settings_path=settings,
                    existing_kind="appeared_during_create",
                )
            return _receipt("created", success=True, settings_path=settings)
        finally:
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
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
    candidate = shortcut.with_name(f".{shortcut.stem}.{uuid.uuid4().hex}.tmp{shortcut.suffix}")
    try:
        write_shortcut(candidate, desired)
        candidate_identity = _shortcut_identity(candidate)
        try:
            current_identity = _shortcut_identity(shortcut)
        except OSError, RuntimeError, ValueError:
            current_identity = None
        if current_identity != baseline_identity:
            return _receipt("conflict", success=False, settings_path=settings, existing_kind=kind)
        os.replace(candidate, shortcut)
        if _shortcut_identity(shortcut) != candidate_identity:
            raise OSError("replacement shortcut identity changed")
    finally:
        candidate.unlink(missing_ok=True)
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
    del executable, icon_path
    settings, shortcut = _resolved_lifecycle_inputs(settings_path, desktop_path)
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
    kind = (
        "foreign"
        if existing is None or existing.description != OWNERSHIP_DESCRIPTION
        else "owned_current"
    )
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
        checks["entry_executable_available"] = installed_gui_entry_executable().is_file()
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
    "decode_settings_path_token",
    "encode_settings_path_token",
    "inspect_windows_shortcut",
    "installed_entry_executable",
    "installed_gui_entry_executable",
    "known_desktop_path",
    "remove_desktop_shortcut",
    "shortcut_prerequisites",
    "shortcut_status",
]
