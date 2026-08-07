"""Caller-declared host resources for licensed integration entry points."""

from __future__ import annotations

import os
from collections.abc import Mapping

ACCEPTANCE_CORES_ENV = "COMSOL_MCP_ACCEPTANCE_CORES"


def required_acceptance_cores(environ: Mapping[str, str] | None = None) -> int:
    """Return an explicit positive core count bounded by live host capacity."""
    source = os.environ if environ is None else environ
    raw = source.get(ACCEPTANCE_CORES_ENV)
    if raw is None or not raw.strip():
        raise RuntimeError(f"{ACCEPTANCE_CORES_ENV} must be explicitly configured")
    try:
        cores = int(raw)
    except ValueError as exc:
        raise ValueError(f"{ACCEPTANCE_CORES_ENV} must be a positive integer") from exc
    if cores < 1 or str(cores) != raw.strip():
        raise ValueError(f"{ACCEPTANCE_CORES_ENV} must be a canonical positive integer")
    available = os.cpu_count()
    if not isinstance(available, int) or available < 1:
        raise RuntimeError("live host CPU capacity is unavailable")
    if cores > available:
        raise ValueError(f"{ACCEPTANCE_CORES_ENV} exceeds live host CPU capacity")
    return cores


__all__ = ["ACCEPTANCE_CORES_ENV", "required_acceptance_cores"]
