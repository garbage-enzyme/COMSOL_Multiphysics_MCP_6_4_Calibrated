"""Detached, solver-free H2 cancellation coordinator."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time
from typing import Any

from .process_control import capture_owned_descendants, inspect_identity, terminate_exact, verify_absent
from .store import JobStore, atomic_write_json, process_identity


def _claim(store: JobStore, job_id: str, request_id: str, identity: dict[str, Any]) -> dict[str, Any] | None:
    with store.lock(job_id):
        state = store.read_state(job_id)
        control = store.read_control(job_id)
        cancel = state.get("cancel") if isinstance(state.get("cancel"), dict) else {}
        if (
            control.get("request") != "cancel_requested"
            or control.get("request_id") != request_id
            or state.get("status") not in {"cancel_requested", "cancelling"}
        ):
            return None
        coordinator = cancel.get("coordinator")
        if isinstance(coordinator, dict) and coordinator.get("pid") != identity["pid"]:
            if inspect_identity(coordinator)["state"] == "active":
                return None
        cancel["coordinator"] = identity
        cancel["phase"] = "native_grace"
        state["cancel"] = cancel
        state["status"] = "cancelling"
        state["updated_at_epoch"] = time.time()
        atomic_write_json(store.job_dir(job_id) / "state.json", state)
        store._append_event_unlocked(
            job_id,
            "cancel_coordinator_claimed",
            {"request_id": request_id, "coordinator_pid": identity["pid"]},
            "cancelling",
        )
        return {"state": state, "control": control}


def _record_blocker(store: JobStore, job_id: str, request_id: str, message: str) -> None:
    with store.lock(job_id):
        state = store.read_state(job_id)
        control = store.read_control(job_id)
        if control.get("request_id") != request_id or state.get("status") != "cancelling":
            return
        cancel = dict(state.get("cancel") or {})
        cancel["phase"] = "blocked"
        cancel["blocker"] = message
        state["cancel"] = cancel
        state["updated_at_epoch"] = time.time()
        atomic_write_json(store.job_dir(job_id) / "state.json", state)
        store._append_event_unlocked(job_id, "cancel_blocked", {"request_id": request_id, "message": message}, "cancelling")


def _commit_cancelled(
    store: JobStore, job_id: str, request_id: str, verification: dict[str, Any], actions: list[dict[str, Any]]
) -> bool:
    with store.lock(job_id):
        state = store.read_state(job_id)
        control = store.read_control(job_id)
        if control.get("request_id") != request_id or state.get("status") != "cancelling":
            return False
        cancel = dict(state.get("cancel") or {})
        cancel.update({"phase": "verified", "worker_actions": actions, "verification": verification, "completed_at_epoch": time.time()})
        state["status"] = "cancelled"
        state["cancel"] = cancel
        state["updated_at_epoch"] = time.time()
        atomic_write_json(store.job_dir(job_id) / "state.json", state)
        store._append_event_unlocked(job_id, "cancelled", {"request_id": request_id}, "cancelled")
        return True


def run(root: str, job_id: str, request_id: str, grace_seconds: float = 10.0, terminate_seconds: float = 5.0) -> int:
    store = JobStore(Path(root))
    identity = process_identity(os.getpid())
    claimed = _claim(store, job_id, request_id, identity)
    if claimed is None:
        return 0
    control = claimed["control"]
    worker = control.get("target_worker")
    if not isinstance(worker, dict) or worker.get("pid") is None:
        _record_blocker(store, job_id, request_id, "target worker identity is missing")
        return 2

    initial_capture = capture_owned_descendants(worker)
    initial_descendants = initial_capture["descendants"]

    deadline = time.monotonic() + max(0.0, float(grace_seconds))
    while time.monotonic() < deadline:
        if inspect_identity(worker)["state"] != "active":
            verification = verify_absent([worker, *initial_descendants])
            if verification["absent"]:
                return 0 if _commit_cancelled(store, job_id, request_id, verification, []) else 0
            _record_blocker(store, job_id, request_id, "worker exited during grace with uncertain descendants")
            return 0
        time.sleep(0.025)

    captured = capture_owned_descendants(worker)
    if captured["worker"]["state"] != "active":
        _record_blocker(store, job_id, request_id, captured["worker"]["reason"])
        return 0
    descendants = captured["descendants"]
    actions = [terminate_exact(worker, force=False)]
    wait_deadline = time.monotonic() + max(0.0, float(terminate_seconds))
    while time.monotonic() < wait_deadline and inspect_identity(worker)["state"] == "active":
        time.sleep(0.025)
    if inspect_identity(worker)["state"] == "active":
        actions.append(terminate_exact(worker, force=True))
    for descendant in descendants:
        if inspect_identity(descendant)["state"] == "active":
            actions.append(terminate_exact(descendant, force=True))
    verification = verify_absent([worker, *descendants])
    if not verification["absent"]:
        _record_blocker(store, job_id, request_id, "worker or captured descendant identity remains active/uncertain")
        return 0
    return 0 if _commit_cancelled(store, job_id, request_id, verification, actions) else 0


if __name__ == "__main__":
    code = run(sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4]), float(sys.argv[5]))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
