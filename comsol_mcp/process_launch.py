"""Launch independent owners so the optional shim never ends them.

The 0.8.1 runtime contract (section 8) says a host restart never kills a
durable worker. The optional Rust shim contains its Python worker in a
kill-on-close Windows job object; every process the worker starts inherits
that job, so without help a normal client disconnect would end durable job
workers, their wall watchdog and cancel coordinator, the standalone driver,
and the settings window together with the worker.

The shim's job therefore permits breakaway, and a worker whose shim launch
binding verified (see ``comsol_mcp.server``) asks for
``CREATE_BREAKAWAY_FROM_JOB`` for exactly these independent owners. Every other
child stays contained. When an enclosing job (for example one an MCP client
created) forbids breakaway, Windows refuses with ``ERROR_ACCESS_DENIED``; the
launch is retried inside the job, which is the behavior without the shim.
The direct Python host never requests breakaway, so its launches are
unchanged.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

logger = logging.getLogger(__name__)

CREATE_BREAKAWAY_FROM_JOB = 0x01000000
ERROR_ACCESS_DENIED = 5

BREAKAWAY_REQUESTED = "requested"
BREAKAWAY_REFUSED = "refused_by_enclosing_job"
BREAKAWAY_NOT_REQUESTED = "not_requested"

_breakaway_enabled = False


def enable_job_breakaway() -> None:
    """Request breakaway for independent owners (verified shim workers only)."""
    global _breakaway_enabled
    _breakaway_enabled = os.name == "nt"


def job_breakaway_enabled() -> bool:
    return _breakaway_enabled


def popen_independent_owner(
    factory: Callable[..., Any],
    command: list[str],
    *,
    creationflags: int,
    **kwargs: Any,
) -> tuple[Any, str]:
    """Start one process that must outlive its host; return it and the outcome."""
    if not _breakaway_enabled:
        return factory(command, creationflags=creationflags, **kwargs), BREAKAWAY_NOT_REQUESTED
    try:
        process = factory(
            command, creationflags=creationflags | CREATE_BREAKAWAY_FROM_JOB, **kwargs
        )
        return process, BREAKAWAY_REQUESTED
    except OSError as exc:
        if getattr(exc, "winerror", None) != ERROR_ACCESS_DENIED:
            raise
    logger.warning(
        "An enclosing job object forbids breakaway; %s stays inside the host's job "
        "and ends when that job is closed",
        command[0] if command else "the process",
    )
    return factory(command, creationflags=creationflags, **kwargs), BREAKAWAY_REFUSED


__all__ = [
    "BREAKAWAY_NOT_REQUESTED",
    "BREAKAWAY_REFUSED",
    "BREAKAWAY_REQUESTED",
    "CREATE_BREAKAWAY_FROM_JOB",
    "enable_job_breakaway",
    "job_breakaway_enabled",
    "popen_independent_owner",
]
