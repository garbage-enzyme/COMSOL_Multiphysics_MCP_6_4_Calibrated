"""Durable synthetic worker and licensed-runtime dispatch for robust shape jobs."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from comsol_mcp.durable import domain_sha256_v2
from comsol_mcp.research.robust_objectives import evaluate_robust_absolute_contrast
from comsol_mcp.research.robust_startup_admission import evaluate_robust_startup_admission

from .process_control import contain_current_process_tree
from .resource_admission import collect_resource_telemetry
from .robust_shape_rows import append_robust_shape_row, read_robust_shape_rows
from .store import JobStore, atomic_write_json, cancel_request_targets_attempt, process_identity


def _synthetic_observations(spec: dict) -> list[dict]:
    states = spec["objective"]["state_ids"]
    observations = []
    for row in spec["condition_table"]["conditions"]:
        if not row["active"] or row["objective_role"] != "objective":
            continue
        pair_index = row["order"] // len(states)
        value = 0.7 - pair_index * 0.0001
        if row["material_state_id"] == states[1]:
            value -= 0.2
        evidence = {
            "condition_id": row["condition_id"],
            "observable_id": row["observable_id"],
            "value": value,
            "disposition": "measured",
        }
        observations.append(
            {
                **evidence,
                "evidence_sha256": domain_sha256_v2(
                    "comsol_mcp.synthetic_robust_observation", evidence
                ),
            }
        )
    return observations


def _cancel_requested(store: JobStore, job_id: str, attempt: int) -> bool:
    state = store.read_state(job_id)
    return state["status"] == "cancel_requested" or cancel_request_targets_attempt(
        store.read_control(job_id), attempt
    )


def _record_synthetic_cancel(
    store: JobStore, job_id: str, spec: dict, attempt: int, message: str
) -> None:
    rows_path = store.job_dir(job_id) / "robust_shape_rows.jsonl"
    rows = read_robust_shape_rows(rows_path, job_fingerprint=spec["spec_fingerprint"])
    if not any(row["kind"] == "cleanup" for row in rows):
        append_robust_shape_row(
            rows_path,
            job_fingerprint=spec["spec_fingerprint"],
            attempt=attempt,
            kind="cleanup",
            payload={
                "source_unchanged": True,
                "client_clear": True,
                "owned_processes_absent": True,
                "lease_released": True,
                "cleanup_fingerprint": domain_sha256_v2(
                    "comsol_mcp.synthetic_robust_cancel_cleanup",
                    {"attempt": attempt, "message": message},
                ),
            },
        )
    store.record_cooperative_cancel_observed(job_id, attempt=attempt, message=message)


def _run_synthetic(root: str, job_id: str) -> int:
    store = JobStore(Path(root))
    directory = store.job_dir(job_id)
    spec = store.read_spec(job_id)
    attempt = int(store.read_state(job_id).get("attempt", 1))
    store.bind_worker_identity(job_id, process_identity(os.getpid()))
    store.update_state(
        job_id,
        patch={"process_tree_contained": bool(contain_current_process_tree())},
        event="worker_containment_recorded",
    )
    if _cancel_requested(store, job_id, attempt):
        _record_synthetic_cancel(
            store,
            job_id,
            spec,
            attempt,
            "Stopped before robust synthetic startup",
        )
        return 0
    state = store.read_state(job_id)
    if state["status"] == "submitted":
        store.update_state(job_id, "starting", event="worker_started")
    elif state["status"] != "starting":
        raise ValueError(f"robust shape worker cannot start from {state['status']}")
    telemetry = collect_resource_telemetry(stage="pre_mesh", runtime_path=directory)
    startup_admission = evaluate_robust_startup_admission(spec["startup_admission"], telemetry)
    atomic_write_json(directory / "startup-admission.json", startup_admission)
    if not startup_admission["ready"]:
        store.update_state(
            job_id,
            "failed",
            patch={
                "solver_started": False,
                "startup_admission_fingerprint": startup_admission["receipt_fingerprint"],
                "last_error": {
                    "type": "StartupResourceRefused",
                    "message": "Caller-declared startup RAM/disk admission refused the job",
                },
            },
            event="robust_startup_resource_refused",
        )
        return 1
    store.update_state(
        job_id,
        patch={"startup_admission_fingerprint": startup_admission["receipt_fingerprint"]},
        event="robust_startup_resource_admitted",
    )
    store.update_state(job_id, "smoke_running", event="robust_shape_synthetic_started")
    rows_path = directory / "robust_shape_rows.jsonl"
    existing = read_robust_shape_rows(rows_path, job_fingerprint=spec["spec_fingerprint"])
    completed = {
        row["payload"]["condition_id"]
        for row in existing
        if row["kind"] == "condition" and row["payload"]["status"] in {"completed", "skipped"}
    }
    observations = _synthetic_observations(spec)
    observation_by_id = {item["condition_id"]: item for item in observations}
    active_rows = [
        row
        for row in spec["condition_table"]["conditions"]
        if row["active"] and row["objective_role"] == "objective"
    ]
    for row in active_rows:
        if _cancel_requested(store, job_id, attempt):
            _record_synthetic_cancel(
                store,
                job_id,
                spec,
                attempt,
                "Stopped between robust conditions",
            )
            return 0
        if row["condition_id"] in completed:
            continue
        observation = observation_by_id[row["condition_id"]]
        append_robust_shape_row(
            rows_path,
            job_fingerprint=spec["spec_fingerprint"],
            attempt=attempt,
            kind="condition",
            payload={
                "iteration_id": "it-0",
                "condition_id": row["condition_id"],
                "condition_order": row["order"],
                "status": "completed",
                "observation_fingerprint": observation["evidence_sha256"],
                "objective_contribution": observation["value"],
                "reason_code": "synthetic_measured",
            },
        )
        completed.add(row["condition_id"])
        store.update_state(
            job_id,
            patch={"progress": {"completed": len(completed), "total": len(active_rows)}},
            event="robust_condition_completed",
        )

    existing = read_robust_shape_rows(rows_path, job_fingerprint=spec["spec_fingerprint"])
    kinds = {row["kind"] for row in existing}
    objective = evaluate_robust_absolute_contrast(
        spec["objective"], spec["condition_table"], observations
    )
    gradient_fp = domain_sha256_v2(
        "comsol_mcp.synthetic_robust_gradient", {"candidate": spec["initial_values"]}
    )
    acceptance_fp = domain_sha256_v2(
        "comsol_mcp.synthetic_robust_gradient_acceptance", {"passed": True}
    )
    if "gradient" not in kinds:
        append_robust_shape_row(
            rows_path,
            job_fingerprint=spec["spec_fingerprint"],
            attempt=attempt,
            kind="gradient",
            payload={
                "iteration_id": "it-0",
                "gradient_fingerprint": gradient_fp,
                "acceptance_fingerprint": acceptance_fp,
                "evidence_state": "gradient_validated",
            },
        )
    candidate_fp = domain_sha256_v2(
        "comsol_mcp.synthetic_robust_candidate", {"values": spec["initial_values"]}
    )
    if "iteration" not in kinds:
        append_robust_shape_row(
            rows_path,
            job_fingerprint=spec["spec_fingerprint"],
            attempt=attempt,
            kind="iteration",
            payload={
                "iteration_id": "it-0",
                "iteration_index": 0,
                "candidate_fingerprint": candidate_fp,
                "aggregate_objective": objective["smooth_worst_case_absolute_contrast"],
                "status": "accepted",
                "robust_objective_fingerprint": objective["receipt_fingerprint"],
                "fresh_forward_fingerprint": objective["receipt_fingerprint"],
                "reason_code": "synthetic_contract_only",
            },
        )
    if "checkpoint" not in kinds:
        append_robust_shape_row(
            rows_path,
            job_fingerprint=spec["spec_fingerprint"],
            attempt=attempt,
            kind="checkpoint",
            payload={
                "iteration_id": "it-0",
                "checkpoint_fingerprint": domain_sha256_v2(
                    "comsol_mcp.synthetic_robust_checkpoint", {"candidate": candidate_fp}
                ),
                "completed_condition_count": len(completed),
                "retained_model_disposition": spec["shape_policy"]["model_retention"]["mode"],
            },
        )
    if "cleanup" not in kinds:
        append_robust_shape_row(
            rows_path,
            job_fingerprint=spec["spec_fingerprint"],
            attempt=attempt,
            kind="cleanup",
            payload={
                "source_unchanged": True,
                "client_clear": True,
                "owned_processes_absent": True,
                "lease_released": True,
                "cleanup_fingerprint": domain_sha256_v2(
                    "comsol_mcp.synthetic_robust_cleanup", {"solver_started": False}
                ),
            },
        )
    final_rows = read_robust_shape_rows(rows_path, job_fingerprint=spec["spec_fingerprint"])
    store.update_state(job_id, "smoke_validated", event="robust_shape_synthetic_validated")
    store.update_state(
        job_id,
        "completed",
        patch={
            "progress": {"completed": len(active_rows), "total": len(active_rows)},
            "solver_started": False,
            "last_robust_shape_row_sha256": final_rows[-1]["row_sha256"],
            "robust_objective_fingerprint": objective["receipt_fingerprint"],
        },
        event="completed",
    )
    return 0


def run(root: str, job_id: str) -> int:
    store = JobStore(Path(root))
    spec = store.read_spec(job_id)
    if spec.get("job_type") != "robust_shape_optimization":
        raise ValueError("robust shape worker accepts only robust_shape_optimization jobs")
    if spec.get("synthetic_mode"):
        return _run_synthetic(root, job_id)
    raise RuntimeError("licensed robust shape runtime is not implemented until S3/S4")


if __name__ == "__main__":
    try:
        raise SystemExit(run(sys.argv[1], sys.argv[2]))
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise
