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
