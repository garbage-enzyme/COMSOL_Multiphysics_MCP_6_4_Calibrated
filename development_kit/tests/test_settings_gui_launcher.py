"""Solver-free Settings GUI launch and MCP bridge tests."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from comsol_mcp import settings_gui_launcher as launcher
from comsol_mcp.server import create_server
from comsol_mcp.settings_gui_handshake import publish_handshake


def _call_tool(server, name: str, arguments: dict) -> dict:
    result = asyncio.run(server.call_tool(name, arguments))
    if isinstance(result, dict):
        return result
    for block in result:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            value = json.loads(text)
            if isinstance(value, dict):
                return value
    raise ValueError("public FastMCP call did not return a JSON object")


def test_detached_launch_uses_pythonw_devnull_and_ready_handshake(
    ascii_tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(launcher, "settings_gui_is_running", lambda _target: False)
    executable = ascii_tmp_path / "python.exe"
    pythonw = ascii_tmp_path / "pythonw.exe"
    executable.write_bytes(b"")
    pythonw.write_bytes(b"")
    captured = {}

    class FakeProcess:
        def poll(self):
            return None

        def wait(self):
            return 0

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        assert publish_handshake("ready", kwargs["env"]) is True
        return FakeProcess()

    result = launcher.launch_settings_gui(
        environ={},
        runtime_dir=ascii_tmp_path,
        executable=executable,
        popen_factory=fake_popen,
    )

    assert result == {
        "success": True,
        "state": "launched",
        "gui_release": "alpha6.1",
        "restart_required_after_change": True,
        "message_code": "settings_gui_opened",
        "contains_local_path": False,
        "agent_action_required": "pause_for_user",
    }
    assert os.path.normcase(captured["command"][0]) == os.path.normcase(
        str(pythonw.resolve(strict=False))
    )
    assert captured["command"][1:] == [
        "-m",
        "settings_gui",
        "--settings-path",
        str((Path(__file__).parents[2] / "settings.json").resolve()),
    ]
    assert captured["kwargs"]["stdin"] == subprocess.DEVNULL
    assert captured["kwargs"]["stdout"] == subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] == subprocess.DEVNULL
    assert captured["kwargs"]["close_fds"] is True
    assert "startupinfo" not in captured["kwargs"]
    assert not list((ascii_tmp_path / "settings_gui").glob("*.json"))


def test_second_call_reports_already_running_without_launch(ascii_tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(launcher, "settings_gui_is_running", lambda _target: True)
    launched = []

    result = launcher.launch_settings_gui(
        environ={},
        runtime_dir=ascii_tmp_path,
        popen_factory=lambda *_args, **_kwargs: launched.append(True),
    )

    assert result["success"] is True
    assert result["state"] == "already_running"
    assert result["agent_action_required"] == "pause_for_user"
    assert launched == []


def test_instance_mutex_reports_a_live_gui_in_another_process(ascii_tmp_path: Path) -> None:
    target = ascii_tmp_path / "settings.json"
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from comsol_mcp.settings_gui_launcher import SettingsGuiInstanceLock\n"
        "with SettingsGuiInstanceLock(Path(sys.argv[1])):\n"
        " print('READY', flush=True)\n"
        " sys.stdin.readline()\n"
    )
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", script, str(target)],
        cwd=Path(__file__).parents[2],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "READY"
        assert launcher.settings_gui_is_running(target) is True
    finally:
        output, errors = process.communicate("done\n", timeout=10)
        assert process.returncode == 0, output + errors
    assert launcher.settings_gui_is_running(target) is False


def test_child_runtime_failure_is_stable_and_redacted(ascii_tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(launcher, "settings_gui_is_running", lambda _target: False)

    class FakeProcess:
        def poll(self):
            return 2

        def wait(self):
            return 2

    def fake_popen(_command, **kwargs):
        assert publish_handshake("gui_runtime_unavailable", kwargs["env"]) is True
        return FakeProcess()

    result = launcher.launch_settings_gui(
        environ={},
        runtime_dir=ascii_tmp_path,
        popen_factory=fake_popen,
    )

    assert result["success"] is False
    assert result["state"] == "gui_runtime_unavailable"
    assert result["contains_local_path"] is False
    assert str(ascii_tmp_path) not in json.dumps(result)


def test_launch_timeout_is_bounded_and_cleans_handshake(ascii_tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(launcher, "settings_gui_is_running", lambda _target: False)
    now = [0.0]

    class FakeProcess:
        def poll(self):
            return None

        def wait(self):
            return 0

    def sleep(seconds: float) -> None:
        now[0] += seconds

    result = launcher.launch_settings_gui(
        environ={},
        runtime_dir=ascii_tmp_path,
        popen_factory=lambda *_args, **_kwargs: FakeProcess(),
        clock=lambda: now[0],
        sleeper=sleep,
        timeout_seconds=0.1,
    )

    assert result["state"] == "launch_failed"
    assert now[0] <= 0.15
    assert not list((ascii_tmp_path / "settings_gui").glob("*.json"))


def test_public_dispatch_has_no_arguments_and_returns_tool_result(monkeypatch) -> None:
    expected = {
        "success": True,
        "state": "already_running",
        "gui_release": "alpha6.1",
        "restart_required_after_change": True,
        "message_code": "settings_gui_already_open",
        "contains_local_path": False,
        "agent_action_required": "pause_for_user",
    }
    monkeypatch.setattr(
        "comsol_mcp.tools.settings_gui.launch_settings_gui",
        lambda: expected,
    )
    server = create_server("settings-gui-dispatch", profile="core")

    result = _call_tool(server, "settings.start", {})
    assert {key: result[key] for key in expected} == expected
    assert result["path_policy"]["paths_included"] is False


def test_mcp_server_registration_never_imports_tkinter() -> None:
    script = (
        "import json,sys\n"
        "from comsol_mcp.server import create_server\n"
        "server=create_server(profile='core')\n"
        "print(json.dumps({'tkinter': 'tkinter' in sys.modules, "
        "'tool': 'settings.start' in server._tool_manager._tools}))\n"
    )
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    result = json.loads(completed.stdout.strip())

    assert result == {"tkinter": False, "tool": True}
