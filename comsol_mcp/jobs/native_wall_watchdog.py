"""Detached exact-attempt wall watchdog for licensed native execution."""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .manager import JobManager
from .store import TERMINAL_STATES, JobStore, atomic_write_json, read_json

WATCHDOG_SCHEMA_NAME = "comsol_mcp.robust_wall_watchdog"
WATCHDOG_SCHEMA_VERSION = "1.0.0"


def native_wall_budget(spec: dict[str, Any]) -> tuple[str, int | float] | None:
    """Read the declared native wall budget without rounding or selecting policy."""
    if spec.get("synthetic_mode") is not False:
        return None
    kind = spec.get("job_type")
    if kind == "adjoint_optimization":
        return "optimizer.budget.max_wall_time_seconds", int(
            spec["optimizer"]["budget"]["max_wall_time_seconds"]
        )
    if kind == "validation_matrix" and any(
        item["name"] in {"native_condition_gradient", "native_condition_forward"}
        for point in spec["points"]
        for item in point["collectors"]
    ):
        value = spec["resource_policy"]["rules"]["wall_time_budget_seconds"]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError("native wall budget must be a finite positive number")
        return "resource_policy.rules.wall_time_budget_seconds", value
    return None


def await_native_wall_watchdog(
    store: JobStore,
    job_id: str,
    spec: dict[str, Any],
    attempt: int,
    *,
    timeout_seconds: float = 2.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    wall_clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Require the exact worker, attempt and declared deadline before native startup."""
    budget = native_wall_budget(spec)
    if budget is None:
        raise RuntimeError("native execution has no bound wall budget")
    source, seconds = budget
    deadline = monotonic() + timeout_seconds
    while True:
        try:
            record = read_json(store.job_dir(job_id) / "wall-watchdog.json")
        except FileNotFoundError, RuntimeError:
            record = None
        if isinstance(record, dict) and record.get("status") == "armed":
            state = store.read_state(job_id)
            worker = {
                "pid": state.get("worker_pid"),
                "process_create_time": state.get("worker_process_create_time"),
                "command_signature": state.get("worker_command_signature"),
            }
            if record.get("attempt") != attempt or record.get("target_worker") != worker:
                raise RuntimeError("native wall watchdog worker or attempt identity differs")
            if record.get("budget_source") != source or record.get("budget_seconds") != seconds:
                raise RuntimeError("native wall watchdog budget identity differs")
            expected = float(worker["process_create_time"]) + seconds
            if record.get("deadline_epoch") != expected or wall_clock() >= expected:
                raise RuntimeError("native wall watchdog deadline is invalid or expired")
            return record
        if isinstance(record, dict) and record.get("status") == "launch_failed":
            raise RuntimeError("native wall watchdog launch failed")
        if monotonic() >= deadline:
            raise RuntimeError("native wall watchdog was not armed before startup")
        sleep(min(0.02, max(0.0, deadline - monotonic())))


def _write_outcome(
    store: JobStore,
    job_id: str,
    *,
    attempt: int,
    status: str,
    fields: dict[str, Any] | None = None,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    path = store.job_dir(job_id) / "wall-watchdog.json"
    with store.lock(job_id):
        record = read_json(path)
        if int(record.get("attempt", -1)) != int(attempt):
            raise RuntimeError("wall watchdog artifact attempt changed")
        updated = {
            **record,
            "status": status,
            **(fields or {}),
            "updated_at_epoch": clock(),
        }
        atomic_write_json(path, updated)
        store._append_event_unlocked(
            job_id,
            f"robust_wall_watchdog_{status}",
            {"attempt": int(attempt)},
            str(store.read_state(job_id).get("status")),
        )
        return updated


def run(
    root: str | Path,
    job_id: str,
    attempt: int,
    deadline_epoch: float,
    *,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    manager_factory: Callable[..., JobManager] = JobManager,
) -> int:
    if isinstance(attempt, bool) or int(attempt) < 1:
        raise ValueError("watchdog attempt must be positive")
    attempt = int(attempt)
    deadline_epoch = float(deadline_epoch)
    if not math.isfinite(deadline_epoch):
        raise ValueError("watchdog deadline must be finite")
    store = JobStore(root)
    artifact = store.job_dir(job_id) / "wall-watchdog.json"

    arm_deadline = clock() + 2.0
    while True:
        record = read_json(artifact)
        if (
            record.get("status") == "armed"
            and int(record.get("attempt", -1)) == attempt
            and float(record.get("deadline_epoch", math.nan)) == deadline_epoch
        ):
            break
        if record.get("status") == "launch_failed" or clock() >= arm_deadline:
            return 2
        sleep(min(0.02, max(0.0, arm_deadline - clock())))

    spec = store.read_spec(job_id)
    budget = native_wall_budget(spec)
    if budget is None:
        _write_outcome(
            store,
            job_id,
            attempt=attempt,
            status="inapplicable",
            fields={"reason": "not_a_licensed_robust_job"},
            clock=clock,
        )
        return 2
    _budget_source, expected_budget = budget
    if record.get("budget_seconds") != expected_budget:
        _write_outcome(
            store,
            job_id,
            attempt=attempt,
            status="invalid_binding",
            fields={"reason": "budget_mismatch"},
            clock=clock,
        )
        return 2

    while True:
        state = store.read_state(job_id)
        current_attempt = int(state.get("attempt", -1))
        if current_attempt != attempt:
            try:
                _write_outcome(
                    store,
                    job_id,
                    attempt=attempt,
                    status="stale_attempt_refused",
                    fields={"observed_attempt": current_attempt},
                    clock=clock,
                )
            except RuntimeError:
                # A successor attempt re-armed the artifact before this stale
                # watchdog could record; the refusal itself remains the truth
                # and must not escalate into a failed outcome.
                pass
            return 0
        if state.get("status") in TERMINAL_STATES:
            _write_outcome(
                store,
                job_id,
                attempt=attempt,
                status="terminal_before_deadline",
                fields={"terminal_job_status": state["status"]},
                clock=clock,
            )
            return 0
        now = clock()
        if now >= deadline_epoch:
            manager = manager_factory(store.root, reconcile_on_start=False)
            result = manager.cancel(job_id, expected_attempt=attempt)
            if result.get("success"):
                outcome = "cancellation_requested"
                fields = {
                    "deadline_reached_at_epoch": now,
                    "cancellation": result,
                }
            else:
                # The cancel may have raced a terminal transition or a new
                # attempt; re-read the durable state before refusing.
                reread = store.read_state(job_id)
                if int(reread.get("attempt", -1)) != attempt:
                    try:
                        _write_outcome(
                            store,
                            job_id,
                            attempt=attempt,
                            status="stale_attempt_refused",
                            fields={"observed_attempt": int(reread.get("attempt", -1))},
                            clock=clock,
                        )
                    except RuntimeError:
                        pass
                    return 0
                if reread.get("status") in TERMINAL_STATES:
                    _write_outcome(
                        store,
                        job_id,
                        attempt=attempt,
                        status="terminal_before_deadline",
                        fields={
                            "terminal_job_status": reread["status"],
                            "deadline_reached_at_epoch": now,
                            "cancellation": result,
                        },
                        clock=clock,
                    )
                    return 0
                outcome = "cancellation_refused"
                fields = {
                    "deadline_reached_at_epoch": now,
                    "cancellation": result,
                }
            _write_outcome(
                store,
                job_id,
                attempt=attempt,
                status=outcome,
                fields=fields,
                clock=clock,
            )
            return 0 if result.get("success") else 2
        sleep(min(1.0, max(0.0, deadline_epoch - now)))


def main(argv: list[str] | None = None) -> int:
    values = sys.argv[1:] if argv is None else argv
    if len(values) != 4:
        return 2
    try:
        return run(values[0], values[1], int(values[2]), float(values[3]))
    except Exception as exc:
        try:
            store = JobStore(values[0])
            _write_outcome(
                store,
                values[1],
                attempt=int(values[2]),
                status="failed",
                fields={"error": {"type": type(exc).__name__, "message": str(exc)[:500]}},
            )
        except Exception:
            return 2
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run"]
