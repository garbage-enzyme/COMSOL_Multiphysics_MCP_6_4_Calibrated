"""Detached exact-attempt wall watchdog for licensed robust shape jobs."""

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
    if (
        spec.get("job_type") != "robust_shape_optimization"
        or spec.get("synthetic_mode") is not False
    ):
        _write_outcome(
            store,
            job_id,
            attempt=attempt,
            status="inapplicable",
            fields={"reason": "not_a_licensed_robust_job"},
            clock=clock,
        )
        return 2
    expected_budget = int(spec["native_optimizer"]["budget"]["max_wall_time_seconds"])
    if int(record.get("budget_seconds", -1)) != expected_budget:
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
