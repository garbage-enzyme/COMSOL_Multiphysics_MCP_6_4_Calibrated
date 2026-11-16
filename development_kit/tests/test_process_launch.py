"""Independent owners leave the shim's job object only when a shim worker asks."""

from __future__ import annotations

import pytest

from comsol_mcp import process_launch


class _Recorder:
    def __init__(self, *failures: int) -> None:
        self.calls: list[int] = []
        self.failures = list(failures)

    def __call__(self, command, *, creationflags, **kwargs):
        self.calls.append(creationflags)
        if self.failures:
            error = OSError("refused")
            error.winerror = self.failures.pop(0)  # type: ignore[attr-defined]
            raise error
        return {"command": command, "creationflags": creationflags, **kwargs}


@pytest.fixture(autouse=True)
def _reset_breakaway(monkeypatch):
    monkeypatch.setattr(process_launch, "_breakaway_enabled", False)


def test_breakaway_is_off_by_default_so_direct_hosts_are_unchanged():
    factory = _Recorder()
    process, outcome = process_launch.popen_independent_owner(factory, ["x"], creationflags=0x8)
    assert outcome == process_launch.BREAKAWAY_NOT_REQUESTED
    assert factory.calls == [0x8]
    assert process["creationflags"] == 0x8


def test_a_verified_shim_worker_requests_breakaway(monkeypatch):
    monkeypatch.setattr(process_launch.os, "name", "nt")
    process_launch.enable_job_breakaway()
    factory = _Recorder()
    _process, outcome = process_launch.popen_independent_owner(factory, ["x"], creationflags=0x8)
    assert outcome == process_launch.BREAKAWAY_REQUESTED
    assert factory.calls == [0x8 | process_launch.CREATE_BREAKAWAY_FROM_JOB]


def test_an_enclosing_job_that_forbids_breakaway_refuses_before_starting(monkeypatch):
    monkeypatch.setattr(process_launch.os, "name", "nt")
    process_launch.enable_job_breakaway()
    factory = _Recorder(process_launch.ERROR_ACCESS_DENIED)
    with pytest.raises(process_launch.JobBreakawayDenied) as error:
        process_launch.popen_independent_owner(factory, ["x"], creationflags=0x8)
    assert error.value.winerror == process_launch.ERROR_ACCESS_DENIED
    # Exactly one attempt, with the breakaway flag: nothing was started
    # inside the job, so no dependent lifetime was ever promised.
    assert factory.calls == [0x8 | process_launch.CREATE_BREAKAWAY_FROM_JOB]


def test_the_refusal_is_an_os_error_so_existing_launch_handlers_record_it(monkeypatch):
    monkeypatch.setattr(process_launch.os, "name", "nt")
    process_launch.enable_job_breakaway()
    factory = _Recorder(process_launch.ERROR_ACCESS_DENIED)
    with pytest.raises(OSError):
        process_launch.popen_independent_owner(factory, ["x"], creationflags=0x8)


def test_a_real_enclosing_job_that_denies_breakaway_refuses_the_launch():
    """Real Windows process control: no fallback launch inside the job."""
    import subprocess
    import sys

    pytest.importorskip("psutil")
    if sys.platform != "win32":
        pytest.skip("Windows job objects")
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]

    class ExtendedLimits(ctypes.Structure):
        # JOBOBJECT_EXTENDED_LIMIT_INFORMATION, the same layout the shim uses.
        _fields_ = [
            ("per_process_user_time", ctypes.c_int64),
            ("per_job_user_time", ctypes.c_int64),
            ("limit_flags", ctypes.c_uint32),
            ("minimum_working_set", ctypes.c_size_t),
            ("maximum_working_set", ctypes.c_size_t),
            ("active_process_limit", ctypes.c_uint32),
            ("affinity", ctypes.c_size_t),
            ("priority_class", ctypes.c_uint32),
            ("scheduling_class", ctypes.c_uint32),
            *[
                ("io_" + name, ctypes.c_uint64)
                for name in (
                    "read_ops",
                    "write_ops",
                    "other_ops",
                    "read_bytes",
                    "write_bytes",
                    "other_bytes",
                )
            ],
            ("process_memory_limit", ctypes.c_size_t),
            ("job_memory_limit", ctypes.c_size_t),
            ("peak_process_memory", ctypes.c_size_t),
            ("peak_job_memory", ctypes.c_size_t),
        ]

    job = kernel32.CreateJobObjectW(None, None)
    assert job
    # No BREAKAWAY_OK (0x0800) and no KILL_ON_JOB_CLOSE: breakaway denial
    # alone must refuse; closing the handle must not kill this test process.
    limits = ExtendedLimits(limit_flags=0)
    assert ctypes.sizeof(limits) == 144
    assert kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
    # Put this test process inside the denying job, then attempt the launch.
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    assert kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess())
    try:
        process_launch.enable_job_breakaway()
        with pytest.raises(process_launch.JobBreakawayDenied):
            process_launch.popen_independent_owner(
                subprocess.Popen,
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=0x0,
            )
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(job))


def test_other_launch_failures_are_not_retried(monkeypatch):
    monkeypatch.setattr(process_launch.os, "name", "nt")
    process_launch.enable_job_breakaway()
    factory = _Recorder(2)
    with pytest.raises(OSError):
        process_launch.popen_independent_owner(factory, ["x"], creationflags=0x8)
    assert len(factory.calls) == 1


def test_posix_never_enables_breakaway(monkeypatch):
    monkeypatch.setattr(process_launch.os, "name", "posix")
    process_launch.enable_job_breakaway()
    assert process_launch.job_breakaway_enabled() is False
