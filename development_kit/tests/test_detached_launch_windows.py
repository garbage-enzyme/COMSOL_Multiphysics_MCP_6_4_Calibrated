"""No production launch may open a console or terminal window.

A process started without any console (DETACHED_PROCESS) makes every
console-subsystem child it starts allocate a fresh, visible console. With
Windows Terminal as the default terminal, each one is a focus-stealing
window. The durable job, watchdog, cancel and standalone launchers therefore
use a hidden console instead, which their children inherit, and every other
production process launch passes explicit creation flags.
"""

from __future__ import annotations

import ast
import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from comsol_mcp.jobs import manager

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_PACKAGES = ("comsol_mcp", "settings_gui")
SPAWN_FUNCTIONS = {"Popen", "run", "call", "check_call", "check_output"}
CONSOLE_WINDOW_CLASSES = {"ConsoleWindowClass", "PseudoConsoleWindow"}
windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows console allocation")


def _production_sources() -> list[Path]:
    sources = []
    for package in PRODUCTION_PACKAGES:
        for path in sorted((ROOT / package).rglob("*.py")):
            if "tests" not in path.relative_to(ROOT).parts:
                sources.append(path)
    return sources


def test_every_production_subprocess_launch_passes_creation_flags():
    missing = []
    for path in _production_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not (
                isinstance(function, ast.Attribute)
                and function.attr in SPAWN_FUNCTIONS
                and isinstance(function.value, ast.Name)
                and function.value.id == "subprocess"
            ):
                continue
            if not any(keyword.arg == "creationflags" for keyword in node.keywords):
                missing.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
    assert missing == [], f"subprocess launches without creationflags: {missing}"


def test_no_production_launch_uses_a_console_less_detached_process_for_console_programs():
    allowed = {"comsol_mcp/settings_gui_launcher.py"}  # Launches pythonw: a GUI program.
    offenders = []
    for path in _production_sources():
        relative = path.relative_to(ROOT).as_posix()
        if relative in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "DETACHED_PROCESS":
                offenders.append(f"{relative}:{node.lineno}")
            if isinstance(node, ast.Attribute) and node.attr == "DETACHED_PROCESS":
                offenders.append(f"{relative}:{node.lineno}")
    assert offenders == [], f"DETACHED_PROCESS used for console programs: {offenders}"


@windows_only
def test_detached_launch_flags_use_a_hidden_console_not_detached_process():
    flags = manager._detached_creation_flags()
    assert flags & subprocess.CREATE_NO_WINDOW
    assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
    assert not flags & subprocess.DETACHED_PROCESS


def _console_windows_owned_by(pids: set[int]) -> list[tuple[str, int]]:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found: list[tuple[str, int]] = []

    def callback(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            name = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, name, 128)
            if owner.value in pids and name.value in CONSOLE_WINDOW_CLASSES:
                found.append((name.value, owner.value))
        return True

    user32.EnumWindows(callback_type(callback), 0)
    return found


@windows_only
def test_a_detached_worker_and_its_console_children_open_no_window():
    script = (
        "import subprocess, sys, time;"
        "subprocess.run([sys.executable, '-c', 'import time; time.sleep(1.5)']);"
        "time.sleep(0.5)"
    )
    root = subprocess.Popen(  # noqa: S603 - fixed interpreter argv
        [sys.executable, "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=manager._detached_creation_flags(),
    )
    seen: list[tuple[str, int]] = []
    tree: set[int] = {root.pid}
    deadline = time.monotonic() + 30
    while root.poll() is None and time.monotonic() < deadline:
        try:
            tree |= {child.pid for child in psutil.Process(root.pid).children(recursive=True)}
        except psutil.Error:
            pass
        seen.extend(_console_windows_owned_by(tree))
        time.sleep(0.02)
    if root.poll() is None:
        root.kill()
    root.wait(timeout=10)
    assert len(tree) >= 2, "the launched tree was observed"
    assert seen == [], f"console windows appeared: {sorted(set(seen))}"
