"""Detached, solver-free durable cancellation cancellation coordinator."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time
from typing import Any, Callable

import psutil

from .process_control import capture_owned_descendants, inspect_identity, terminate_exact, verify_absent
from .store import JobStore, atomic_write_json, cancel_request_targets_attempt, process_identity


_RESUMABLE_PHASES = {"terminate", "force_kill", "verifying"}


def _enter_phase(cancel: dict[str, Any], phase: str, now: float) -> dict[str, Any]:
    updated = dict(cancel)
    timestamps = dict(updated.get("phase_timestamps") or {})
    timestamps.setdefault(phase, float(now))
    updated["phase"] = phase
    updated["phase_timestamps"] = timestamps
    return updated


def _claim(
    store: JobStore,
    job_id: str,
    request_id: str,
    identity: dict[str, Any],
    *,
    grace_seconds: float,
    terminate_seconds: float,
) -> dict[str, Any] | None:
    with store.lock(job_id):
        state = store.read_state(job_id)
        control = store.read_control(job_id)
        cancel = state.get("cancel") if isinstance(state.get("cancel"), dict) else {}
        attempt = int(state.get("attempt", 1))
        if (
            control.get("request") != "cancel_requested"
            or control.get("request_id") != request_id
            or not cancel_request_targets_attempt(control, attempt)
            or cancel.get("target_attempt") not in (None, attempt)
            or state.get("status") not in {"cancel_requested", "cancelling"}
        ):
            return None
        coordinator = cancel.get("coordinator")
        if isinstance(coordinator, dict) and coordinator.get("pid") != identity["pid"]:
            if inspect_identity(coordinator)["state"] == "active":
                return None
        previous_phase = cancel.get("phase")
        now = time.time()
        cancel["coordinator"] = identity
        cancel = _enter_phase(
            cancel,
            previous_phase if previous_phase in _RESUMABLE_PHASES else "native_grace",
            now,
        )
        cancel["timing_policy"] = {
            "native_grace_budget_s": max(0.0, float(grace_seconds)),
            "terminate_budget_s": max(0.0, float(terminate_seconds)),
        }
        cancel.pop("blocker", None)
        state["cancel"] = cancel
        state["status"] = "cancelling"
        state["updated_at_epoch"] = now
        atomic_write_json(store.job_dir(job_id) / "state.json", state)
        store._append_event_unlocked(
            job_id,
            "cancel_coordinator_claimed",
            {"request_id": request_id, "coordinator_pid": identity["pid"]},
            "cancelling",
        )
        return {"state": state, "control": control, "resume_phase": cancel["phase"]}


def _checkpoint(
    store: JobStore,
    job_id: str,
    request_id: str,
    identity: dict[str, Any],
    phase: str,
    *,
    patch: dict[str, Any] | None = None,
) -> bool:
    """Persist one restartable phase before its associated side effect."""
    with store.lock(job_id):
        state = store.read_state(job_id)
        control = store.read_control(job_id)
        cancel = dict(state.get("cancel") or {})
        if (
            control.get("request_id") != request_id
            or state.get("status") != "cancelling"
            or (cancel.get("coordinator") or {}).get("pid") != identity.get("pid")
        ):
            return False
        now = time.time()
        cancel = _enter_phase(cancel, phase, now)
        if patch:
            cancel.update(patch)
        state["cancel"] = cancel
        state["updated_at_epoch"] = now
        atomic_write_json(store.job_dir(job_id) / "state.json", state)
        store._append_event_unlocked(
            job_id,
            "cancel_phase_checkpoint",
            {"request_id": request_id, "phase": phase},
            "cancelling",
        )
        return True


def _record_blocker(store: JobStore, job_id: str, request_id: str, message: str) -> None:
    with store.lock(job_id):
        state = store.read_state(job_id)
        control = store.read_control(job_id)
        if control.get("request_id") != request_id or state.get("status") != "cancelling":
            return
        cancel = dict(state.get("cancel") or {})
        now = time.time()
        cancel = _enter_phase(cancel, "blocked", now)
        cancel["blocker"] = message
        state["cancel"] = cancel
        state["updated_at_epoch"] = now
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
        completed_at = time.time()
        cancel = _enter_phase(cancel, "verified", completed_at)
        cancel = _enter_phase(cancel, "terminal_commit", completed_at)
        timestamps = dict(cancel.get("phase_timestamps") or {})
        requested_at = timestamps.get("requested", cancel.get("requested_at_epoch"))
        coordinator_at = min(
            (
                float(value)
                for phase, value in timestamps.items()
                if phase not in {"requested", "terminal_commit"}
            ),
            default=None,
        )
        verifying_at = timestamps.get("verifying")
        cancel.update(
            {
                "phase": "verified",
                "worker_actions": actions,
                "verification": verification,
                "completed_at_epoch": completed_at,
                "teardown_latency": {
                    "requested_to_terminal_s": (
                        max(0.0, completed_at - float(requested_at))
                        if requested_at is not None
                        else None
                    ),
                    "coordinator_to_terminal_s": (
                        max(0.0, completed_at - coordinator_at)
                        if coordinator_at is not None
                        else None
                    ),
                    "verification_to_terminal_s": (
                        max(0.0, completed_at - float(verifying_at))
                        if verifying_at is not None
                        else None
                    ),
                },
            }
        )
        state["status"] = "cancelled"
        state["cancel"] = cancel
        state["updated_at_epoch"] = completed_at
        atomic_write_json(store.job_dir(job_id) / "state.json", state)
        store._append_event_unlocked(job_id, "cancelled", {"request_id": request_id}, "cancelled")
        return True


def _verify_solver_cleanup(store: JobStore, job_id: str) -> dict[str, Any]:
    """Verify the target job's recorded lease, server identities, and port.

    Bare historical server PIDs are intentionally insufficient: an durable cancellation terminal
    cancellation must remain blocked rather than risk acting after PID reuse.
    """
    # This common path is intentionally dependency-free: most workers release
    # their lease before exiting, and a coordinator must not import the complete
    # MCP tool package merely to confirm its absence.
    if not (store.root.parent / "solver_owner.json").is_file():
        return {"ok": True, "lease_state": "absent", "lease_recovered": False, "recorded_port_closed": True}

    from src.tools.ownership import SolverOwnership

    ownership = SolverOwnership(store.root.parent)
    status = ownership.status()
    lease_status = status["lease"]
    if lease_status["state"] == "absent":
        return {"ok": True, "lease_state": "absent", "lease_recovered": False, "recorded_port_closed": True}
    lease = lease_status.get("lease")
    if not isinstance(lease, dict) or lease.get("owner") != f"job:{job_id}":
        return {"ok": False, "reason": "target job does not exclusively own the recorded solver lease", "lease_state": lease_status["state"]}
    servers = lease.get("comsol_server_processes")
    if not isinstance(servers, list):
        return {"ok": False, "reason": "lease server identities are missing", "lease_state": lease_status["state"]}
    if lease.get("comsol_server_pids") and not servers:
        return {"ok": False, "reason": "lease contains only legacy server PIDs", "lease_state": lease_status["state"]}
    server_verification = verify_absent(servers)
    if not server_verification["absent"]:
        return {"ok": False, "reason": "recorded server identity remains active or uncertain", "servers": server_verification, "lease_state": lease_status["state"]}
    port = lease.get("comsol_server_port")
    try:
        port_open = bool(port) and any(
            connection.laddr and int(connection.laddr.port) == int(port)
            for connection in psutil.net_connections(kind="inet")
        )
    except (psutil.AccessDenied, OSError) as exc:
        return {"ok": False, "reason": f"cannot verify recorded server port: {exc}", "lease_state": lease_status["state"]}
    if port_open:
        return {"ok": False, "reason": "recorded COMSOL server port remains open", "lease_state": lease_status["state"]}
    if lease_status["state"] != "stale":
        return {"ok": False, "reason": "target job lease is not proven stale", "lease_state": lease_status["state"]}
    recovered = ownership.recover_stale()
    if not recovered.get("success"):
        return {"ok": False, "reason": f"stale lease recovery refused: {recovered}", "lease_state": lease_status["state"]}
    return {
        "ok": True,
        "lease_state": "recovered",
        "lease_recovered": bool(recovered.get("recovered")),
        "recorded_port_closed": True,
        "servers": server_verification,
    }


def _verified_cancel(store: JobStore, job_id: str, worker_verification: dict[str, Any]) -> dict[str, Any]:
    solver = _verify_solver_cleanup(store, job_id)
    return {**worker_verification, "solver": solver, "absent": bool(worker_verification["absent"] and solver["ok"])}


def _wait_for_process_absence(
    identities: list[dict[str, Any]],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Poll boundedly after termination; uncertainty fails closed immediately."""
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        verification = verify_absent(identities)
        if verification["absent"]:
            return verification
        if any(item.get("state") == "uncertain" for item in verification.get("verdicts", [])):
            return verification
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return verification
        time.sleep(min(0.025, remaining))


def run(
    root: str,
    job_id: str,
    request_id: str,
    grace_seconds: float = 10.0,
    terminate_seconds: float = 5.0,
    *,
    phase_hook: Callable[[str], None] | None = None,
) -> int:
    store = JobStore(Path(root))
    identity = process_identity(os.getpid())
    cleanup_verify_seconds = max(0.5, float(terminate_seconds))
    claimed = _claim(
        store,
        job_id,
        request_id,
        identity,
        grace_seconds=grace_seconds,
        terminate_seconds=terminate_seconds,
    )
    if claimed is None:
        return 0
    control = claimed["control"]
    resume_phase = claimed["resume_phase"]
    worker = control.get("target_worker")
    if not isinstance(worker, dict) or worker.get("pid") is None:
        _record_blocker(store, job_id, request_id, "target worker identity is missing")
        return 2

    cancel_evidence = claimed["state"].get("cancel") or {}
    cancel_evidence["timing_policy"] = {
        **dict(cancel_evidence.get("timing_policy") or {}),
        "cleanup_verification_budget_s": cleanup_verify_seconds,
    }
    if not _checkpoint(
        store,
        job_id,
        request_id,
        identity,
        resume_phase,
        patch={"timing_policy": cancel_evidence["timing_policy"]},
    ):
        return 0
    persisted_descendants = cancel_evidence.get("descendants")
    if isinstance(persisted_descendants, list):
        initial_descendants = persisted_descendants
    else:
        initial_capture = capture_owned_descendants(worker)
        if initial_capture["worker"]["state"] == "uncertain":
            _record_blocker(store, job_id, request_id, initial_capture["worker"]["reason"])
            return 0
        process_tree_contained = bool(
            store.read_state(job_id).get("process_tree_contained")
        )
        capture_complete = bool(initial_capture.get("capture_complete", True))
        if not capture_complete and not process_tree_contained:
            _record_blocker(
                store,
                job_id,
                request_id,
                str(
                    initial_capture.get("reason")
                    or "worker exited before descendants were captured and no process-tree containment is proved"
                ),
            )
            return 0
        initial_descendants = initial_capture["descendants"]
        if not _checkpoint(
            store,
            job_id,
            request_id,
            identity,
            resume_phase,
            patch={
                "descendants": initial_descendants,
                "descendant_capture": {
                    **initial_capture,
                    "captured_at_epoch": time.time(),
                    "capture_method": (
                        "live_enumeration"
                        if capture_complete
                        else "contained_worker_exit"
                    ),
                    "process_tree_contained": process_tree_contained,
                },
            },
        ):
            return 0

    if phase_hook is not None:
        phase_hook(resume_phase)

    if resume_phase not in _RESUMABLE_PHASES:
        deadline = time.monotonic() + max(0.0, float(grace_seconds))
        while time.monotonic() < deadline:
            if inspect_identity(worker)["state"] != "active":
                if not _checkpoint(store, job_id, request_id, identity, "verifying"):
                    return 0
                if phase_hook is not None:
                    phase_hook("verifying")
                verification = _verified_cancel(
                    store,
                    job_id,
                    _wait_for_process_absence(
                        [worker, *initial_descendants],
                        cleanup_verify_seconds,
                    ),
                )
                if verification["absent"]:
                    return 0 if _commit_cancelled(store, job_id, request_id, verification, []) else 0
                _record_blocker(store, job_id, request_id, str(verification.get("solver", {}).get("reason") or "worker exited during grace with uncertain descendants"))
                return 0
            time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))

    captured = capture_owned_descendants(worker)
    if captured["worker"]["state"] != "active" and resume_phase not in {"terminate", "force_kill"}:
        if not _checkpoint(store, job_id, request_id, identity, "verifying"):
            return 0
        if phase_hook is not None:
            phase_hook("verifying")
        verification = _verified_cancel(
            store,
            job_id,
            _wait_for_process_absence(
                [worker, *initial_descendants],
                cleanup_verify_seconds,
            ),
        )
        if verification["absent"]:
            return 0 if _commit_cancelled(store, job_id, request_id, verification, []) else 0
        _record_blocker(store, job_id, request_id, str(verification.get("solver", {}).get("reason") or captured["worker"]["reason"]))
        return 0
    descendants = initial_descendants or captured["descendants"]
    actions = list(cancel_evidence.get("worker_actions") or [])
    if resume_phase != "verifying":
        if resume_phase != "force_kill":
            if not _checkpoint(store, job_id, request_id, identity, "terminate", patch={"descendants": descendants, "worker_actions": actions}):
                return 0
            if phase_hook is not None:
                phase_hook("terminate")
            actions.append(terminate_exact(worker, force=False))
            if not _checkpoint(store, job_id, request_id, identity, "terminate", patch={"worker_actions": actions}):
                return 0
            wait_deadline = time.monotonic() + max(0.0, float(terminate_seconds))
            while time.monotonic() < wait_deadline and inspect_identity(worker)["state"] == "active":
                time.sleep(min(0.025, max(0.0, wait_deadline - time.monotonic())))
        if inspect_identity(worker)["state"] == "active":
            if not _checkpoint(store, job_id, request_id, identity, "force_kill", patch={"worker_actions": actions}):
                return 0
            if phase_hook is not None:
                phase_hook("force_kill")
            actions.append(terminate_exact(worker, force=True))
            if not _checkpoint(store, job_id, request_id, identity, "force_kill", patch={"worker_actions": actions}):
                return 0
    for descendant in descendants:
        if inspect_identity(descendant)["state"] == "active":
            actions.append(terminate_exact(descendant, force=True))
    if not _checkpoint(store, job_id, request_id, identity, "verifying", patch={"worker_actions": actions}):
        return 0
    if phase_hook is not None:
        phase_hook("verifying")
    verification = _verified_cancel(
        store,
        job_id,
        _wait_for_process_absence(
            [worker, *descendants],
            cleanup_verify_seconds,
        ),
    )
    if not verification["absent"]:
        _record_blocker(store, job_id, request_id, str(verification.get("solver", {}).get("reason") or "worker or captured descendant identity remains active/uncertain"))
        return 0
    return 0 if _commit_cancelled(store, job_id, request_id, verification, actions) else 0


if __name__ == "__main__":
    code = run(sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4]), float(sys.argv[5]))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
