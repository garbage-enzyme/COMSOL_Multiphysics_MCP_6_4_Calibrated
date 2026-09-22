"""Injected fake surrogate-training worker used to prove durability without COMSOL.

This worker performs no COMSOL call and claims no model quality.  It exists so
that the durable state machine, journal ordering, checkpointing, cancellation,
and no-duplicate-work guarantees can be exercised on Windows without a licensed
solver session.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from comsol_mcp.durable.io import append_jsonl_record, atomic_write_json, read_complete_jsonl

from .process_control import contain_current_process_tree
from .store import JobStore, cancel_request_targets_attempt, process_identity

MAX_EPOCH_ROWS = 10_000


def _epochs_path(root: Path, job_id: str) -> Path:
    return root / job_id / "epochs.jsonl"


def _read_completed_epochs(path: Path, *, total: int) -> set[int]:
    """Return the set of epochs already durably recorded for this attempt."""
    report = read_complete_jsonl(path)
    if report["state"] == "corrupt":
        raise ValueError("epoch journal is corrupt")
    completed: set[int] = set()
    for record in report["records"]:
        index = record.get("epoch")
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError("epoch journal record has an invalid epoch index")
        if not 0 <= index < total:
            raise ValueError("epoch journal record is outside the declared budget")
        completed.add(index)
    if len(completed) != len(report["records"]):
        raise ValueError("epoch journal contains duplicate epochs")
    return completed


def _run(root: str, job_id: str) -> int:
    process_tree_contained = contain_current_process_tree()
    store = JobStore(Path(root))
    spec = store.read_spec(job_id)
    if spec.get("job_type") != "surrogate_training":
        raise ValueError("Surrogate worker refuses non-surrogate jobs")
    identity = process_identity(os.getpid())
    store.bind_worker_identity(job_id, identity)
    store.update_state(
        job_id,
        patch={"process_tree_contained": bool(process_tree_contained)},
        event="worker_containment_recorded",
        event_data={"process_tree_contained": bool(process_tree_contained)},
    )
    initial_state = store.read_state(job_id)
    attempt = int(initial_state.get("attempt", 1))
    current = initial_state["status"]
    if current == "cancel_requested" or cancel_request_targets_attempt(
        store.read_control(job_id), attempt
    ):
        store.record_cooperative_cancel_observed(
            job_id, attempt=attempt, message="Stopped before startup"
        )
        return 0
    if current == "submitted":
        store.update_state(job_id, "starting", event="worker_started")
    elif current != "starting":
        raise ValueError(f"Surrogate worker cannot start from {current}")

    total_epochs = int(spec["maximum_epochs"])
    if total_epochs > MAX_EPOCH_ROWS:
        raise ValueError("declared epoch budget exceeds the surrogate worker limit")

    # Stage 1: the fake worker records the exact bound identities it will use so
    # that a resume can be proven to target the same contract.
    store.update_state(
        job_id,
        "smoke_running",
        patch={
            "progress": {"completed": 0, "total": total_epochs},
            "dataset_manifest_sha256": spec["dataset_manifest_sha256"],
            "split_manifest_sha256": spec["split_manifest_sha256"],
            "field_schema_sha256": spec["field_schema_sha256"],
            "transforms_sha256": spec["transforms_sha256"],
            "architecture_sha256": spec["architecture_sha256"],
        },
        event="surrogate_contract_bound",
        event_data={
            "dataset_manifest_sha256": spec["dataset_manifest_sha256"],
            "split_manifest_sha256": spec["split_manifest_sha256"],
        },
    )

    epochs_path = _epochs_path(store.root, job_id)
    completed = _read_completed_epochs(epochs_path, total=total_epochs)
    if completed:
        store.update_state(
            job_id,
            patch={"progress": {"completed": len(completed), "total": total_epochs}},
            event="durable_epochs_reconciled",
            event_data={"completed": sorted(completed)[-1] + 1},
        )
    if completed:
        store.update_state(job_id, "smoke_validated", event="surrogate_contract_revalidated")
        store.update_state(job_id, "running", event="training_resumed")

    for epoch in range(total_epochs):
        if epoch in completed:
            continue
        if cancel_request_targets_attempt(store.read_control(job_id), attempt):
            store.record_cooperative_cancel_observed(
                job_id, attempt=attempt, message="Stopped between epochs"
            )
            return 0
        # Durable epoch row is written and fsynced before progress publication,
        # so a crash can never publish progress the journal does not contain.
        append_jsonl_record(
            epochs_path,
            {
                "epoch": epoch,
                "seeds": list(spec["seeds"]),
                "loss": 1.0 / (epoch + 1),
            },
        )
        next_status = None
        if epoch == 0:
            next_status = "smoke_validated"
        store.update_state(
            job_id,
            next_status,
            patch={"progress": {"completed": epoch + 1, "total": total_epochs}},
            event="surrogate_epoch",
            event_data={"epoch": epoch},
        )
        if epoch == 0:
            store.update_state(job_id, "running", event="training_started")

    # Terminal receipt is published only after the epoch journal is complete.
    receipt = {
        "job_type": "surrogate_training",
        "campaign_id": spec["campaign_id"],
        "attempt": attempt,
        "completed_epochs": total_epochs,
        "seeds": list(spec["seeds"]),
        "dataset_manifest_sha256": spec["dataset_manifest_sha256"],
        "split_manifest_sha256": spec["split_manifest_sha256"],
        "field_schema_sha256": spec["field_schema_sha256"],
        "transforms_sha256": spec["transforms_sha256"],
        "architecture_sha256": spec["architecture_sha256"],
        "scientific_disposition": "predicted",
        "claims_model_quality": False,
        "claims_fem_evidence": False,
    }
    atomic_write_json(store.job_dir(job_id) / "terminal_receipt.json", receipt)
    store.update_state(
        job_id,
        "completed",
        event="completed",
        event_data={"completed_epochs": total_epochs},
    )
    time.sleep(0)
    return 0


def run(root: str, job_id: str) -> int:
    try:
        return _run(root, job_id)
    except ValueError as exc:
        store = JobStore(Path(root))
        state = store.read_state(job_id)
        if state.get("status") in {"cancel_requested", "cancelling"}:
            store.record_cooperative_cancel_observed(
                job_id,
                attempt=int(state.get("attempt", 1)),
                message="Stopped between state transitions",
                worker_error={
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "cleanup_errors": [],
                },
            )
            return 1
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(run(sys.argv[1], sys.argv[2]))
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise
