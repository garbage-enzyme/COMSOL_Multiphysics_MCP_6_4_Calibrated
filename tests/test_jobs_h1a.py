"""Process-only acceptance tests for the H1a durable job control plane."""

from __future__ import annotations

import json
import csv
from concurrent.futures import ThreadPoolExecutor
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
from src.jobs.store import JobLock, JobStore, atomic_write_json, process_identity, process_identity_state
import src.jobs.store as store_module
from src.jobs import worker as production_worker
from src.jobs import cancel_worker


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
    raise AssertionError(
        f"Job did not reach {statuses}: {manager.status(job_id)}; "
        f"tail={manager.tail(job_id, 50)}"
    )


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
    terminal = wait_for(manager, result["job_id"], {"cancelled"})

    assert requested["status"] == "cancel_requested"
    assert terminal["cancel"]["verification"]["absent"] is True
    assert terminal["cancel"]["cooperative_observation"]["target_attempt"] == 1
    timestamps = terminal["cancel"]["phase_timestamps"]
    assert set(timestamps) >= {"requested", "native_grace", "verifying", "verified", "terminal_commit"}
    assert timestamps["requested"] <= timestamps["native_grace"] <= timestamps["terminal_commit"]
    assert terminal["cancel"]["timing_policy"] == {
        "native_grace_budget_s": 10.0,
        "terminate_budget_s": 5.0,
    }
    assert terminal["cancel"]["teardown_latency"]["requested_to_terminal_s"] >= 0
    assert terminal["cancel"]["teardown_latency"]["coordinator_to_terminal_s"] >= 0
    assert terminal["cancel"]["teardown_latency"]["verification_to_terminal_s"] >= 0
    manager.resume(result["job_id"])
    wait_for(manager, result["job_id"], {"completed"})


def test_repeated_and_concurrent_cancel_calls_share_one_attempt_bound_request(jobs_root):
    manager = JobManager(jobs_root, allow_test_jobs=True)
    result = manager.submit({"job_type": "test_sequence", "delays": [0.05, 1.0]})
    wait_for(manager, result["job_id"], {"running"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _value: manager.cancel(result["job_id"]), range(2)))

    assert first["success"] is True
    assert second["success"] is True
    assert first["request_id"] == second["request_id"]
    assert {first["idempotent"], second["idempotent"]} == {False, True}
    control = manager.store.read_control(result["job_id"])
    assert control["target_attempt"] == 1
    assert control["target_worker"]["pid"] is not None
    assert manager.store.read_state(result["job_id"])["cancel"]["request_id"] == first["request_id"]
    wait_for(manager, result["job_id"], {"cancelled"})


def test_completed_before_cancel_acquires_lock_has_no_control_side_effect(jobs_root):
    manager = JobManager(jobs_root, allow_test_jobs=True)
    result = manager.submit({"job_type": "test_sequence", "delays": [0.01]})
    wait_for(manager, result["job_id"], {"completed"})

    cancelled = manager.cancel(result["job_id"])

    assert cancelled["success"] is False
    assert manager.store.read_control(result["job_id"])["request"] is None
    assert manager.store.read_state(result["job_id"])["status"] == "completed"


def test_cancel_requested_state_cannot_be_overwritten_by_completed(jobs_root):
    manager = JobManager(jobs_root, allow_test_jobs=True)
    result = manager.submit({"job_type": "test_sequence", "delays": [0.05, 1.0]})
    wait_for(manager, result["job_id"], {"running"})
    manager.cancel(result["job_id"])

    with pytest.raises(ValueError, match="Invalid job state transition"):
        manager.store.update_state(result["job_id"], "completed")

    wait_for(manager, result["job_id"], {"cancelled"})


def test_stale_attempt_control_is_ignored_after_resume(jobs_root):
    manager = JobManager(jobs_root, allow_test_jobs=True)
    result = manager.submit({"job_type": "test_sequence", "delays": [0.05, 0.4]})
    wait_for(manager, result["job_id"], {"running"})
    first = manager.cancel(result["job_id"])
    wait_for(manager, result["job_id"], {"cancelled"})

    resumed = manager.resume(result["job_id"])
    manager.store.write_control(
        result["job_id"],
        "cancel_requested",
        fields={"request_id": first["request_id"], "target_attempt": 1},
    )
    completed = wait_for(manager, result["job_id"], {"completed"}, timeout=10)

    assert resumed["attempt"] == 2
    assert completed["progress"] == {"completed": 2, "total": 2}


def test_cancel_coordinator_refuses_stale_attempt_request(jobs_root):
    store = JobStore(jobs_root)
    identity = process_identity(os.getpid())
    job_id = store.create(
        {"schema_version": "2", "job_type": "test"},
        {
            "schema_version": "2",
            "status": "cancel_requested",
            "attempt": 2,
            "cancel": {
                "request_id": "cancel-attempt-1",
                "target_attempt": 1,
                "phase": "requested",
            },
        },
    )
    store.write_control(
        job_id,
        "cancel_requested",
        fields={"request_id": "cancel-attempt-1", "target_attempt": 1},
    )

    claimed = cancel_worker._claim(
        store,
        job_id,
        "cancel-attempt-1",
        identity,
        grace_seconds=1.0,
        terminate_seconds=1.0,
    )

    assert claimed is None
    assert store.read_state(job_id)["status"] == "cancel_requested"


def test_legacy_h1_cancel_control_migrates_to_an_idempotent_request(jobs_root):
    store = JobStore(jobs_root)
    identity = process_identity(os.getpid())
    job_id = store.create(
        {"schema_version": "1", "job_type": "test"},
        {
            "schema_version": "1",
            "status": "running",
            "attempt": 1,
            "worker_pid": identity["pid"],
            "worker_process_create_time": identity["process_create_time"],
            "worker_command_signature": identity["command_signature"],
        },
    )
    store.write_control(job_id, "cancel_requested")

    migrated = store.request_cancel(job_id, requester_identity=identity)

    assert migrated["accepted"] is True
    assert migrated["idempotent"] is True
    assert migrated["control"]["target_attempt"] == 1
    assert migrated["control"]["request_id"].startswith("cancel-")


def test_unknown_control_request_fails_closed(jobs_root):
    store = JobStore(jobs_root)
    job_id = store.create(
        {"schema_version": "1", "job_type": "test"},
        {"schema_version": "1", "status": "running", "attempt": 1},
    )
    store.write_control(job_id, "unexpected")

    refused = store.request_cancel(job_id, requester_identity=process_identity(os.getpid()))

    assert refused["accepted"] is False
    assert refused["reason"] == "unknown_control_request"
    assert store.read_state(job_id)["status"] == "running"


def test_cooperative_cancel_observation_is_attempt_bound_and_nonterminal(jobs_root):
    store = JobStore(jobs_root)
    identity = process_identity(os.getpid())
    job_id = store.create(
        {"schema_version": "2", "job_type": "test"},
        {
            "schema_version": "2",
            "status": "running",
            "attempt": 1,
            "worker_pid": identity["pid"],
            "worker_process_create_time": identity["process_create_time"],
            "worker_command_signature": identity["command_signature"],
        },
    )
    request = store.request_cancel(job_id, requester_identity=identity)

    observed = store.record_cooperative_cancel_observed(
        job_id,
        attempt=1,
        message="Stopped between points",
    )
    repeated = store.record_cooperative_cancel_observed(
        job_id,
        attempt=1,
        message="A repeated observation must not replace the first",
    )
    state = store.read_state(job_id)

    assert request["accepted"] is True
    assert observed["recorded"] is True
    assert observed["idempotent"] is False
    assert repeated["recorded"] is True
    assert repeated["idempotent"] is True
    assert state["status"] == "cancel_requested"
    assert state["cancel"]["phase"] == "requested"
    assert state["cancel"]["cooperative_observation"] == {
        "request_id": request["control"]["request_id"],
        "target_attempt": 1,
        "observed_at_epoch": state["cancel"]["cooperative_observation"]["observed_at_epoch"],
        "message": "Stopped between points",
        "worker": identity,
    }


def test_stale_attempt_cannot_record_cooperative_cancel_observation(jobs_root):
    store = JobStore(jobs_root)
    identity = process_identity(os.getpid())
    job_id = store.create(
        {"schema_version": "2", "job_type": "test"},
        {
            "schema_version": "2",
            "status": "cancel_requested",
            "attempt": 2,
            "worker_pid": identity["pid"],
            "worker_process_create_time": identity["process_create_time"],
            "worker_command_signature": identity["command_signature"],
            "cancel": {"request_id": "cancel-old", "target_attempt": 1, "phase": "requested"},
        },
    )
    store.write_control(
        job_id,
        "cancel_requested",
        fields={"request_id": "cancel-old", "target_attempt": 1},
    )

    observed = store.record_cooperative_cancel_observed(
        job_id,
        attempt=2,
        message="Must be ignored",
    )

    assert observed == {
        "recorded": False,
        "reason": "no_matching_cancel_request",
        "state": observed["state"],
    }
    assert "cooperative_observation" not in store.read_state(job_id)["cancel"]


def test_status_preserves_matching_cancel_when_worker_identity_is_stale(jobs_root):
    manager = JobManager(jobs_root, allow_test_jobs=True, reconcile_on_start=False)
    identity = process_identity(os.getpid())
    job_id = manager.store.create(
        {"schema_version": "2", "job_type": "test"},
        {
            "schema_version": "2",
            "status": "running",
            "attempt": 1,
            "worker_pid": identity["pid"],
            "worker_process_create_time": identity["process_create_time"] - 1000,
            "worker_command_signature": identity["command_signature"],
        },
    )
    manager.store.request_cancel(job_id, requester_identity=identity)

    observed = manager.status(job_id)

    assert observed["status"] == "cancel_requested"
    assert observed["worker_process_state"] == "stale"
    assert manager.store.read_state(job_id)["status"] == "cancel_requested"


def test_native_cancel_evidence_merges_without_overwriting_coordinator(jobs_root):
    store = JobStore(jobs_root)
    identity = process_identity(os.getpid())
    job_id = store.create(
        {"schema_version": "2", "job_type": "test"},
        {
            "schema_version": "2",
            "status": "running",
            "attempt": 1,
            "worker_pid": identity["pid"],
            "worker_process_create_time": identity["process_create_time"],
            "worker_command_signature": identity["command_signature"],
        },
    )
    store.request_cancel(job_id, requester_identity=identity)
    coordinator = {"pid": 123, "process_create_time": 456.0, "command_signature": "coordinator"}
    store.update_state(
        job_id,
        "cancelling",
        patch={"cancel": {**store.read_state(job_id)["cancel"], "coordinator": coordinator}},
    )

    recorded = production_worker._record_native_cancel(
        store,
        job_id,
        1,
        {"attempted": True, "supported": True, "outcome": "returned"},
    )
    state = store.read_state(job_id)

    assert recorded is True
    assert state["cancel"]["coordinator"] == coordinator
    assert state["cancel"]["native"] == {
        "attempted": True,
        "supported": True,
        "outcome": "returned",
    }


def test_detached_coordinator_force_stops_exact_test_worker_and_allows_resume(jobs_root):
    manager = JobManager(
        jobs_root,
        allow_test_jobs=True,
        cancel_grace_seconds=0.1,
        cancel_terminate_seconds=0.1,
    )
    result = manager.submit({"job_type": "test_sequence", "delays": [0.05, 0.4]})
    running = wait_for(manager, result["job_id"], {"running"})
    worker_identity = {
        "pid": running["worker_pid"],
        "process_create_time": running["worker_process_create_time"],
        "command_signature": running["worker_command_signature"],
    }

    requested = manager.cancel(result["job_id"])
    cancelled = wait_for(manager, result["job_id"], {"cancelled"}, timeout=10)

    assert requested["request_id"] == cancelled["cancel"]["request_id"]
    assert process_identity_state(worker_identity)[0] == "stale"
    assert cancelled["cancel"]["verification"]["absent"] is True
    resumed = manager.resume(result["job_id"])
    assert resumed["attempt"] == 2
    wait_for(manager, result["job_id"], {"completed"}, timeout=10)


def test_startup_reconciliation_relaunches_only_existing_stale_cancel_request(jobs_root, monkeypatch):
    manager = JobManager(jobs_root, allow_test_jobs=True)
    job_id = manager.store.create(
        {"schema_version": "2", "job_type": "test"},
        {"schema_version": "2", "status": "cancelling", "attempt": 1},
    )
    manager.store.write_control(
        job_id,
        "cancel_requested",
        fields={"request_id": "cancel-existing", "target_attempt": 1},
    )
    calls = []
    monkeypatch.setattr(manager, "_launch_cancel_coordinator", lambda jid, rid: calls.append((jid, rid)))

    assert manager.reconcile_cancellations() == 1
    assert calls == [(job_id, "cancel-existing")]


def test_orphan_reconciliation_commits_only_from_complete_cleanup_proof(jobs_root):
    manager = JobManager(jobs_root, allow_test_jobs=True, reconcile_on_start=False)
    identity = process_identity(os.getpid())
    stale_worker = {**identity, "process_create_time": identity["process_create_time"] - 1000}
    stale_coordinator = {**identity, "process_create_time": identity["process_create_time"] - 2000}
    job_id = manager.store.create(
        {"schema_version": "2", "job_type": "test"},
        {
            "schema_version": "2",
            "status": "cancelling",
            "attempt": 1,
            "worker_pid": stale_worker["pid"],
            "worker_process_create_time": stale_worker["process_create_time"],
            "worker_command_signature": stale_worker["command_signature"],
            "cancel": {
                "request_id": "cancel-orphan",
                "target_attempt": 1,
                "phase": "verifying",
                "phase_timestamps": {"requested": time.time() - 1, "verifying": time.time()},
                "coordinator": stale_coordinator,
                "descendants": [],
                "descendant_capture": {
                    "worker": {"identity": identity, "state": "active", "reason": "captured while active"},
                    "descendants": [],
                    "captured_at_epoch": time.time() - 0.5,
                },
            },
        },
    )
    manager.store.write_control(
        job_id,
        "cancel_requested",
        fields={
            "request_id": "cancel-orphan",
            "target_attempt": 1,
            "target_worker": stale_worker,
        },
    )

    assert manager.reconcile_cancellations() == 1
    terminal = manager.store.read_state(job_id)

    assert terminal["status"] == "cancelled"
    assert terminal["cancel"]["verification"]["absent"] is True
    assert len(terminal["cancel"]["verification"]["verdicts"]) == 2


def test_orphan_reconciliation_fails_closed_without_descendant_capture(jobs_root, monkeypatch):
    manager = JobManager(jobs_root, allow_test_jobs=True, reconcile_on_start=False)
    identity = process_identity(os.getpid())
    stale = {**identity, "process_create_time": identity["process_create_time"] - 1000}
    job_id = manager.store.create(
        {"schema_version": "2", "job_type": "test"},
        {
            "schema_version": "2",
            "status": "cancelling",
            "attempt": 1,
            "cancel": {
                "request_id": "cancel-missing-capture",
                "target_attempt": 1,
                "phase": "verifying",
                "coordinator": stale,
            },
        },
    )
    manager.store.write_control(
        job_id,
        "cancel_requested",
        fields={
            "request_id": "cancel-missing-capture",
            "target_attempt": 1,
            "target_worker": stale,
        },
    )
    launches = []
    monkeypatch.setattr(manager, "_launch_cancel_coordinator", lambda *args: launches.append(args))

    assert manager.reconcile_cancellations() == 0
    blocked = manager.store.read_state(job_id)

    assert blocked["status"] == "cancelling"
    assert blocked["cancel"]["reconciliation"]["outcome"] == "blocked_missing_descendant_capture"
    assert launches == []


def test_orphan_reconciliation_fails_closed_on_uncertain_identity(jobs_root, monkeypatch):
    manager = JobManager(jobs_root, allow_test_jobs=True, reconcile_on_start=False)
    identity = process_identity(os.getpid())
    job_id = manager.store.create(
        {"schema_version": "2", "job_type": "test"},
        {
            "schema_version": "2",
            "status": "cancelling",
            "attempt": 1,
            "cancel": {
                "request_id": "cancel-uncertain",
                "target_attempt": 1,
                "phase": "verifying",
                "coordinator": identity,
                "descendants": [],
                "descendant_capture": {
                    "worker": {"identity": identity, "state": "active", "reason": "captured while active"},
                    "descendants": [],
                },
            },
        },
    )
    manager.store.write_control(
        job_id,
        "cancel_requested",
        fields={
            "request_id": "cancel-uncertain",
            "target_attempt": 1,
            "target_worker": identity,
        },
    )
    monkeypatch.setattr(
        "src.jobs.manager.inspect_identity",
        lambda value: {"identity": value, "state": "uncertain", "reason": "access denied"},
    )
    launches = []
    monkeypatch.setattr(manager, "_launch_cancel_coordinator", lambda *args: launches.append(args))

    assert manager.reconcile_cancellations() == 0
    blocked = manager.store.read_state(job_id)

    assert blocked["status"] == "cancelling"
    assert blocked["cancel"]["reconciliation"]["outcome"] == "blocked_uncertain_identity"
    assert launches == []


@pytest.mark.parametrize("crash_phase", ["native_grace", "terminate", "force_kill", "verifying"])
def test_coordinator_loss_at_each_durable_phase_reconciles_safely(jobs_root, monkeypatch, crash_phase):
    manager = JobManager(
        jobs_root,
        allow_test_jobs=True,
        cancel_grace_seconds=0.02,
        cancel_terminate_seconds=0.05,
    )
    result = manager.submit({"job_type": "test_sequence", "delays": [0.01, 10.0]})
    running = wait_for(manager, result["job_id"], {"running"})
    worker_identity = {
        "pid": running["worker_pid"],
        "process_create_time": running["worker_process_create_time"],
        "command_signature": running["worker_command_signature"],
    }

    normal_launcher = manager._launch_cancel_coordinator
    monkeypatch.setattr(manager, "_launch_cancel_coordinator", lambda *_args: None)
    requested = manager.cancel(result["job_id"])
    monkeypatch.setattr(manager, "_launch_cancel_coordinator", normal_launcher)

    crash_script = (
        "import os; import src.jobs.cancel_worker as c; "
        f"phase={crash_phase!r}; real=c.terminate_exact; "
        "c.terminate_exact=(lambda identity,force=False: real(identity,force=force) if force else "
        "{'acted':False,'reason':'test_noop_terminate'}) if phase=='force_kill' else real; "
        "hook=lambda current: os._exit(91) if current==phase else None; "
        f"c.run({str(jobs_root)!r},{result['job_id']!r},{requested['request_id']!r},0.02,0.05,phase_hook=hook)"
    )
    crashed = subprocess.Popen([sys.executable, "-c", crash_script], cwd=Path(__file__).resolve().parents[1])
    assert crashed.wait(timeout=5) == 91
    phase_state = manager.store.read_state(result["job_id"])
    assert phase_state["status"] == "cancelling"
    assert phase_state["cancel"]["phase"] == crash_phase
    entered_at = phase_state["cancel"]["phase_timestamps"][crash_phase]
    assert process_identity_state(phase_state["cancel"]["coordinator"])[0] == "stale"

    assert manager.reconcile_cancellations() == 1
    cancelled = wait_for(manager, result["job_id"], {"cancelled"}, timeout=10)

    assert process_identity_state(worker_identity)[0] == "stale"
    assert cancelled["cancel"]["verification"]["absent"] is True
    assert cancelled["cancel"]["phase_timestamps"][crash_phase] == entered_at
    events = "\n".join(manager.tail(result["job_id"], 100)["events"])
    assert f'"phase": "{crash_phase}"' in events


def test_read_only_manager_construction_skips_startup_reconciliation(jobs_root, monkeypatch):
    def unexpected_reconciliation(_self, **_kwargs):
        raise AssertionError("read-only manager must not reconcile")

    monkeypatch.setattr(JobManager, "reconcile_cancellations", unexpected_reconciliation)
    JobManager(jobs_root, allow_test_jobs=True, reconcile_on_start=False)


def test_thirty_cancel_status_polling_races_have_no_false_terminal_state(jobs_root):
    iterations = int(os.environ.get("COMSOL_E4R_SOAK_ITERATIONS", "30"))
    if iterations < 1 or iterations > 100:
        raise ValueError("COMSOL_E4R_SOAK_ITERATIONS must be between 1 and 100")
    manager = JobManager(
        jobs_root,
        allow_test_jobs=True,
        cancel_grace_seconds=0.02,
        cancel_terminate_seconds=0.05,
    )
    calibration_count = min(5, iterations)
    deadline_s = 5.0
    latencies: list[float] = []
    records: list[dict[str, object]] = []
    summary_path = jobs_root / "e4r_cancellation_soak_summary.json"
    active_job_id = None
    try:
        for index in range(iterations):
            result = manager.submit({"job_type": "test_sequence", "delays": [0.01, 0.25]})
            active_job_id = result["job_id"]
            wait_for(manager, active_job_id, {"running"})
            requested_at = time.monotonic()
            request = manager.cancel(active_job_id)
            final = wait_for(manager, active_job_id, {"cancelled"}, timeout=deadline_s)
            latency = time.monotonic() - requested_at
            latencies.append(latency)
            records.append(
                {
                    "iteration": index + 1,
                    "job_id": active_job_id,
                    "request_id": request["request_id"],
                    "deadline_s": deadline_s,
                    "observed_latency_s": latency,
                    "status": final["status"],
                    "phase": final["cancel"].get("phase"),
                    "cleanup_absent": final["cancel"]["verification"]["absent"],
                    "teardown_latency": final["cancel"].get("teardown_latency"),
                }
            )
            assert final["cancel"]["verification"]["absent"] is True
            if len(latencies) == calibration_count:
                baseline_s = max(latencies)
                deadline_s = min(10.0, max(1.0, baseline_s * 4.0 + 0.5))
        summary = {
            "success": True,
            "iterations": iterations,
            "calibration_iterations": calibration_count,
            "baseline_max_s": max(latencies[:calibration_count]),
            "derived_deadline_s": deadline_s,
            "maximum_observed_latency_s": max(latencies),
            "records": records,
        }
        summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        artifact_root_value = os.environ.get("COMSOL_E4R_SOAK_ARTIFACT_ROOT")
        if artifact_root_value:
            artifact_root = Path(artifact_root_value)
            artifact_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(
                summary_path,
                artifact_root / f"soak-{jobs_root.name}.json",
            )
    except Exception as exc:
        summary = {
            "success": False,
            "iterations_requested": iterations,
            "iterations_completed": len(records),
            "active_job_id": active_job_id,
            "deadline_s": deadline_s,
            "error": f"{type(exc).__name__}: {exc}",
            "records": records,
            "active_state": manager.status(active_job_id) if active_job_id else None,
            "active_tail": manager.tail(active_job_id, 100) if active_job_id else None,
        }
        summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        archive_root = Path(
            os.environ.get("COMSOL_E4R_FAILURE_ROOT", "D:/comsol_runtime_test/e4r_failures")
        )
        archive_root.mkdir(parents=True, exist_ok=True)
        archive = archive_root / f"{jobs_root.name}-{int(time.time())}"
        archive.mkdir(parents=True, exist_ok=False)
        shutil.copy2(summary_path, archive / summary_path.name)
        try:
            shutil.copytree(jobs_root, archive / "jobs", dirs_exist_ok=True)
        except OSError as archive_exc:
            (archive / "archive_error.txt").write_text(
                f"{type(archive_exc).__name__}: {archive_exc}\n",
                encoding="utf-8",
            )
        raise AssertionError(
            f"E4R cancellation soak failed; durable evidence archived at {archive}"
        ) from exc


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


def test_atomic_state_replace_retries_transient_windows_file_lock(jobs_root, monkeypatch):
    path = jobs_root / "state.json"
    real_replace = store_module.os.replace
    calls = 0

    def flaky_replace(source, destination):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("simulated transient sharing violation")
        return real_replace(source, destination)

    monkeypatch.setattr(store_module.os, "replace", flaky_replace)

    atomic_write_json(path, {"status": "completed"})

    assert calls == 2
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "completed"


def test_job_lock_release_retries_transient_windows_reader(jobs_root, monkeypatch):
    lock_path = jobs_root / ".state.lock"
    real_unlink = Path.unlink
    calls = 0

    def flaky_unlink(path, *args, **kwargs):
        nonlocal calls
        if path == lock_path:
            calls += 1
            if calls == 1:
                raise PermissionError("simulated polling reader sharing violation")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    with JobLock(lock_path, timeout=0.5):
        assert lock_path.exists()

    assert calls == 2
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


def test_production_submit_uses_parameter_count_not_test_delays(jobs_root, monkeypatch):
    source = jobs_root / "baseline.mph"
    source.write_bytes(b"model")
    manager = JobManager(
        jobs_root,
        preflight=lambda **_kwargs: {"ready": True},
    )
    identity = process_identity(os.getpid())
    monkeypatch.setattr(manager, "_launch_worker", lambda *_args: identity)

    submitted = manager.submit(
        {
            "job_type": "staged_sweep",
            "source_model_path": str(source),
            "parameter_name": "wl",
            "parameter_values": [4.25, 4.251, 4.253],
            "expressions": ["A"],
        }
    )

    assert manager.store.read_state(submitted["job_id"])["progress"] == {
        "completed": 0,
        "total": 3,
    }


def test_resume_preflight_can_read_job_summaries_without_self_lock_delay(jobs_root, monkeypatch):
    source = jobs_root / "baseline.mph"
    source.write_bytes(b"model")
    manager = None

    def preflight(**_kwargs):
        assert manager is not None
        assert manager.summaries()["available"] is True
        return {"ready": True}

    manager = JobManager(jobs_root, preflight=preflight)
    spec = validate_staged_sweep_spec(
        {
            "job_type": "staged_sweep",
            "source_model_path": str(source),
            "parameter_name": "wl",
            "parameter_values": [4.25],
            "expressions": ["A"],
        }
    )
    job_id = manager.store.create(
        spec,
        {
            "schema_version": "1",
            "status": "interrupted",
            "attempt": 1,
            "progress": {"completed": 0, "total": 1},
        },
    )
    identity = process_identity(os.getpid())
    monkeypatch.setattr(manager, "_launch_worker", lambda *_args: identity)

    started = time.monotonic()
    result = manager.resume(job_id)

    assert time.monotonic() - started < 1.0
    assert result["attempt"] == 2


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
            self.cleared = False
            self.port = None

        def load(self, _path):
            return object()

        def disconnect(self):
            self.disconnected = True

        def clear(self):
            self.cleared = True

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
