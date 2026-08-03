"""Repository-root manual Settings GUI launcher tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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
