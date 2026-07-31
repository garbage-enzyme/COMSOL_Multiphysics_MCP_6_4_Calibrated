"""Regression tests for the shared durable-job and solver-lease root."""

from __future__ import annotations

from pathlib import Path

import pytest
import src.utils.runtime_paths as runtime_paths
from src.jobs.store import default_jobs_root
from src.tools.ownership import _default_runtime_dir


def test_windows_without_d_drive_uses_programdata_for_both_roots(monkeypatch):
    monkeypatch.setattr(
        runtime_paths,
        "settings_environment",
        lambda _environ=None: {"PROGRAMDATA": "C:/ProgramData"},
    )
    monkeypatch.setattr(runtime_paths, "_is_windows", lambda: True)
    monkeypatch.setattr(runtime_paths, "_has_d_runtime_drive", lambda: False)

    assert _default_runtime_dir() == Path("C:/ProgramData/comsol_mcp_runtime")
    assert default_jobs_root() == Path("C:/ProgramData/comsol_mcp_runtime/jobs")


def test_jobs_override_also_sets_lease_root_when_runtime_is_not_explicit(monkeypatch):
    monkeypatch.delenv("COMSOL_MCP_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("COMSOL_MCP_JOBS_DIR", "E:/durable/jobs")

    assert _default_runtime_dir() == Path("E:/durable")
    assert default_jobs_root() == Path("E:/durable/jobs")


def test_conflicting_runtime_and_jobs_configuration_fails_closed(monkeypatch):
    monkeypatch.setenv("COMSOL_MCP_RUNTIME_DIR", "E:/runtime")
    monkeypatch.setenv("COMSOL_MCP_JOBS_DIR", "F:/other/jobs")

    with pytest.raises(ValueError, match="jobs subdirectory"):
        default_jobs_root()


def test_equivalent_normalized_runtime_and_jobs_configuration_is_accepted(monkeypatch):
    monkeypatch.setenv("COMSOL_MCP_RUNTIME_DIR", "E:/runtime")
    monkeypatch.setenv("COMSOL_MCP_JOBS_DIR", "E:/runtime/../runtime/jobs")

    assert default_jobs_root() == Path("E:/runtime/../runtime/jobs")
