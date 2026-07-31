"""Shared test-environment preparation for ASCII runtime fixtures."""

import os
import shutil
import tempfile
from pathlib import Path

import pytest


def _ascii_temp_candidates() -> tuple[Path, ...]:
    candidates = []
    configured = os.environ.get("COMSOL_MCP_TEST_ASCII_ROOT")
    if configured:
        candidates.append(Path(configured))
    candidates.extend(
        [
            Path(tempfile.gettempdir()),
            Path(os.environ.get("SystemRoot", "C:/Windows")) / "Temp",
            Path("D:/comsol_runtime_test"),
        ]
    )
    return tuple(candidates)


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
    raise OSError("no writable ASCII temporary directory is available")


@pytest.fixture
def ascii_tmp_path():
    root = _create_ascii_temp_dir()
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)
