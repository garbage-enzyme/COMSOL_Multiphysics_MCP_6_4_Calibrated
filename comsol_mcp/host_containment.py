"""Host containment: the complete descendant evidence for stale recovery.

Windows: the host assigns itself to a named job object at startup and records
the name in the admission lock. Every process the host starts inherits the
job unless it explicitly breaks away (independent owners do). A recovery
reader opens the job by name and asks the kernel for its member list: that
list is authoritative and complete regardless of ppid readability. The job
object keeps existing while any member process lives, so a name that no
longer opens proves no descendant remains.

POSIX: the host becomes a session leader at startup and records the session
id. Descendants inherit the session unless they start a new one (independent
owners do). A recovery reader lists every process whose session matches.
Unreadable evidence is UNKNOWN, never absence.

The job is only a membership registry: it sets no kill-on-close limit, so a
normal host exit never ends contained children by itself.
"""

from __future__ import annotations

import ctypes
import os
import uuid
from typing import Any

WINDOWS = os.name == "nt"

if WINDOWS:
    import ctypes.wintypes as wt

    c_void_p = ctypes.c_void_p

    def _load_kernel32() -> Any:
        # ``WinDLL`` exists only on Windows typeshed; resolve it by name so
        # strict mypy accepts the module on every platform.
        factory = getattr(ctypes, "WinDLL")
        return factory("kernel32", use_last_error=True)

    _kernel32 = _load_kernel32()
    _kernel32.CreateJobObjectW.restype = wt.HANDLE
    _kernel32.CreateJobObjectW.argtypes = [c_void_p, wt.LPCWSTR]
    _kernel32.OpenJobObjectW.restype = wt.HANDLE
    _kernel32.OpenJobObjectW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
    _kernel32.AssignProcessToJobObject.restype = wt.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    _kernel32.GetCurrentProcess.restype = wt.HANDLE
    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.CloseHandle.restype = wt.BOOL
    _kernel32.CloseHandle.argtypes = [wt.HANDLE]
    _kernel32.QueryInformationJobObject.restype = wt.BOOL
    _kernel32.QueryInformationJobObject.argtypes = [
        wt.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.POINTER(wt.DWORD),
    ]

    _JOB_OBJECT_QUERY = 0x0004
    _JOB_OBJECT_PROCESS_ID_LIST = 3
    _ERROR_FILE_NOT_FOUND = 2
    _ERROR_INSUFFICIENT_BUFFER = 122


def _last_error() -> int:
    """The thread's ctypes last error; 0 where the platform lacks the call."""
    getter = getattr(ctypes, "get_last_error", None)
    return int(getter()) if getter is not None else 0


class UnknownContainment(RuntimeError):
    """The containment evidence could not be read; the state is UNKNOWN."""


def host_containment_id(*, pid: int) -> dict[str, Any]:
    """The containment identity this host should adopt and record."""
    if WINDOWS:
        return {"kind": "windows_job", "name": f"Local\\comsol_mcp_host_{pid}_{uuid.uuid4().hex}"}
    return {"kind": "posix_session", "id": pid}


def adopt_host_containment(identity: dict[str, Any]) -> Any:
    """Adopt the containment identity; returns an owned handle (Windows).

    POSIX: becomes a session leader (idempotent) and returns None.
    """
    if WINDOWS:
        name = identity.get("name") if isinstance(identity, dict) else None
        if not isinstance(name, str) or not name:
            raise ValueError("a windows containment identity requires a job name")
        handle = _kernel32.CreateJobObjectW(None, name)
        if not handle:
            raise OSError(_last_error(), "CreateJobObjectW failed")
        if not _kernel32.AssignProcessToJobObject(handle, _kernel32.GetCurrentProcess()):
            error = _last_error()
            _kernel32.CloseHandle(handle)
            raise OSError(error, "AssignProcessToJobObject failed")
        return handle
    setsid = getattr(os, "setsid", None)
    if setsid is not None:
        try:
            setsid()
        except OSError:
            pass  # Already a session leader; membership is what matters.
    return None


def containment_members(identity: dict[str, Any]) -> set[int]:
    """Live member PIDs of one recorded containment identity.

    Raises :class:`UnknownContainment` when the evidence is unreadable. An
    absent Windows job name means no member is alive (the object dies with
    its last member), so that returns an empty set.
    """
    kind = identity.get("kind") if isinstance(identity, dict) else None
    if kind == "windows_job":
        if not WINDOWS:
            raise UnknownContainment("a windows job record cannot be read on this platform")
        name = identity.get("name")
        if not isinstance(name, str) or not name:
            raise UnknownContainment("the recorded job name is missing")
        handle = _kernel32.OpenJobObjectW(_JOB_OBJECT_QUERY, False, name)
        if not handle:
            error = _last_error()
            if error == _ERROR_FILE_NOT_FOUND:
                return set()  # The object died with its last member.
            raise UnknownContainment(f"the host job cannot be opened: winerror {error}")
        try:

            class _Header(ctypes.Structure):
                _fields_ = [("count", wt.DWORD), ("returned", wt.DWORD)]

            needed = wt.DWORD(0)
            # JOBOBJECT_BASIC_PROCESS_ID_LIST needs room for the header plus
            # at least one entry, so size up front and grow on demand.
            capacity = ctypes.sizeof(_Header) + 4096 * ctypes.sizeof(ctypes.c_size_t)
            while True:
                buffer = ctypes.create_string_buffer(capacity)
                if _kernel32.QueryInformationJobObject(
                    handle,
                    _JOB_OBJECT_PROCESS_ID_LIST,
                    buffer,
                    capacity,
                    ctypes.byref(needed),
                ):
                    break
                error = _last_error()
                if error == _ERROR_INSUFFICIENT_BUFFER and needed.value > capacity:
                    capacity = needed.value
                    continue
                raise UnknownContainment(f"the host job members cannot be listed: winerror {error}")
            filled = ctypes.cast(buffer, ctypes.POINTER(_Header)).contents
            # ProcessIdList entries are ULONG_PTR: pointer-sized on each arch.
            base = ctypes.addressof(buffer) + ctypes.sizeof(_Header)
            room = (capacity - ctypes.sizeof(_Header)) // ctypes.sizeof(ctypes.c_size_t)
            pid_list = (ctypes.c_size_t * room).from_address(base)
            return {int(pid_list[index]) for index in range(filled.returned)}
        finally:
            _kernel32.CloseHandle(handle)
    if kind == "posix_session":
        session = identity.get("id")
        if not isinstance(session, int) or session <= 0:
            raise UnknownContainment("the recorded session id is missing")
        members: set[int] = set()
        try:
            proc_entries: list[str] = os.listdir("/proc")
        except OSError as exc:
            raise UnknownContainment(f"/proc cannot be listed: {exc}") from exc
        for entry in proc_entries:
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", encoding="ascii") as handle:
                    stat = handle.read()
            except OSError as exc:
                raise UnknownContainment(f"/proc/{entry}/stat is unreadable: {exc}") from exc
            tail = stat.rsplit(")", 1)[1].split()
            # After the comm field: state(0) ppid(1) pgrp(2) session(3).
            if len(tail) > 3 and tail[3] == str(session):
                members.add(int(entry))
        return members
    raise UnknownContainment("the record carries no supported containment identity")


__all__ = [
    "UnknownContainment",
    "adopt_host_containment",
    "containment_members",
    "host_containment_id",
]
