"""Shared test-environment preparation for ASCII runtime fixtures."""

import os
import shutil
import tempfile
from pathlib import Path

import pytest


def _ascii_temp_candidates() -> tuple[Path, ...]:
    configured = os.environ.get("COMSOL_MCP_TEST_ASCII_ROOT")
    if configured:
        return (Path(configured),)
    from development_kit.tests.platform_fixtures import platform_test_root

    return (platform_test_root(),)


def _create_ascii_temp_dir(*, candidates=None) -> Path:
    """Create an isolated directory under the first writable ASCII parent."""
    for parent in candidates or _ascii_temp_candidates():
        parent = Path(parent)
        if not str(parent).isascii():
            continue
        try:
            parent.mkdir(parents=True, exist_ok=True)
            return Path(tempfile.mkdtemp(prefix="comsol-mcp-test-", dir=parent))
        except OSError:
            continue
    raise OSError(
        "no writable ASCII temporary directory is available; "
        "set COMSOL_MCP_TEST_ASCII_ROOT to an absolute ASCII path"
    )


@pytest.fixture
def ascii_tmp_path():
    root = _create_ascii_temp_dir()
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _hide_child_console_windows():
    """Keep every console-subsystem child of the suite from opening a window.

    The suite runs under console-less parents (harness shells, hosted CI).
    On Windows a console-subsystem child of a console-less parent otherwise
    allocates a visible, focus-stealing console per spawn. Every spawn gains
    ``CREATE_NO_WINDOW`` and loses ``CREATE_NEW_CONSOLE``. This governs only
    console allocation: real GUI windows that GUI tests must capture are not
    consoles and stay visible. Non-Windows platforms are unchanged.
    """
    if os.name != "nt":
        yield
        return

    import subprocess

    original_popen = subprocess.Popen
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    new_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

    def _popen_hidden(*args, **kwargs):
        flags = kwargs.get("creationflags") or 0
        kwargs["creationflags"] = (flags & ~new_console) | no_window
        return original_popen(*args, **kwargs)

    subprocess.Popen = _popen_hidden
    try:
        yield
    finally:
        subprocess.Popen = original_popen


@pytest.fixture(scope="session", autouse=True)
def _synthetic_runtime_isolation():
    """Scope one synthetic runtime root per test worker before any ownership.

    The production default shares one runtime directory (and therefore one
    solver lease) across agent processes on purpose. Synthetic tests running in
    parallel workers must not contend on that shared default, so each worker
    session derives its own ASCII runtime root and selects it through the
    supported configuration variable; subprocesses inherit it with the rest of
    the environment. An explicitly configured value always wins - this never
    overrides caller configuration, and it is test isolation, never solver
    authorization.
    """
    from development_kit.tests.platform_fixtures import select_synthetic_runtime_dir

    root = select_synthetic_runtime_dir()
    if root is None:
        yield
        return
    os.environ["COMSOL_MCP_RUNTIME_DIR"] = str(root)
    try:
        yield
    finally:
        shutil.rmtree(root, ignore_errors=True)
