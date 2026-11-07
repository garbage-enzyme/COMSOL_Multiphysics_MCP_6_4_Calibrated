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


def test_an_enclosing_job_that_forbids_breakaway_falls_back_inside_the_job(monkeypatch):
    monkeypatch.setattr(process_launch.os, "name", "nt")
    process_launch.enable_job_breakaway()
    factory = _Recorder(process_launch.ERROR_ACCESS_DENIED)
    process, outcome = process_launch.popen_independent_owner(factory, ["x"], creationflags=0x8)
    assert outcome == process_launch.BREAKAWAY_REFUSED
    assert factory.calls == [0x8 | process_launch.CREATE_BREAKAWAY_FROM_JOB, 0x8]
    assert process["creationflags"] == 0x8


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
