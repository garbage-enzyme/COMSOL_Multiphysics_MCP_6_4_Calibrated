"""Shared, ASCII-safe locations for durable MCP runtime artifacts."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

from comsol_mcp.settings import settings_environment


def _is_windows() -> bool:
    return os.name == "nt"


def _has_d_runtime_drive() -> bool:
    return _is_windows() and Path("D:/").exists()


def default_runtime_dir(environ: dict[str, str] | None = None) -> Path:
    """Return the common root for leases and durable jobs.

    ``COMSOL_MCP_RUNTIME_DIR`` is authoritative.  For backward compatibility,
    setting only ``COMSOL_MCP_JOBS_DIR`` makes its parent the common root.
    """
    environment = settings_environment(environ)
    configured = environment.get("COMSOL_MCP_RUNTIME_DIR")
    if configured:
        return Path(configured)

    configured_jobs = environment.get("COMSOL_MCP_JOBS_DIR")
    if configured_jobs:
        return Path(configured_jobs).parent

    if _has_d_runtime_drive():
        return Path("D:/comsol_runtime")
    if _is_windows():
        program_data = Path(environment.get("PROGRAMDATA", "C:/ProgramData"))
        return program_data / "comsol_mcp_runtime"
    return Path(tempfile.gettempdir()) / "comsol_runtime"


def default_jobs_root(environ: dict[str, str] | None = None) -> Path:
    """Return the durable job directory, guaranteed to share the lease root."""
    environment = settings_environment(environ)
    configured = environment.get("COMSOL_MCP_JOBS_DIR")
    runtime_dir = default_runtime_dir(environ)
    if configured:
        jobs_dir = Path(configured)
        explicit_runtime = environment.get("COMSOL_MCP_RUNTIME_DIR")
        if explicit_runtime and jobs_dir.parent != runtime_dir:
            raise ValueError(
                "COMSOL_MCP_JOBS_DIR must be the jobs subdirectory of "
                "COMSOL_MCP_RUNTIME_DIR"
            )
        return jobs_dir
    return runtime_dir / "jobs"
