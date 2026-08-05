"""Build the Python-free Windows COMSOL 6.4 launcher from packaged sources."""

from __future__ import annotations

import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from copy import deepcopy
from importlib.resources import files
from pathlib import Path
from typing import Any

from comsol_mcp.durable.io import atomic_write_json, read_file_bytes_bounded

BUILD_SCHEMA = "comsol_mcp.standalone_build_receipt"
BUILD_SCHEMA_VERSION = "1.0.0"
EXECUTABLE_NAME = "comsol-mcp-standalone.exe"
MANIFEST_NAME = "standalone-build.json"
MAX_SOURCE_BYTES = 512 * 1024
MAX_BUILD_LOG_BYTES = 1024 * 1024
MAX_EXECUTABLE_BYTES = 8 * 1024 * 1024
BUILD_TIMEOUT_SECONDS = 60.0


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path, *, maximum_bytes: int) -> tuple[str, int]:
    payload = read_file_bytes_bounded(path, max_bytes=maximum_bytes)
    return _sha256_bytes(payload), len(payload)


def _resource_bytes(name: str) -> bytes:
    resource = files("comsol_mcp.standalone.assets").joinpath(name)
    with resource.open("rb") as handle:
        payload = handle.read(MAX_SOURCE_BYTES + 1)
    if not payload or len(payload) > MAX_SOURCE_BYTES:
        raise ValueError(f"packaged standalone source is invalid: {name}")
    return payload


def _default_csc_path(environ: dict[str, str] | None = None) -> Path:
    environment = os.environ if environ is None else environ
    windows = Path(environment.get("WINDIR", "C:/Windows"))
    return windows / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"


def _validate_build_host(csc_path: Path) -> None:
    if os.name != "nt" or struct.calcsize("P") != 8:
        raise PlatformError("standalone builds require Windows x64")
    version = sys.getwindowsversion()
    if version.major != 10 or version.build < 10240 or version.product_type != 1:
        raise PlatformError("standalone builds require Windows 10 or 11 workstation")
    if not csc_path.is_file() or csc_path.is_symlink():
        raise FileNotFoundError("Windows x64 .NET Framework compiler is unavailable")


class PlatformError(RuntimeError):
    """Raised when the exact Windows build prerequisites are absent."""


def _prepare_output_directory(output_directory: str | Path) -> Path:
    target = Path(output_directory)
    if not target.is_absolute() or not str(target).isascii():
        raise ValueError("standalone output directory must be absolute and ASCII-only")
    if target.exists():
        if target.is_symlink() or not target.is_dir():
            raise ValueError("standalone output must be a regular directory")
        entries = list(target.iterdir())
        retry_logs = {"build.stdout.log", "build.stderr.log"}
        if any(entry.name not in retry_logs or not entry.is_file() for entry in entries):
            raise FileExistsError("standalone output directory must be empty")
        for entry in entries:
            entry.unlink()
    else:
        target.mkdir(parents=True, exist_ok=False)
    return target.resolve(strict=True)


def _run_compiler_bounded(
    command: list[str], *, cwd: Path, run_command: Any
) -> tuple[Any, bytes, bytes, bool]:
    if run_command is not subprocess.run:
        completed = run_command(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=BUILD_TIMEOUT_SECONDS,
            check=False,
        )
        stdout = bytes(completed.stdout or b"")
        stderr = bytes(completed.stderr or b"")
        return completed, stdout, stderr, len(stdout) + len(stderr) > MAX_BUILD_LOG_BYTES

    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        completed = run_command(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            timeout=BUILD_TIMEOUT_SECONDS,
            check=False,
        )
        stdout_file.seek(0)
        stdout = stdout_file.read(MAX_BUILD_LOG_BYTES + 1)
        remaining = max(0, MAX_BUILD_LOG_BYTES + 1 - len(stdout))
        stderr_file.seek(0)
        stderr = stderr_file.read(remaining)
        stdout_file.seek(0, os.SEEK_END)
        stderr_file.seek(0, os.SEEK_END)
        overflow = stdout_file.tell() + stderr_file.tell() > MAX_BUILD_LOG_BYTES
        return completed, stdout, stderr, overflow


def build_standalone_executable(
    output_directory: str | Path,
    *,
    csc_path: str | Path | None = None,
    run_command: Any = subprocess.run,
) -> dict[str, Any]:
    """Build one reviewed launcher without installing or importing build dependencies."""
    target = _prepare_output_directory(output_directory)
    compiler = Path(csc_path) if csc_path is not None else _default_csc_path()
    _validate_build_host(compiler)

    launcher_source = _resource_bytes("Launcher.cs")
    driver_source = _resource_bytes("CapacitorPointTemplate.java")
    source_root = target / "build-sources"
    source_root.mkdir()
    launcher_path = source_root / "Launcher.cs"
    driver_path = source_root / "CapacitorPointTemplate.java"
    launcher_path.write_bytes(launcher_source)
    driver_path.write_bytes(driver_source)

    executable = target / EXECUTABLE_NAME
    command = [
        str(compiler),
        "/nologo",
        "/target:exe",
        "/platform:x64",
        "/optimize+",
        f"/out:{executable}",
        "/reference:System.Web.Extensions.dll",
        f"/resource:{driver_path},CapacitorPointTemplate.java",
        str(launcher_path),
    ]
    try:
        completed, stdout, stderr, overflow = _run_compiler_bounded(
            command, cwd=source_root, run_command=run_command
        )
        (target / "build.stdout.log").write_bytes(stdout[:MAX_BUILD_LOG_BYTES])
        (target / "build.stderr.log").write_bytes(
            stderr[: max(0, MAX_BUILD_LOG_BYTES - min(len(stdout), MAX_BUILD_LOG_BYTES))]
        )
        if overflow:
            raise RuntimeError("standalone compiler output exceeded its bound")
        if completed.returncode != 0 or not executable.is_file():
            raise RuntimeError("standalone launcher compilation failed")
    except BaseException:
        shutil.rmtree(source_root, ignore_errors=True)
        executable.unlink(missing_ok=True)
        (target / MANIFEST_NAME).unlink(missing_ok=True)
        raise

    executable_hash, executable_bytes = _sha256_file(executable, maximum_bytes=MAX_EXECUTABLE_BYTES)
    compiler_hash, compiler_bytes = _sha256_file(compiler, maximum_bytes=16 * 1024 * 1024)
    receipt: dict[str, Any] = {
        "schema_name": BUILD_SCHEMA,
        "schema_version": BUILD_SCHEMA_VERSION,
        "status": "passed",
        "target_os": ["Windows 10 x64", "Windows 11 x64"],
        "target_comsol": "6.4 release line",
        "python_required_at_runtime": False,
        "external_java_required_at_runtime": False,
        "windows_inbox_dotnet_framework_required": True,
        "separate_dotnet_runtime_required": False,
        "separate_dotnet_sdk_required": False,
        "visual_studio_required": False,
        "network_download_required": False,
        "local_comsol_installation_required": True,
        "comsol_runtime_bundled": False,
        "runtime_architecture": [
            "licensed COMSOL 6.4 installation",
            "COMSOL-compiled Java point driver",
            "native Windows x64 launcher",
        ],
        "launcher": {
            "name": EXECUTABLE_NAME,
            "sha256": executable_hash,
            "byte_count": executable_bytes,
        },
        "sources": {
            "Launcher.cs": {
                "sha256": _sha256_bytes(launcher_source),
                "byte_count": len(launcher_source),
            },
            "CapacitorPointTemplate.java": {
                "sha256": _sha256_bytes(driver_source),
                "byte_count": len(driver_source),
            },
        },
        "compiler": {
            "sha256": compiler_hash,
            "byte_count": compiler_bytes,
            "source": "Windows inbox .NET Framework 4.x x64",
            "locator": "%WINDIR%/Microsoft.NET/Framework64/v4.0.30319/csc.exe",
        },
        "command_argument_count": len(command),
    }
    atomic_write_json(target / MANIFEST_NAME, receipt)
    return deepcopy(receipt)


__all__ = [
    "BUILD_SCHEMA",
    "BUILD_SCHEMA_VERSION",
    "EXECUTABLE_NAME",
    "MANIFEST_NAME",
    "PlatformError",
    "build_standalone_executable",
]
