"""Solver-free fake worker used to exercise adjoint durable lifecycle contracts."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from comsol_mcp.durable import domain_sha256_v2

from .adjoint_rows import append_adjoint_row, read_adjoint_rows
from .process_control import contain_current_process_tree
from .store import JobStore, cancel_request_targets_attempt, process_identity


def run(root: str, job_id: str) -> int:
    store = JobStore(Path(root))
    directory = store.job_dir(job_id)
    spec = store.read_spec(job_id)
    if spec.get("job_type") != "adjoint_optimization" or not spec.get("synthetic_mode"):
        raise ValueError("adjoint fake worker accepts only synthetic_mode jobs")
    attempt = int(store.read_state(job_id).get("attempt", 1))
    store.bind_worker_identity(job_id, process_identity(os.getpid()))
    store.update_state(
        job_id,
        patch={"process_tree_contained": bool(contain_current_process_tree())},
        event="worker_containment_recorded",
    )
    state = store.read_state(job_id)
    if state["status"] == "cancel_requested" or cancel_request_targets_attempt(
        store.read_control(job_id), attempt
    ):
        store.record_cooperative_cancel_observed(
            job_id, attempt=attempt, message="Stopped before startup"
        )
        return 0
    if state["status"] == "submitted":
        store.update_state(job_id, "starting", event="worker_started")
    elif state["status"] != "starting":
        raise ValueError(f"adjoint fake worker cannot start from {state['status']}")
    store.update_state(job_id, "smoke_running", event="adjoint_synthetic_started")
    rows_path = directory / "optimization_rows.jsonl"
    existing = read_adjoint_rows(rows_path, job_fingerprint=spec["spec_fingerprint"])
    if not existing:
        gradient_fp = domain_sha256_v2("comsol_mcp.synthetic_gradient", {"values": [0.0]})
        check_fp = domain_sha256_v2("comsol_mcp.synthetic_gradient_check", {"passed": True})
        append_adjoint_row(
            rows_path,
            job_fingerprint=spec["spec_fingerprint"],
            attempt=attempt,
            kind="gradient",
            payload={
                "iteration_id": "it-0",
                "gradient_fingerprint": gradient_fp,
                "check_fingerprint": check_fp,
                "evidence_state": "gradient_validated",
            },
        )
        append_adjoint_row(
            rows_path,
            job_fingerprint=spec["spec_fingerprint"],
            attempt=attempt,
            kind="iteration",
            payload={
                "iteration_id": "it-0",
                "iteration_index": 0,
                "candidate_fingerprint": domain_sha256_v2(
                    "comsol_mcp.synthetic_candidate", {"values": spec["initial_values"]}
                ),
                "objective_value": 0.0,
                "status": "accepted",
                "gradient_fingerprint": gradient_fp,
                "forward_fingerprint": check_fp,
                "reason_code": "synthetic_contract_only",
            },
        )
    store.update_state(
        job_id,
        "completed",
        patch={"progress": {"completed": 1, "total": 1}, "solver_started": False},
        event="completed",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1], sys.argv[2]))
