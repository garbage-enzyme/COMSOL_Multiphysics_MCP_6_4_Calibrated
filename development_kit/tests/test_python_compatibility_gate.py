"""Solver-free tests for the python compatibility licensed-gate identity guard."""

from __future__ import annotations

import contextlib

import psutil
import pytest

from development_kit.scripts import python_compatibility_licensed_gate as gate


class _FakeProcess:
    pid = 7

    def __init__(self, error=None):
        self._error = error

    def oneshot(self):
        return contextlib.nullcontext()

    def cmdline(self):
        if self._error is not None:
            raise self._error
        return ["python", "-m", "worker"]

    def exe(self):
        return r"C:\python\python.exe"

    def ppid(self):
        return 1

    def name(self):
        return "python.exe"

    def create_time(self):
        return 1000.0


def _patch_process(monkeypatch, instance):
    monkeypatch.setattr(gate.psutil, "Process", lambda _pid: instance)


def test_attach_worker_identity_records_full_identity(monkeypatch):
    _patch_process(monkeypatch, _FakeProcess())
    receipt: dict = {}

    gate._attach_worker_identity(receipt, _FakeProcess())

    identity = receipt["worker_process"]
    assert identity["pid"] == 7
    assert identity["command_line"] == ["python", "-m", "worker"]
    assert "worker_identity_incomplete" not in receipt


def test_attach_worker_identity_tolerates_raced_worker(monkeypatch):
    _patch_process(monkeypatch, _FakeProcess(error=psutil.NoSuchProcess(7)))
    receipt: dict = {}

    gate._attach_worker_identity(receipt, _FakeProcess())

    assert receipt["worker_process"] is None
    assert receipt["worker_identity_incomplete"] is True


@pytest.mark.parametrize("error", [psutil.AccessDenied(7), psutil.ZombieProcess(7)])
def test_attach_worker_identity_covers_denied_and_zombie(monkeypatch, error):
    class _CmdlineFails(_FakeProcess):
        def cmdline(self):
            raise error

    _patch_process(monkeypatch, _CmdlineFails())
    receipt: dict = {}

    gate._attach_worker_identity(receipt, _FakeProcess())

    assert receipt["worker_process"] is None
    assert receipt["worker_identity_incomplete"] is True
