"""Injected process-only worker used to prove reference-power durability without COMSOL."""

from __future__ import annotations

import os
from pathlib import Path
import csv
import sys
import time

from .process_control import contain_current_process_tree
from .store import JobStore, cancel_request_targets_attempt, process_identity


def _run(root: str, job_id: str) -> int:
    process_tree_contained = contain_current_process_tree()
    store = JobStore(Path(root))
    spec = store.read_spec(job_id)
    if spec.get("job_type") != "test_sequence":
        raise ValueError("Sequence worker refuses non-test jobs")
    identity = process_identity(os.getpid())
    deadline = time.monotonic() + 2.0
    while store.read_state(job_id).get("worker_pid") != identity["pid"]:
        if time.monotonic() >= deadline:
            raise RuntimeError("Control plane did not durably record the worker identity")
        time.sleep(0.01)
    store.update_state(
        job_id,
        patch={"process_tree_contained": bool(process_tree_contained)},
        event="worker_containment_recorded",
        event_data={"process_tree_contained": bool(process_tree_contained)},
    )
    initial_state = store.read_state(job_id)
    attempt = int(initial_state.get("attempt", 1))
    current = initial_state["status"]
    if current == "cancel_requested" or cancel_request_targets_attempt(store.read_control(job_id), attempt):
        store.record_cooperative_cancel_observed(
            job_id,
            attempt=attempt,
            message="Stopped before startup",
        )
        return 0
    if current == "submitted":
        store.update_state(job_id, "starting", event="worker_started")
    elif current != "starting":
        raise ValueError(f"Sequence worker cannot start from {current}")
    if cancel_request_targets_attempt(store.read_control(job_id), attempt):
        store.record_cooperative_cancel_observed(
            job_id,
            attempt=attempt,
            message="Stopped before smoke",
        )
        return 0
    store.update_state(job_id, "smoke_running", event="smoke_started")
    delays = spec["delays"]
    results_path = store.job_dir(job_id) / "results.csv"
    completed = set()
    if results_path.is_file() and results_path.stat().st_size:
        with results_path.open(newline="", encoding="utf-8") as handle:
            completed = {
                int(row["index"])
                for row in csv.DictReader(handle)
                if row.get("status") == "ok"
            }
    if 0 in completed:
        store.update_state(job_id, "smoke_validated", event="smoke_revalidated")
        if len(delays) > 1:
            store.update_state(job_id, "running", event="broad_phase_resumed")
    for index, delay in enumerate(delays):
        if index in completed:
            continue
        time.sleep(float(delay))
        write_header = not results_path.exists() or results_path.stat().st_size == 0
        with results_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["index", "status"])
            if write_header:
                writer.writeheader()
            writer.writerow({"index": index, "status": "ok"})
            handle.flush()
            os.fsync(handle.fileno())
        cancel_requested = cancel_request_targets_attempt(store.read_control(job_id), attempt)
        if cancel_requested:
            store.update_state(
                job_id,
                patch={"progress": {"completed": index + 1, "total": len(delays)}},
                event="sequence_step",
                event_data={"index": index},
            )
            store.record_cooperative_cancel_observed(
                job_id,
                attempt=attempt,
                message="Stopped between points",
            )
            return 0
        next_status = None
        if index == 0:
            next_status = "smoke_validated"
        store.update_state(
            job_id,
            next_status,
            patch={"progress": {"completed": index + 1, "total": len(delays)}},
            event="sequence_step",
            event_data={"index": index},
        )
        if index == 0 and len(delays) > 1:
            store.update_state(job_id, "running", event="broad_phase_started")
    store.update_state(job_id, "completed", event="completed")
    return 0


def run(root: str, job_id: str) -> int:
    try:
        return _run(root, job_id)
    except ValueError:
        store = JobStore(Path(root))
        state = store.read_state(job_id)
        if state.get("status") == "cancel_requested":
            store.record_cooperative_cancel_observed(
                job_id,
                attempt=int(state.get("attempt", 1)),
                message="Stopped between state transitions",
            )
            return 0
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(run(sys.argv[1], sys.argv[2]))
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise
