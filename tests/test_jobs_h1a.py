"""Process-only acceptance tests for the H1a durable job control plane."""

from __future__ import annotations

import json
import csv
import os
import shutil
import subprocess
import sys
import time
import types
import uuid
from pathlib import Path

import psutil
import pytest

from src.jobs.manager import JobManager, validate_staged_sweep_spec
from src.jobs.store import JobLock, JobStore, atomic_write_json, process_identity
from src.jobs import worker as production_worker


@pytest.fixture()
def jobs_root():
    root = Path("D:/comsol_runtime_test/jobs") / uuid.uuid4().hex
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def wait_for(manager: JobManager, job_id: str, statuses: set[str], timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = manager.status(job_id)
        if state["status"] in statuses:
            return state
        time.sleep(0.025)
    raise AssertionError(f"Job did not reach {statuses}: {manager.status(job_id)}")


def test_submit_returns_promptly_and_second_manager_observes_completion(jobs_root):
    first = JobManager(jobs_root, allow_test_jobs=True)
    started = time.monotonic()
    result = first.submit({"job_type": "test_sequence", "delays": [0.1, 0.1]})
    elapsed = time.monotonic() - started

    second = JobManager(jobs_root, allow_test_jobs=True)
    completed = wait_for(second, result["job_id"], {"completed"})

    assert elapsed < 1.0
    assert completed["progress"] == {"completed": 2, "total": 2}
    assert second.tail(result["job_id"], 2)["events"]


def test_detached_worker_survives_submitting_host_exit(jobs_root):
    script = (
        "import json; from src.jobs.manager import JobManager; "
        f"m=JobManager({str(jobs_root)!r}, allow_test_jobs=True); "
        "print(json.dumps(m.submit({'job_type':'test_sequence','delays':[0.2,0.2]})))"
    )
    completed_host = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    )
    job_id = json.loads(completed_host.stdout.strip().splitlines()[-1])["job_id"]

    completed = wait_for(JobManager(jobs_root, allow_test_jobs=True), job_id, {"completed"})

    assert completed["progress"] == {"completed": 2, "total": 2}


def test_killed_worker_is_reconciled_as_interrupted(jobs_root):
    manager = JobManager(jobs_root, allow_test_jobs=True)
    result = manager.submit({"job_type": "test_sequence", "delays": [0.05, 10.0]})
    running = wait_for(manager, result["job_id"], {"running"})
    worker = psutil.Process(running["worker_pid"])
    worker.terminate()
    worker.wait(timeout=5)

    interrupted = wait_for(JobManager(jobs_root, allow_test_jobs=True), result["job_id"], {"interrupted"})

    assert interrupted["last_error"]["type"] == "WorkerInterrupted"
    assert interrupted["progress"]["completed"] == 1


def test_killed_worker_resumes_durable_rows_without_duplication(jobs_root):
    manager = JobManager(jobs_root, allow_test_jobs=True)
    result = manager.submit({"job_type": "test_sequence", "delays": [0.05, 1.0, 0.05]})
    running = wait_for(manager, result["job_id"], {"running"})
    worker = psutil.Process(running["worker_pid"])
    worker.terminate()
    worker.wait(timeout=5)
    wait_for(manager, result["job_id"], {"interrupted"})

    resumed = manager.resume(result["job_id"])
    assert resumed["attempt"] == 2
    completed = wait_for(manager, result["job_id"], {"completed"}, timeout=15)
    with (manager.store.job_dir(result["job_id"]) / "results.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
        indices = [int(row["index"]) for row in csv.DictReader(handle)]

    assert completed["progress"] == {"completed": 3, "total": 3}
    assert indices == [0, 1, 2]


def test_cooperative_cancel_is_truthful_and_resumable(jobs_root):
    manager = JobManager(jobs_root, allow_test_jobs=True)
    result = manager.submit({"job_type": "test_sequence", "delays": [0.05, 0.3]})
    wait_for(manager, result["job_id"], {"running"})

    requested = manager.cancel(result["job_id"])
    interrupted = wait_for(manager, result["job_id"], {"interrupted"})

    assert requested["status"] == "cancel_requested"
    assert interrupted["last_error"]["type"] == "CooperativeCancel"
    manager.resume(result["job_id"])
    wait_for(manager, result["job_id"], {"completed"})


def test_completed_state_is_immutable(jobs_root):
    manager = JobManager(jobs_root, allow_test_jobs=True)
    result = manager.submit({"job_type": "test_sequence", "delays": [0.01]})
    wait_for(manager, result["job_id"], {"completed"})

    with pytest.raises(ValueError, match="Invalid job state transition"):
        manager.store.update_state(result["job_id"], "failed")
    with pytest.raises(ValueError, match="immutable"):
        manager.store.update_state(result["job_id"], patch={"last_error": {"message": "rewrite"}})


def test_lock_removes_only_proven_stale_identity(jobs_root):
    store = JobStore(jobs_root)
    job_id = store.create(
        {"schema_version": "1", "job_type": "test"},
        {"schema_version": "1", "status": "submitted"},
    )
    lock_path = store.job_dir(job_id) / ".state.lock"
    stale = process_identity(__import__("os").getpid())
    stale["process_create_time"] -= 1000
    atomic_write_json(lock_path, stale)

    with JobLock(lock_path, timeout=0.5):
        assert lock_path.exists()
    assert not lock_path.exists()


def test_tail_is_bounded(jobs_root):
    store = JobStore(jobs_root)
    job_id = store.create(
        {"schema_version": "1", "job_type": "test"},
        {"schema_version": "1", "status": "submitted"},
    )
    for index in range(10):
        store.append_event(job_id, "line", {"index": index})

    tail = store.tail(job_id, 3)

    assert len(tail["events"]) == 3
    assert json.loads(tail["events"][-1])["data"]["index"] == 9


def test_production_schema_is_solver_free_and_guards_source(jobs_root):
    source = jobs_root / "baseline.mph"
    source.write_bytes(b"model")
    spec = validate_staged_sweep_spec(
        {
            "job_type": "staged_sweep",
            "source_model_path": str(source),
            "parameter_name": "wl",
            "parameter_values": [4.25, 4.251],
            "expressions": ["ewfd.Rtotal", "ewfd.Ttotal", "ewfd.Atotal"],
            "smoke_points": 1,
        }
    )

    assert len(spec["spec_fingerprint"]) == 64
    assert len(spec["source_model_sha256"]) == 64
    with pytest.raises(ValueError, match="Unsupported staged_sweep fields"):
        validate_staged_sweep_spec(
            {
                "job_type": "staged_sweep",
                "source_model_path": str(source),
                "parameter_name": "wl",
                "parameter_values": [4.25],
                "expressions": ["A"],
                "checkpoint_model_path": str(source),
            }
        )


def test_test_jobs_require_explicit_injection(jobs_root):
    with pytest.raises(ValueError, match="disabled"):
        JobManager(jobs_root).submit({"job_type": "test_sequence", "delays": [0.01]})


def test_production_worker_bridges_smoke_broad_and_lease_with_mocks(jobs_root, monkeypatch):
    import src.tools.ownership as ownership_module
    import src.tools.workflow as workflow_module

    store = JobStore(jobs_root)
    identity = process_identity(os.getpid())
    spec = {
        "schema_version": "1",
        "spec_fingerprint": "mock-config",
        "job_type": "staged_sweep",
        "source_model_path": str(jobs_root / "source.mph"),
        "source_model_sha256": "0" * 64,
        "parameter_name": "wl",
        "parameter_values": [1.0, 2.0],
        "expressions": ["A"],
        "smoke_points": 1,
    }
    Path(spec["source_model_path"]).write_bytes(b"mock")
    state = {
        "schema_version": "1",
        "status": "submitted",
        "attempt": 1,
        "worker_pid": identity["pid"],
        "worker_process_create_time": identity["process_create_time"],
        "worker_command_signature": identity["command_signature"],
        "progress": {"completed": 0, "total": 2},
    }
    job_id = store.create(spec, state)
    lease_events = []

    class FakeOwnership:
        def __init__(self, *_args, **_kwargs):
            pass

        def preflight(self, **_kwargs):
            return {"ready": True}

        def acquire(self, **_kwargs):
            lease_events.append("acquired")
            return {"success": True}

        def heartbeat(self, **_kwargs):
            lease_events.append("heartbeat")
            return True

        def release(self):
            lease_events.append("released")
            return {"success": True}

    class FakeClient:
        def __init__(self, **_kwargs):
            self.disconnected = False

        def load(self, _path):
            return object()

        def disconnect(self):
            self.disconnected = True

    def fake_runner(_model, _parameter, values, _expressions, **kwargs):
        output = Path(kwargs["csv_path"])
        existing = []
        if output.exists() and output.stat().st_size:
            with output.open(newline="", encoding="utf-8") as handle:
                existing = list(csv.DictReader(handle))
        completed = {row["parameter_value"] for row in existing if row["status"] == "ok"}
        limit = kwargs.get("max_new_points")
        added = 0
        for value in values:
            token = str(value)
            if token in completed or (limit is not None and added >= limit):
                continue
            row = {
                "config_id": "mock-config",
                "parameter_value": token,
                "status": "ok",
            }
            with output.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(row))
                if handle.tell() == 0:
                    writer.writeheader()
                writer.writerow(row)
                handle.flush()
                os.fsync(handle.fileno())
            kwargs["on_durable_row"](row)
            added += 1
        return {"success": True, "stop_reason": None}

    monkeypatch.setattr(ownership_module, "SolverOwnership", FakeOwnership)
    monkeypatch.setattr(workflow_module, "run_staged_parametric_sweep", fake_runner)
    monkeypatch.setitem(sys.modules, "mph", types.SimpleNamespace(Client=FakeClient))

    code = production_worker.run(str(jobs_root), job_id)

    assert code == 0
    assert store.read_state(job_id)["status"] == "completed"
    assert store.read_state(job_id)["progress"] == {"completed": 2, "total": 2}
    assert lease_events[0] == "acquired"
    assert lease_events[-1] == "released"
