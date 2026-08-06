"""Repository-root manual Settings GUI launcher tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "Open_Settings_GUI.ps1"


@pytest.mark.parametrize("shell_name", ["powershell.exe", "pwsh.exe"])
def test_root_launcher_validates_without_starting_gui_or_solver(
    shell_name: str,
    tmp_path: Path,
) -> None:
    shell = shutil.which(shell_name)
    if shell is None:
        pytest.skip(f"{shell_name} is not installed")
    target = tmp_path / "用户设置" / "settings.json"
    target.parent.mkdir()

    completed = subprocess.run(  # noqa: S603
        [
            shell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(LAUNCHER),
            "-PythonPath",
            sys.executable,
            "-SettingsPath",
            str(target),
            "-ValidateOnly",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip())
    assert result == {
        "schema_name": "comsol_mcp.settings_gui_root_launcher",
        "schema_version": "1.0.0",
        "ready": True,
        "settings_path_override": True,
        "solver_started": False,
    }
    assert not target.exists()


def test_root_launcher_reuses_the_bounded_python_launcher() -> None:
    source = LAUNCHER.read_text(encoding="utf-8-sig")

    assert "comsol_mcp.settings_gui_launcher" in source
    assert "launch_settings_gui" in source
    assert "COMSOL_MCP_SETTINGS_PATH" in source
    assert "Start-Process" not in source


@pytest.mark.parametrize("shell_name", ["powershell.exe", "pwsh.exe"])
def test_root_launcher_discovers_supported_python_from_path(
    shell_name: str,
    tmp_path: Path,
) -> None:
    shell = shutil.which(shell_name)
    if shell is None:
        pytest.skip(f"{shell_name} is not installed")
    target = tmp_path / "settings.json"
    environment = dict(os.environ)
    environment.pop("VIRTUAL_ENV", None)
    environment.pop("COMSOL_MCP_SETTINGS_PATH", None)
    environment["PATH"] = str(Path(sys.executable).parent)

    completed = subprocess.run(  # noqa: S603
        [
            shell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(LAUNCHER),
            "-SettingsPath",
            str(target),
            "-ValidateOnly",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip())
    assert result["ready"] is True
    assert result["settings_path_override"] is True
    assert result["solver_started"] is False
    assert not target.exists()


@pytest.mark.parametrize("shell_name", ["powershell.exe", "pwsh.exe"])
def test_root_launcher_restores_inherited_settings_override(
    shell_name: str,
    tmp_path: Path,
) -> None:
    shell = shutil.which(shell_name)
    if shell is None:
        pytest.skip(f"{shell_name} is not installed")
    inherited = tmp_path / "inherited.json"
    requested = tmp_path / "requested.json"
    environment = dict(os.environ)
    environment["OCR_TEST_INHERITED"] = str(inherited)
    environment["OCR_TEST_LAUNCHER"] = str(LAUNCHER)
    environment["OCR_TEST_PYTHON"] = sys.executable
    environment["OCR_TEST_REQUESTED"] = str(requested)
    command = (
        "$env:COMSOL_MCP_SETTINGS_PATH=$env:OCR_TEST_INHERITED; "
        "& $env:OCR_TEST_LAUNCHER -PythonPath $env:OCR_TEST_PYTHON "
        "-SettingsPath $env:OCR_TEST_REQUESTED -ValidateOnly; "
        "[Console]::Out.WriteLine('AFTER=' + $env:COMSOL_MCP_SETTINGS_PATH)"
    )
    completed = subprocess.run(  # noqa: S603
        [
            shell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.splitlines()
    assert json.loads(lines[0])["settings_path_override"] is True
    assert lines[-1] == f"AFTER={inherited}"


@pytest.mark.parametrize("shell_name", ["powershell.exe", "pwsh.exe"])
def test_root_launcher_ignores_benign_probe_stderr(
    shell_name: str,
    tmp_path: Path,
) -> None:
    shell = shutil.which(shell_name)
    if shell is None:
        pytest.skip(f"{shell_name} is not installed")
    fake_python = tmp_path / "python.cmd"
    fake_python.write_text(
        "@echo benign warning 1>&2\r\n@echo COMSOL_MCP_SETTINGS_GUI_PYTHON_READY\r\n@exit /b 0\r\n",
        encoding="ascii",
    )
    completed = subprocess.run(  # noqa: S603
        [
            shell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(LAUNCHER),
            "-PythonPath",
            str(fake_python),
            "-ValidateOnly",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["ready"] is True


@pytest.mark.parametrize("shell_name", ["powershell.exe", "pwsh.exe"])
@pytest.mark.parametrize("settings_path", [r"\settings.json", "/settings.json"])
def test_root_launcher_rejects_drive_relative_rooted_settings_paths(
    shell_name: str, settings_path: str
) -> None:
    shell = shutil.which(shell_name)
    if shell is None:
        pytest.skip(f"{shell_name} is not installed")

    completed = subprocess.run(  # noqa: S603
        [
            shell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(LAUNCHER),
            "-PythonPath",
            sys.executable,
            "-SettingsPath",
            settings_path,
            "-ValidateOnly",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    assert completed.returncode == 1
    assert "must be an absolute path" in completed.stderr


@pytest.mark.parametrize("shell_name", ["powershell.exe", "pwsh.exe"])
def test_root_launcher_selects_only_the_typed_launch_result(
    shell_name: str, tmp_path: Path
) -> None:
    shell = shutil.which(shell_name)
    if shell is None:
        pytest.skip(f"{shell_name} is not installed")
    fake_python = tmp_path / "python.cmd"
    fake_python.write_text(
        "@echo off\r\n"
        'echo %* | findstr /C:"COMSOL_MCP_SETTINGS_GUI_PYTHON_READY" >nul\r\n'
        "if not errorlevel 1 (\r\n"
        "  echo COMSOL_MCP_SETTINGS_GUI_PYTHON_READY\r\n"
        "  exit /b 0\r\n"
        ")\r\n"
        'echo {"noise":"braced"}\r\n'
        'echo {"success":true,"state":"launched"}\r\n'
        "exit /b 0\r\n",
        encoding="ascii",
    )

    completed = subprocess.run(  # noqa: S603
        [
            shell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(LAUNCHER),
            "-PythonPath",
            str(fake_python),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"success": True, "state": "launched"}


@pytest.mark.parametrize("shell_name", ["powershell.exe", "pwsh.exe"])
def test_root_launcher_times_out_a_hung_requested_python(shell_name: str, tmp_path: Path) -> None:
    shell = shutil.which(shell_name)
    if shell is None:
        pytest.skip(f"{shell_name} is not installed")
    fake_python = tmp_path / "python.cmd"
    fake_python.write_text("@echo off\r\n:loop\r\ngoto loop\r\n", encoding="ascii")

    started = time.monotonic()
    completed = subprocess.run(  # noqa: S603
        [
            shell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(LAUNCHER),
            "-PythonPath",
            str(fake_python),
            "-ValidateOnly",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=12,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    assert completed.returncode == 1
    assert time.monotonic() - started < 9
    assert "selected Python must be CPython 3.14" in completed.stderr
