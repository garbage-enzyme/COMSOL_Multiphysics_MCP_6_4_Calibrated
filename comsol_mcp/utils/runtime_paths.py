"""Shared, ASCII-safe locations for durable MCP runtime artifacts."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from comsol_mcp.settings import settings_environment


def _is_windows() -> bool:
    return os.name == "nt"


def default_runtime_dir(environ: dict[str, str] | None = None) -> Path:
    """Return the common root for leases and durable jobs.

    ``COMSOL_MCP_RUNTIME_DIR`` is authoritative.  For backward compatibility,
    setting only ``COMSOL_MCP_JOBS_DIR`` makes its parent the common root.
    """
    source = os.environ if environ is None else environ
    configured = source.get("COMSOL_MCP_RUNTIME_DIR")
    if configured:
        return Path(configured)

    configured_jobs = source.get("COMSOL_MCP_JOBS_DIR")
    if configured_jobs:
        return Path(configured_jobs).parent

    environment = settings_environment(environ)
    configured = environment.get("COMSOL_MCP_RUNTIME_DIR")
    if configured:
        return Path(configured)

    configured_jobs = environment.get("COMSOL_MCP_JOBS_DIR")
    if configured_jobs:
        return Path(configured_jobs).parent

    if _is_windows():
        program_data = environment.get("PROGRAMDATA")
        if program_data:
            return Path(program_data) / "comsol_mcp_runtime"
    temporary = Path(tempfile.gettempdir())
    if not str(temporary).isascii():
        raise RuntimeError(
            "no configured ASCII runtime root is available; set COMSOL_MCP_RUNTIME_DIR"
        )
    return temporary / "comsol_runtime"


def default_jobs_root(environ: dict[str, str] | None = None) -> Path:
    """Return the durable job directory, guaranteed to share the lease root."""
    environment = settings_environment(environ)
    configured = environment.get("COMSOL_MCP_JOBS_DIR")
    runtime_dir = default_runtime_dir(environ)
    if configured:
        jobs_dir = Path(configured)
        source = os.environ if environ is None else environ
        comparison_runtime = (
            source.get("COMSOL_MCP_RUNTIME_DIR")
            if "COMSOL_MCP_JOBS_DIR" in source
            else environment.get("COMSOL_MCP_RUNTIME_DIR")
        )
        if comparison_runtime and jobs_dir.parent.resolve(strict=False) != runtime_dir.resolve(
            strict=False
        ):
            raise ValueError(
                "COMSOL_MCP_JOBS_DIR must be the jobs subdirectory of COMSOL_MCP_RUNTIME_DIR"
            )
        return jobs_dir
    return runtime_dir / "jobs"
