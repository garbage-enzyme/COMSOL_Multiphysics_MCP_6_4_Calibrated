import os
import subprocess
import sys
import time

import psutil

from src.jobs.process_control import capture_owned_descendants, terminate_exact, verify_absent
from src.jobs.store import process_identity


def test_exact_termination_refuses_a_reused_identity():
    identity = process_identity(os.getpid())
    identity["process_create_time"] -= 10

    result = terminate_exact(identity)

    assert result["acted"] is False
    assert result["reason"] == "identity_not_active"


def test_capture_and_terminate_only_owned_child_process():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
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
    child = f"import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', {grandchild!r}]); time.sleep(30)"
    root_script = f"import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(30)"
    root = subprocess.Popen([sys.executable, "-c", root_script])
    sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
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

        assert verify_absent([identity, *descendants])["absent"] is True
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
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        "print(child.pid, flush=True); time.sleep(.1)"
    )
    worker = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    child_pid = int(worker.stdout.readline().strip())
    worker.wait(timeout=5)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and psutil.pid_exists(child_pid):
        time.sleep(0.05)
    assert not psutil.pid_exists(child_pid)
