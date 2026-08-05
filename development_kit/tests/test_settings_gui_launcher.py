"""Solver-free Settings GUI launch and MCP bridge tests."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from comsol_mcp import settings_gui_handshake as handshake_module
from comsol_mcp import settings_gui_launcher as launcher
from comsol_mcp.server import create_server
from comsol_mcp.settings_gui_handshake import (
    publish_handshake,
    read_handshake,
    validate_handshake_path,
)
from development_kit.tests.mcp_test_support import decode_tool_result


def _call_tool(server, name: str, arguments: dict) -> dict:
    return decode_tool_result(asyncio.run(server.call_tool(name, arguments)))


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
        "gui_release": "alpha6.4",
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
        ready_lines: queue.Queue[str] = queue.Queue(maxsize=1)
        reader = threading.Thread(
            target=lambda: ready_lines.put(process.stdout.readline()), daemon=True
        )
        reader.start()
        try:
            assert ready_lines.get(timeout=10).strip() == "READY"
        except queue.Empty:
            pytest.fail("Settings GUI mutex child did not publish readiness within 10 seconds")
        assert launcher.settings_gui_is_running(target) is True
    finally:
        try:
            output, errors = process.communicate("done\n", timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            output, errors = process.communicate(timeout=10)
            pytest.fail(f"Settings GUI mutex child did not exit after input: {output}{errors}")
        reader.join(timeout=1)
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
        terminated = False

        def poll(self):
            return 0 if self.terminated else None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    def sleep(seconds: float) -> None:
        now[0] += seconds

    process = FakeProcess()

    result = launcher.launch_settings_gui(
        environ={},
        runtime_dir=ascii_tmp_path,
        popen_factory=lambda *_args, **_kwargs: process,
        clock=lambda: now[0],
        sleeper=sleep,
        timeout_seconds=0.1,
    )

    assert result["state"] == "launch_failed"
    assert now[0] <= 0.15
    assert result["success"] is False
    assert process.terminated is True
    assert not list((ascii_tmp_path / "settings_gui").glob("*.json"))


def test_handshake_rejects_unhashable_state(ascii_tmp_path: Path) -> None:
    root = ascii_tmp_path / "settings_gui"
    root.mkdir()
    path = root / ".settings-gui-0123456789abcdef0123456789abcdef.json"
    path.write_text('{"state":[]}', encoding="ascii")

    assert read_handshake(path) is None


def test_publish_handshake_rechecks_pending_state_before_replace(
    ascii_tmp_path: Path, monkeypatch
) -> None:
    root = ascii_tmp_path / "settings_gui"
    root.mkdir()
    path = root / ".settings-gui-0123456789abcdef0123456789abcdef.json"
    path.write_bytes(handshake_module.handshake_bytes("pending"))
    observed = [
        handshake_module.handshake_payload("pending"),
        handshake_module.handshake_payload("already_running"),
    ]
    monkeypatch.setattr(
        handshake_module, "read_handshake", lambda _path: observed.pop(0)
    )

    assert publish_handshake("ready", {handshake_module.HANDSHAKE_ENV: str(path)}) is False
    assert path.read_bytes() == handshake_module.handshake_bytes("pending")
    assert not list(root.glob("*.tmp"))


def test_publish_handshake_cleanup_error_does_not_replace_success(
    ascii_tmp_path: Path, monkeypatch
) -> None:
    root = ascii_tmp_path / "settings_gui"
    root.mkdir()
    path = root / ".settings-gui-0123456789abcdef0123456789abcdef.json"
    path.write_bytes(handshake_module.handshake_bytes("pending"))
    original_unlink = Path.unlink

    def fail_missing_temporary(candidate: Path, *args, **kwargs):
        if candidate.name.endswith(".tmp") and not candidate.exists():
            raise PermissionError("injected cleanup sharing failure")
        return original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_missing_temporary)

    assert publish_handshake("ready", {handshake_module.HANDSHAKE_ENV: str(path)}) is True


def test_launcher_cleanup_error_does_not_replace_ready_result(
    ascii_tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(launcher, "settings_gui_is_running", lambda _target: False)
    original_unlink = Path.unlink
    handshake = None

    class Process:
        def poll(self):
            return None

        def wait(self):
            return 0

    def start(_command, **kwargs):
        nonlocal handshake
        handshake = Path(kwargs["env"][handshake_module.HANDSHAKE_ENV])
        assert publish_handshake("ready", kwargs["env"]) is True
        return Process()

    def deny_cleanup(candidate: Path, *args, **kwargs):
        if handshake is not None and candidate == handshake:
            raise PermissionError("injected launcher cleanup sharing failure")
        return original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_cleanup)

    result = launcher.launch_settings_gui(
        environ={}, runtime_dir=ascii_tmp_path, popen_factory=start
    )

    assert result["state"] == "launched"
    assert handshake is not None
    original_unlink(handshake)


def test_instance_mutex_distinguishes_wait_api_failure(monkeypatch, tmp_path) -> None:
    class Kernel:
        def CreateMutexW(self, *_args):
            return 1

        def WaitForSingleObject(self, *_args):
            return 0xFFFFFFFF

    lock = object.__new__(launcher.SettingsGuiInstanceLock)
    lock.name = "test"
    lock._kernel32 = Kernel()
    lock._handle = None
    lock._acquired = False
    monkeypatch.setattr(lock, "close", lambda: setattr(lock, "_handle", None))

    with pytest.raises(OSError, match="WaitForSingleObject"):
        lock.acquire()


def test_handshake_rejects_a_linked_parent_before_resolution(
    ascii_tmp_path: Path,
    monkeypatch,
) -> None:
    parent = ascii_tmp_path / "settings_gui"
    parent.mkdir()
    target = parent / ".settings-gui-0123456789abcdef0123456789abcdef.json"
    original_is_symlink = Path.is_symlink

    def fake_is_symlink(path: Path) -> bool:
        return path == parent or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)

    with pytest.raises(ValueError, match="handshake path is invalid"):
        validate_handshake_path(target)


def test_public_dispatch_has_no_arguments_and_returns_tool_result(monkeypatch) -> None:
    expected = {
        "success": True,
        "state": "already_running",
        "gui_release": "alpha6.4",
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
