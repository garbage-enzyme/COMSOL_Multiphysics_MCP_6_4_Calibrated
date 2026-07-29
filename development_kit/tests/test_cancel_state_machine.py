"""Deterministic cancellation determinism cancellation state-machine tests without wall-clock sleeps."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Any
import uuid

import pytest

from src.jobs import cancel_worker
from src.jobs.store import JobStore, process_identity, read_json


class FakeClock:
    def __init__(self, epoch: float = 1000.0):
        self.epoch = float(epoch)
        self.elapsed = 0.0

    def time(self) -> float:
        return self.epoch + self.elapsed

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.elapsed += max(0.0, float(seconds))


class FakeProcesses:
    def __init__(
        self,
        clock: FakeClock,
        *,
        auto_exit_at: float | None = None,
        initial_worker_state: str = "active",
        coordinator_state: str = "active",
        descendants: int = 0,
    ):
        self.clock = clock
        self.auto_exit_at = auto_exit_at
        self.worker_state = initial_worker_state
        self.coordinator_state = coordinator_state
        self.worker = self._identity(41001, "worker")
        self.coordinator = self._identity(41002, "coordinator")
        self.descendants = [
            self._identity(41100 + index, f"descendant-{index}") for index in range(descendants)
        ]
        self.descendant_states = {item["pid"]: "active" for item in self.descendants}
        self.actions: list[dict[str, Any]] = []

    @staticmethod
    def _identity(pid: int, signature: str) -> dict[str, Any]:
        return {
            "pid": pid,
            "process_create_time": float(pid),
            "command_signature": signature,
        }

    def _refresh_worker(self) -> None:
        if self.auto_exit_at is not None and self.clock.elapsed >= self.auto_exit_at:
            self.worker_state = "stale"

    def inspect(self, identity: dict[str, Any]) -> dict[str, Any]:
        if identity.get("pid") == self.worker["pid"]:
            self._refresh_worker()
            state = self.worker_state
            reason = "worker PID was reused" if state == "stale" else "worker identity matches"
        elif identity.get("pid") in self.descendant_states:
            state = self.descendant_states[int(identity["pid"])]
            reason = (
                "descendant identity is absent"
                if state == "stale"
                else "descendant identity matches"
            )
        else:
            state = self.coordinator_state
            reason = (
                "coordinator identity matches"
                if state == "active"
                else "coordinator identity is absent"
            )
        return {"identity": identity, "state": state, "reason": reason}

    def capture(self, identity: dict[str, Any]) -> dict[str, Any]:
        verdict = self.inspect(identity)
        return {
            "worker": verdict,
            "descendants": list(self.descendants) if verdict["state"] == "active" else [],
            "capture_complete": verdict["state"] == "active",
            "reason": (
                None if verdict["state"] == "active" else "worker exited during descendant capture"
            ),
        }

    def terminate(self, identity: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        before = self.inspect(identity)
        action = {
            "acted": before["state"] == "active",
            "action": "kill" if force else "terminate",
            "before": before,
        }
        self.actions.append(action)
        if before["state"] == "active" and force:
            if identity.get("pid") == self.worker["pid"]:
                self.worker_state = "stale"
            elif identity.get("pid") in self.descendant_states:
                self.descendant_states[int(identity["pid"])] = "stale"
        return action

    def verify(self, identities: list[dict[str, Any]]) -> dict[str, Any]:
        verdicts = [self.inspect(identity) for identity in identities]
        return {
            "absent": all(item["state"] == "stale" for item in verdicts),
            "verdicts": verdicts,
        }


@pytest.fixture()
def jobs_root():
    root = Path("D:/comsol_runtime_test/cancellation_determinism") / uuid.uuid4().hex
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _prepare_cancel(
    store: JobStore,
    processes: FakeProcesses,
    *,
    process_tree_contained: bool = True,
) -> tuple[str, str]:
    worker = processes.worker
    job_id = store.create(
        {"schema_version": "2", "job_type": "test"},
        {
            "schema_version": "2",
            "status": "running",
            "attempt": 1,
            "worker_pid": worker["pid"],
            "worker_process_create_time": worker["process_create_time"],
            "worker_command_signature": worker["command_signature"],
            "process_tree_contained": process_tree_contained,
        },
    )
    request = store.request_cancel(job_id, requester_identity=process_identity(os.getpid()))
    return job_id, str(request["control"]["request_id"])


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    processes: FakeProcesses,
    *,
    solver_cleanup: dict[str, Any] | None = None,
) -> None:
    monkeypatch.setattr(cancel_worker.time, "time", clock.time)
    monkeypatch.setattr(cancel_worker.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(cancel_worker.time, "sleep", clock.sleep)
    monkeypatch.setattr(cancel_worker, "process_identity", lambda _pid: processes.coordinator)
    monkeypatch.setattr(cancel_worker, "inspect_identity", processes.inspect)
    monkeypatch.setattr(cancel_worker, "capture_owned_descendants", processes.capture)
    monkeypatch.setattr(cancel_worker, "terminate_exact", processes.terminate)
    monkeypatch.setattr(cancel_worker, "verify_absent", processes.verify)
    monkeypatch.setattr(
        cancel_worker,
        "_verify_solver_cleanup",
        lambda _store, _job_id: (
            solver_cleanup
            if solver_cleanup is not None
            else {
                "ok": True,
                "lease_state": "absent",
                "lease_recovered": False,
                "recorded_port_closed": True,
            }
        ),
    )


def test_cooperative_exit_inside_native_grace_commits_verified_cancelled(jobs_root, monkeypatch):
    clock = FakeClock()
    processes = FakeProcesses(clock, auto_exit_at=0.5)
    store = JobStore(jobs_root)
    job_id, request_id = _prepare_cancel(store, processes)
    _install_fakes(monkeypatch, clock, processes)

    assert cancel_worker.run(str(jobs_root), job_id, request_id, 1.0, 2.0) == 0
    state = store.read_state(job_id)

    assert state["status"] == "cancelled"
    assert state["cancel"]["verification"]["absent"] is True
    assert state["cancel"]["worker_actions"] == []
    assert clock.elapsed == pytest.approx(0.5)
    assert set(state["cancel"]["phase_timestamps"]) >= {
        "requested",
        "native_grace",
        "verifying",
        "verified",
        "terminal_commit",
    }


def test_grace_and_terminate_deadlines_reach_force_kill_deterministically(jobs_root, monkeypatch):
    clock = FakeClock()
    processes = FakeProcesses(clock, descendants=1)
    store = JobStore(jobs_root)
    job_id, request_id = _prepare_cancel(store, processes)
    _install_fakes(monkeypatch, clock, processes)

    assert cancel_worker.run(str(jobs_root), job_id, request_id, 1.0, 2.0) == 0
    state = store.read_state(job_id)
    actions = state["cancel"]["worker_actions"]

    assert state["status"] == "cancelled"
    assert clock.elapsed == pytest.approx(3.0)
    assert [item["action"] for item in actions] == ["terminate", "kill", "kill"]
    timestamps = state["cancel"]["phase_timestamps"]
    assert timestamps["terminate"] - timestamps["native_grace"] == pytest.approx(1.0)
    assert timestamps["force_kill"] - timestamps["terminate"] == pytest.approx(2.0)
    assert timestamps["verifying"] == pytest.approx(timestamps["force_kill"])


def test_pid_reuse_is_never_terminated_and_can_be_verified_absent(jobs_root, monkeypatch):
    clock = FakeClock()
    processes = FakeProcesses(clock, initial_worker_state="stale")
    store = JobStore(jobs_root)
    job_id, request_id = _prepare_cancel(store, processes)
    _install_fakes(monkeypatch, clock, processes)

    assert cancel_worker.run(str(jobs_root), job_id, request_id, 1.0, 2.0) == 0
    state = store.read_state(job_id)

    assert state["status"] == "cancelled"
    assert processes.actions == []
    assert state["cancel"]["verification"]["verdicts"][0]["reason"] == "worker PID was reused"
    assert state["cancel"]["descendant_capture"]["capture_method"] == "contained_worker_exit"


def test_worker_exit_before_descendant_capture_blocks_without_containment(jobs_root, monkeypatch):
    clock = FakeClock()
    processes = FakeProcesses(clock, initial_worker_state="stale")
    store = JobStore(jobs_root)
    job_id, request_id = _prepare_cancel(
        store,
        processes,
        process_tree_contained=False,
    )
    _install_fakes(monkeypatch, clock, processes)

    assert cancel_worker.run(str(jobs_root), job_id, request_id, 1.0, 2.0) == 0
    state = store.read_state(job_id)

    assert state["status"] == "cancelling"
    assert state["cancel"]["phase"] == "blocked"
    assert state["cancel"]["blocker"] == "worker exited during descendant capture"


def test_cleanup_uncertainty_stays_nonterminal_with_durable_blocker(jobs_root, monkeypatch):
    clock = FakeClock()
    processes = FakeProcesses(clock, auto_exit_at=0.25)
    store = JobStore(jobs_root)
    job_id, request_id = _prepare_cancel(store, processes)
    _install_fakes(
        monkeypatch,
        clock,
        processes,
        solver_cleanup={
            "ok": False,
            "reason": "recorded COMSOL server port cannot be inspected",
            "lease_state": "uncertain",
        },
    )

    assert cancel_worker.run(str(jobs_root), job_id, request_id, 1.0, 2.0) == 0
    state = store.read_state(job_id)

    assert state["status"] == "cancelling"
    assert state["cancel"]["phase"] == "blocked"
    assert state["cancel"]["blocker"] == "recorded COMSOL server port cannot be inspected"
    assert "terminal_commit" not in state["cancel"]["phase_timestamps"]


def test_stale_coordinator_resumes_from_persisted_terminate_phase(jobs_root, monkeypatch):
    clock = FakeClock()
    processes = FakeProcesses(clock, coordinator_state="stale")
    store = JobStore(jobs_root)
    job_id, request_id = _prepare_cancel(store, processes)
    requested = store.read_state(job_id)["cancel"]
    original_terminate_at = clock.time() - 0.5
    store.update_state(
        job_id,
        "cancelling",
        patch={
            "cancel": {
                **requested,
                "phase": "terminate",
                "phase_timestamps": {
                    **requested["phase_timestamps"],
                    "terminate": original_terminate_at,
                },
                "coordinator": processes.coordinator,
                "descendants": [],
                "descendant_capture": {
                    "worker": processes.inspect(processes.worker),
                    "descendants": [],
                    "captured_at_epoch": clock.time() - 0.5,
                },
                "worker_actions": [],
            }
        },
    )
    _install_fakes(monkeypatch, clock, processes)

    assert cancel_worker.run(str(jobs_root), job_id, request_id, 1.0, 0.5) == 0
    state = store.read_state(job_id)

    assert state["status"] == "cancelled"
    assert state["cancel"]["phase_timestamps"]["terminate"] == original_terminate_at
    assert "native_grace" not in state["cancel"]["phase_timestamps"]
    assert clock.elapsed == pytest.approx(0.0)


def test_cleanup_verification_poll_is_bounded_and_waits_for_exit(monkeypatch):
    clock = FakeClock()
    identity = FakeProcesses._identity(42001, "delayed-exit")
    calls = 0

    def delayed_verify(_identities):
        nonlocal calls
        calls += 1
        state = "stale" if calls >= 3 else "active"
        return {
            "absent": state == "stale",
            "verdicts": [{"identity": identity, "state": state, "reason": state}],
        }

    monkeypatch.setattr(cancel_worker.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(cancel_worker.time, "sleep", clock.sleep)
    monkeypatch.setattr(cancel_worker, "verify_absent", delayed_verify)

    verified = cancel_worker._wait_for_process_absence([identity], 0.5)

    assert verified["absent"] is True
    assert calls == 3
    assert clock.elapsed == pytest.approx(0.05)


def test_uncertain_prior_coordinator_cannot_be_replaced(jobs_root, monkeypatch):
    store = JobStore(jobs_root)
    processes = FakeProcesses(FakeClock())
    job_id, request_id = _prepare_cancel(store, processes)
    prior = FakeProcesses._identity(43001, "prior-coordinator")
    requested = store.read_state(job_id)["cancel"]
    store.update_state(
        job_id,
        "cancelling",
        patch={"cancel": {**requested, "coordinator": prior}},
    )
    replacement = FakeProcesses._identity(43002, "replacement-coordinator")
    monkeypatch.setattr(
        cancel_worker,
        "inspect_identity",
        lambda value: {
            "identity": value,
            "state": "uncertain",
            "reason": "access denied",
        },
    )

    claimed = cancel_worker._claim(
        store,
        job_id,
        request_id,
        replacement,
        grace_seconds=1.0,
        terminate_seconds=1.0,
    )

    assert claimed is None
    assert store.read_state(job_id)["cancel"]["coordinator"] == prior


def test_superseded_coordinator_cannot_checkpoint_block_or_commit(jobs_root):
    store = JobStore(jobs_root)
    processes = FakeProcesses(FakeClock())
    job_id, request_id = _prepare_cancel(store, processes)
    first = FakeProcesses._identity(43101, "first-coordinator")
    second = FakeProcesses._identity(43102, "second-coordinator")
    requested = store.read_state(job_id)["cancel"]
    store.update_state(
        job_id,
        "cancelling",
        patch={"cancel": {**requested, "coordinator": second}},
    )

    assert cancel_worker._checkpoint(store, job_id, request_id, first, "terminate") is False
    assert (
        cancel_worker._record_blocker(
            store,
            job_id,
            request_id,
            first,
            "stale coordinator blocker",
        )
        is False
    )
    assert (
        cancel_worker._commit_cancelled(
            store,
            job_id,
            request_id,
            first,
            {"absent": True, "verdicts": []},
            [],
        )
        is False
    )
    state = store.read_state(job_id)
    assert state["status"] == "cancelling"
    assert state["cancel"]["coordinator"] == second
    assert "blocker" not in state["cancel"]


def test_resumed_native_grace_uses_only_remaining_budget(jobs_root, monkeypatch):
    clock = FakeClock()
    processes = FakeProcesses(clock)
    store = JobStore(jobs_root)
    job_id, request_id = _prepare_cancel(store, processes)
    requested = store.read_state(job_id)["cancel"]
    original_grace_at = clock.time() - 0.75
    store.update_state(
        job_id,
        "cancelling",
        patch={
            "cancel": {
                **requested,
                "phase": "native_grace",
                "phase_timestamps": {
                    **requested["phase_timestamps"],
                    "native_grace": original_grace_at,
                },
                "timing_policy": {
                    "native_grace_budget_s": 1.0,
                    "terminate_budget_s": 0.0,
                },
                "coordinator": processes.coordinator,
            }
        },
    )
    _install_fakes(monkeypatch, clock, processes)

    assert cancel_worker.run(str(jobs_root), job_id, request_id, 100.0, 100.0) == 0

    state = store.read_state(job_id)
    assert state["status"] == "cancelled"
    assert state["cancel"]["phase_timestamps"]["native_grace"] == original_grace_at
    assert clock.elapsed == pytest.approx(0.25)


def test_later_descendant_capture_is_merged_before_terminal_commit(jobs_root, monkeypatch):
    clock = FakeClock()
    processes = FakeProcesses(clock, descendants=2)
    store = JobStore(jobs_root)
    job_id, request_id = _prepare_cancel(store, processes)
    requested = store.read_state(job_id)["cancel"]
    first_descendant = processes.descendants[0]
    store.update_state(
        job_id,
        "cancelling",
        patch={
            "cancel": {
                **requested,
                "phase": "force_kill",
                "phase_timestamps": {
                    **requested["phase_timestamps"],
                    "force_kill": clock.time(),
                },
                "coordinator": processes.coordinator,
                "descendants": [first_descendant],
                "descendant_capture": {
                    "worker": processes.inspect(processes.worker),
                    "descendants": [first_descendant],
                    "capture_complete": True,
                    "capture_method": "live_enumeration",
                },
            }
        },
    )
    _install_fakes(monkeypatch, clock, processes)

    assert cancel_worker.run(str(jobs_root), job_id, request_id, 0.0, 0.0) == 0

    state = store.read_state(job_id)
    assert state["status"] == "cancelled"
    assert state["cancel"]["descendants"] == processes.descendants
    assert all(value == "stale" for value in processes.descendant_states.values())
    verified_pids = {
        item["identity"]["pid"] for item in state["cancel"]["verification"]["verdicts"]
    }
    assert verified_pids == {
        processes.worker["pid"],
        *(item["pid"] for item in processes.descendants),
    }


def test_fake_cleanup_preserves_an_explicit_empty_mapping(monkeypatch):
    clock = FakeClock()
    processes = FakeProcesses(clock)
    _install_fakes(monkeypatch, clock, processes, solver_cleanup={})

    assert cancel_worker._verify_solver_cleanup(None, "job-empty-cleanup") == {}


def test_cleanup_verification_poll_reaches_the_always_active_timeout(monkeypatch):
    clock = FakeClock()
    identity = FakeProcesses._identity(45001, "always-active")
    calls = 0

    def active_verify(_identities):
        nonlocal calls
        calls += 1
        return {
            "absent": False,
            "verdicts": [{"identity": identity, "state": "active", "reason": "active"}],
        }

    monkeypatch.setattr(cancel_worker.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(cancel_worker.time, "sleep", clock.sleep)
    monkeypatch.setattr(cancel_worker, "verify_absent", active_verify)

    verified = cancel_worker._wait_for_process_absence([identity], 0.1)

    assert verified["absent"] is False
    assert verified["verdicts"][0]["state"] == "active"
    assert calls == 5
    assert clock.elapsed == pytest.approx(0.1)


def test_coordinator_helper_retries_wrapped_reads_and_uses_action_verdict(
    jobs_root, monkeypatch
):
    from development_kit.tests.integration import coordinator_claim_kill

    coordinator = FakeProcesses._identity(45101, "restart-coordinator")
    store = JobStore(jobs_root)
    job_id = store.create(
        {},
        {
            "status": "cancelling",
            "cancel": {"phase": "terminate", "coordinator": coordinator},
        },
    )
    real_read = JobStore.read_state
    reads = 0
    action_before = {
        "identity": coordinator,
        "state": "active",
        "reason": "same process object",
    }

    def transient_read(self, target_job_id):
        nonlocal reads
        reads += 1
        if reads == 1:
            raise RuntimeError("transient durable read")
        return real_read(self, target_job_id)

    monkeypatch.setattr(JobStore, "read_state", transient_read)
    monkeypatch.setattr(coordinator_claim_kill.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        coordinator_claim_kill,
        "terminate_exact",
        lambda _identity: {"acted": True, "action": "terminate", "before": action_before},
    )

    assert coordinator_claim_kill.main(str(jobs_root), job_id, 0.5) == 0

    probe = read_json(store.job_dir(job_id) / "coordinator_restart_probe.json")
    assert reads == 2
    assert probe["before"] == action_before
    assert probe["action"]["acted"] is True


def test_coordinator_helper_writes_evidence_when_identity_action_raises(
    jobs_root, monkeypatch
):
    from development_kit.tests.integration import coordinator_claim_kill

    coordinator = {"pid": "malformed"}
    store = JobStore(jobs_root)
    job_id = store.create(
        {},
        {
            "status": "cancelling",
            "cancel": {"phase": "terminate", "coordinator": coordinator},
        },
    )
    monkeypatch.setattr(
        coordinator_claim_kill,
        "terminate_exact",
        lambda _identity: (_ for _ in ()).throw(ValueError("malformed coordinator")),
    )

    assert coordinator_claim_kill.main(str(jobs_root), job_id, 0.5) == 2

    probe = read_json(store.job_dir(job_id) / "coordinator_restart_probe.json")
    assert probe["coordinator"] == coordinator
    assert probe["error"] == {
        "type": "ValueError",
        "message": "malformed coordinator",
    }
