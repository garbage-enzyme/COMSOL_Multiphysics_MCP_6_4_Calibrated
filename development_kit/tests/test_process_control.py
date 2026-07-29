import os
import hashlib
import subprocess
import sys
import time

import psutil
import pytest

import src.jobs.manager as manager_module
import src.jobs.process_control as process_control_module
from src.jobs.process_control import (
    capture_owned_descendants,
    owned_solver_identities_from_lease,
    terminate_exact,
    verify_absent,
)
from src.jobs.store import process_identity

_HIDDEN_PROCESS_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _wait_absent(identities, timeout=5.0):
    deadline = time.monotonic() + timeout
    verification = verify_absent(identities)
    while not verification["absent"] and time.monotonic() < deadline:
        if any(item["state"] == "uncertain" for item in verification["verdicts"]):
            break
        time.sleep(0.025)
        verification = verify_absent(identities)
    return verification


def test_detached_process_tracker_reaps_completed_child_without_wait():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.1)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_HIDDEN_PROCESS_FLAGS,
    )
    manager_module._track_detached_process(process)

    deadline = time.monotonic() + 5.0
    while process.returncode is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert process.returncode == 0


def test_exact_termination_refuses_a_reused_identity():
    identity = process_identity(os.getpid())
    identity["process_create_time"] -= 10

    result = terminate_exact(identity)

    assert result["acted"] is False
    assert result["reason"] == "identity_not_active"


def test_capture_and_terminate_only_owned_child_process():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        creationflags=_HIDDEN_PROCESS_FLAGS,
    )
    try:
        identity = process_identity(child.pid)
        captured = capture_owned_descendants(identity)
        assert captured["worker"]["state"] == "active"

        terminated = terminate_exact(identity)
        assert terminated["acted"] is True
        child.wait(timeout=5)
        verified = verify_absent([identity])
        assert verified["absent"] is True
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_owned_tree_capture_excludes_unrelated_sentinel():
    grandchild = "import time; time.sleep(30)"
    child = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}], "
        "creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)); time.sleep(30)"
    )
    root_script = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}], "
        "creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)); time.sleep(30)"
    )
    root = subprocess.Popen(
        [sys.executable, "-c", root_script], creationflags=_HIDDEN_PROCESS_FLAGS
    )
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        creationflags=_HIDDEN_PROCESS_FLAGS,
    )
    try:
        identity = process_identity(root.pid)
        deadline = time.monotonic() + 5
        captured = capture_owned_descendants(identity)
        while len(captured["descendants"]) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
            captured = capture_owned_descendants(identity)
        assert len(captured["descendants"]) >= 2
        descendants = captured["descendants"]

        assert terminate_exact(identity)["acted"] is True
        for descendant in descendants:
            terminate_exact(descendant, force=True)
        root.wait(timeout=5)

        assert _wait_absent([identity, *descendants])["absent"] is True
        assert sentinel.poll() is None
    finally:
        for process in (root, sentinel):
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_worker_job_object_kills_inherited_child_on_worker_exit_windows_only():
    if os.name != "nt":
        return
    script = (
        "import subprocess,sys,time; "
        "from src.jobs.process_control import contain_current_process_tree; "
        "assert contain_current_process_tree(); "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'], "
        "creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)); "
        "print(child.pid, flush=True); time.sleep(.1)"
    )
    worker = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        text=True,
        creationflags=_HIDDEN_PROCESS_FLAGS,
    )
    stdout, _stderr = worker.communicate(timeout=5)
    child_pid = int(stdout.strip())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and psutil.pid_exists(child_pid):
        time.sleep(0.05)
    assert not psutil.pid_exists(child_pid)


def test_attached_server_is_never_returned_as_an_owned_termination_identity():
    attached = {
        "owned": False,
        "server_pid": 900,
        "process_create_time": 900.0,
        "command_signature": "a" * 64,
    }
    lease = {
        "attached_server": attached,
        "comsol_server_processes": [],
    }

    assert owned_solver_identities_from_lease(lease) == []

    contaminated = {
        **lease,
        "comsol_server_processes": [
            {
                "pid": 900,
                "process_create_time": 900.0,
                "command_signature": "a" * 64,
            }
        ],
    }
    try:
        owned_solver_identities_from_lease(contaminated)
    except ValueError as exc:
        assert "non-owned attached server" in str(exc)
    else:
        raise AssertionError("attached server entered owned termination identities")


def test_descendant_exit_during_capture_preserves_other_exact_identities(monkeypatch):
    worker_identity = {
        "pid": 44000,
        "process_create_time": 44000.0,
        "command_signature": "a" * 64,
    }

    class Child:
        def __init__(self, pid):
            self.pid = pid

    class Worker:
        def children(self, recursive):
            assert recursive is True
            return [Child(44001), Child(44002), Child(44003)]

    monkeypatch.setattr(
        process_control_module,
        "inspect_identity",
        lambda identity: {"identity": identity, "state": "active", "reason": "exact"},
    )
    monkeypatch.setattr(process_control_module.psutil, "Process", lambda _pid: Worker())

    def identity_for(pid):
        if pid == 44002:
            raise psutil.NoSuchProcess(pid)
        return {
            "pid": pid,
            "process_create_time": float(pid),
            "command_signature": f"{pid:064x}",
        }

    monkeypatch.setattr(process_control_module, "process_identity", identity_for)

    captured = capture_owned_descendants(worker_identity)

    assert captured["capture_complete"] is True
    assert [item["pid"] for item in captured["descendants"]] == [44001, 44003]


def test_exact_termination_validates_and_acts_through_one_process_object(monkeypatch):
    command = ["python", "worker.py"]
    identity = {
        "pid": 44100,
        "process_create_time": 44100.0,
        "command_signature": hashlib.sha256("\0".join(command).encode()).hexdigest(),
    }
    constructions = []
    actions = []

    class OneShot:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return None

    class Process:
        pid = identity["pid"]

        def oneshot(self):
            return OneShot()

        def create_time(self):
            return identity["process_create_time"]

        def cmdline(self):
            return command

        def terminate(self):
            actions.append("terminate")

        def kill(self):
            actions.append("kill")

    def construct(pid):
        constructions.append(pid)
        return Process()

    monkeypatch.setattr(process_control_module.psutil, "Process", construct)

    result = terminate_exact(identity)

    assert result["acted"] is True
    assert constructions == [identity["pid"]]
    assert actions == ["terminate"]


@pytest.mark.parametrize("member", [None, "server", 7, []])
def test_non_mapping_solver_lease_member_is_a_controlled_value_error(member):
    with pytest.raises(ValueError, match="must be an object"):
        process_control_module.owned_solver_identities_from_lease(
            {"comsol_server_processes": [member]}
        )
