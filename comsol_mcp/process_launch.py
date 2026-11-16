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
child stays contained. The direct Python host never requests breakaway, so
its launches are unchanged.

A launch that must outlive its host either leaves the job or does not start:
when the enclosing job denies breakaway, this module raises
:class:`JobBreakawayDenied` instead of starting a process that would die with
the host. A dependent "independent owner" would silently lose the contract's
lifetime guarantee, so the refusal must happen before any dependent work.
"""

from __future__ import annotations

import os
from typing import Any, Callable

CREATE_BREAKAWAY_FROM_JOB = 0x01000000
ERROR_ACCESS_DENIED = 5

BREAKAWAY_REQUESTED = "requested"
BREAKAWAY_NOT_REQUESTED = "not_requested"

_breakaway_enabled = False


class JobBreakawayDenied(OSError):
    """The enclosing job forbids breakaway, so the launch was refused.

    No process was started; nothing dependent on an independent lifetime may
    proceed from this failure.
    """

    def __init__(self, command: list[str]):
        self.command = list(command)
        super().__init__(
            ERROR_ACCESS_DENIED,
            "the enclosing job object forbids breakaway, so this independent "
            f"owner was not started: {command[0] if command else 'the process'}",
        )
        self.winerror = ERROR_ACCESS_DENIED


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
    """Start one process that must outlive its host; return it and the outcome.

    Raises :class:`JobBreakawayDenied` when breakaway is required but the
    enclosing job denies it — before any process is started.
    """
    if not _breakaway_enabled:
        return factory(command, creationflags=creationflags, **kwargs), BREAKAWAY_NOT_REQUESTED
    try:
        process = factory(
            command, creationflags=creationflags | CREATE_BREAKAWAY_FROM_JOB, **kwargs
        )
        return process, BREAKAWAY_REQUESTED
    except OSError as exc:
        if getattr(exc, "winerror", None) == ERROR_ACCESS_DENIED:
            raise JobBreakawayDenied(command) from exc
        raise


__all__ = [
    "BREAKAWAY_NOT_REQUESTED",
    "BREAKAWAY_REQUESTED",
    "CREATE_BREAKAWAY_FROM_JOB",
    "JobBreakawayDenied",
    "enable_job_breakaway",
    "job_breakaway_enabled",
    "popen_independent_owner",
]
