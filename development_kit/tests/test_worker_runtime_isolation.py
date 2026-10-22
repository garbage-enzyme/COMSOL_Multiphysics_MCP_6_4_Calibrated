"""Per-worker synthetic runtime-root isolation and shared-default exclusion."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from development_kit.tests.platform_fixtures import synthetic_runtime_root


def test_synthetic_runtime_roots_are_unique_and_ascii():
    first = synthetic_runtime_root()
    second = synthetic_runtime_root()
    try:
        assert first != second
        assert str(first).isascii()
        assert str(second).isascii()
    finally:
        shutil.rmtree(first, ignore_errors=True)
        shutil.rmtree(second, ignore_errors=True)


def test_worker_environment_selects_distinct_runtime_roots_in_subprocesses():
    program = (
        "from development_kit.tests.platform_fixtures import synthetic_runtime_root; "
        "print(synthetic_runtime_root())"
    )
    roots = []
    try:
        for worker in ("gw0", "gw1"):
            result = subprocess.run(
                [sys.executable, "-c", program],
                capture_output=True,
                text=True,
                env=dict(os.environ, PYTEST_XDIST_WORKER=worker),
                cwd=str(Path(__file__).resolve().parents[2]),
                check=True,
            )
            root = Path(result.stdout.strip())
            assert root.name.startswith(f"comsol-mcp-rt-{worker}-")
            assert str(root).isascii()
            roots.append(root)
        assert roots[0] != roots[1]
    finally:
        for root in roots:
            shutil.rmtree(root, ignore_errors=True)


def test_isolation_selection_never_overrides_explicit_configuration(monkeypatch):
    from development_kit.tests.platform_fixtures import select_synthetic_runtime_dir

    assert os.environ.get("COMSOL_MCP_RUNTIME_DIR"), (
        "the session fixture must have selected a synthetic runtime root before tests ran"
    )

    monkeypatch.setenv("COMSOL_MCP_RUNTIME_DIR", "E:/explicit/runtime")
    assert select_synthetic_runtime_dir() is None

    monkeypatch.delenv("COMSOL_MCP_RUNTIME_DIR")
    selected = select_synthetic_runtime_dir()
    try:
        assert selected is not None
        assert str(selected).isascii()
    finally:
        if selected is not None:
            shutil.rmtree(selected, ignore_errors=True)
