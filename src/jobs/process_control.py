"""Exact-identity process inspection used by the H2 cancellation coordinator."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from typing import Any

import psutil

from .store import process_identity, process_identity_state


class OwnedJobObject:
    """Windows kill-on-close containment for one process tree we launched.

    The handle deliberately remains owned by the MCP control process.  Closing it
    (including normal process teardown) kills only processes explicitly assigned
    to this object; it never searches for processes by name or command line.
    """

    def __init__(self, handle: int, pid: int):
        self._handle = handle
        self.pid = int(pid)

    @classmethod
    def assign(cls, pid: int) -> "OwnedJobObject | None":
        """Create a kill-on-close Job Object and assign an exact child PID.

        Returns ``None`` on non-Windows platforms or when Windows refuses a safe
        assignment (for example a nested/restricted job).  Callers retain the
        existing exact-identity fallback in that case.
        """
        if os.name != "nt":
            return None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD)
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        class _LargeInteger(ctypes.Structure):
            _fields_ = [("QuadPart", ctypes.c_longlong)]

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", _LargeInteger),
                ("PerJobUserTimeLimit", _LargeInteger),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        process = None
        assigned = False
        try:
            # PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION
            process = kernel32.OpenProcess(0x0100 | 0x0001 | 0x1000, False, int(pid))
            if not process:
                return None
            limits = _ExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                return None
            if not kernel32.AssignProcessToJobObject(job, process):
                return None
            assigned = True
            return cls(int(job), pid)
        finally:
            if process:
                kernel32.CloseHandle(process)
            if not assigned:
                kernel32.CloseHandle(job)

    def close(self) -> None:
        if self._handle and os.name == "nt":
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(wintypes.HANDLE(self._handle))
            self._handle = 0


_CURRENT_PROCESS_JOB: OwnedJobObject | None = None


def contain_current_process_tree() -> bool:
    """Keep kill-on-close containment in the detached worker itself.

    The MCP host never owns this handle, so a host restart cannot kill a durable
    worker.  Worker exit instead closes the handle and cleans only descendants
    explicitly inherited by the job object.
    """
    global _CURRENT_PROCESS_JOB
    if _CURRENT_PROCESS_JOB is not None:
        return True
    contained = OwnedJobObject.assign(os.getpid())
    if contained is None:
        return False
    _CURRENT_PROCESS_JOB = contained
    return True


def inspect_identity(identity: dict[str, Any]) -> dict[str, Any]:
    """Return an exact identity verdict without acting on a process."""
    state, reason = process_identity_state(identity)
    return {"identity": identity, "state": state, "reason": reason}


def capture_owned_descendants(worker_identity: dict[str, Any]) -> dict[str, Any]:
    """Capture only descendants of a worker whose full identity still matches."""
    verdict = inspect_identity(worker_identity)
    if verdict["state"] != "active":
        return {"worker": verdict, "descendants": []}
    try:
        worker = psutil.Process(int(worker_identity["pid"]))
        descendants = [process_identity(item.pid) for item in worker.children(recursive=True)]
    except psutil.NoSuchProcess as exc:
        # The worker can exit between the exact identity check and children().
        # Re-inspect so callers can distinguish proven exit from inspection
        # uncertainty. Descendant cleanup still requires separate containment
        # evidence; an empty list alone is not proof.
        after = inspect_identity(worker_identity)
        return {
            "worker": after,
            "descendants": [],
            "capture_complete": False,
            "reason": f"worker exited during descendant capture: {exc}",
        }
    except (psutil.AccessDenied, psutil.ZombieProcess, OSError) as exc:
        return {
            "worker": {**verdict, "state": "uncertain", "reason": f"cannot inspect descendants: {exc}"},
            "descendants": [],
            "capture_complete": False,
        }
    return {"worker": verdict, "descendants": descendants, "capture_complete": True}


def terminate_exact(identity: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    """Terminate one process only after immediate full-identity revalidation."""
    before = inspect_identity(identity)
    if before["state"] != "active":
        return {"acted": False, "before": before, "reason": "identity_not_active"}
    try:
        process = psutil.Process(int(identity["pid"]))
        if force:
            process.kill()
            action = "kill"
        else:
            process.terminate()
            action = "terminate"
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError) as exc:
        return {
            "acted": False,
            "before": before,
            "reason": f"process_action_failed: {type(exc).__name__}: {exc}",
        }
    return {"acted": True, "action": action, "before": before}


def verify_absent(identities: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify every captured identity is stale; uncertainty is never absence."""
    verdicts = [inspect_identity(identity) for identity in identities]
    return {
        "absent": all(item["state"] == "stale" for item in verdicts),
        "verdicts": verdicts,
    }
