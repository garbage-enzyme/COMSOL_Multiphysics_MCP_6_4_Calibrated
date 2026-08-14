"""Durable synthetic worker and licensed-runtime dispatch for robust shape jobs."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from comsol_mcp.durable import domain_sha256_v2
from comsol_mcp.research.robust_finalist_evidence import assess_robust_finalist_validation
from comsol_mcp.research.robust_objectives import evaluate_robust_absolute_contrast
from comsol_mcp.research.robust_startup_admission import evaluate_robust_startup_admission

from .process_control import contain_current_process_tree
from .resource_admission import collect_resource_telemetry
from .robust_shape_rows import append_robust_shape_row, read_robust_shape_rows
from .store import (
    JobStore,
    atomic_write_json,
    cancel_request_targets_attempt,
    process_identity,
    read_json,
)


def _synthetic_observations(spec: dict[str, Any]) -> list[dict[str, Any]]:
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


def _synthetic_finalist_evidence(
    spec: dict[str, Any], candidate: str, objective_value: float
) -> dict[str, Any]:
    """Build clearly synthetic evidence for the solver-free durable contract path."""
    shape = spec["shape_policy"]
    finalist = spec["finalist_validation_policy"]
    mesh = finalist["mesh_convergence"]
    baseline_count = max(1, mesh["max_elements_per_model"] // 2)
    finer_count = mesh["max_elements_per_model"]
    quality = mesh["minimum_element_quality"]
    objective_configuration = domain_sha256_v2(
        "comsol_mcp.synthetic_robust_objective_configuration", spec["objective"]
    )
    off_design_rows = []
    for condition in spec["condition_table"]["conditions"]:
        if not condition["active"] or condition["objective_role"] != "objective":
            continue
        for axis, offsets in (
            ("wavelength_relative", finalist["off_design"]["wavelength_relative_offsets"]),
            ("angle_deg", finalist["off_design"]["angle_offsets_deg"]),
        ):
            for offset in offsets:
                body = {
                    "base_condition_id": condition["condition_id"],
                    "axis": axis,
                    "offset": offset,
                    "status": "measured",
                    "objective_value": objective_value,
                }
                off_design_rows.append(
                    {
                        **body,
                        "evidence_sha256": domain_sha256_v2(
                            "comsol_mcp.synthetic_robust_off_design", body
                        ),
                    }
                )
    branch_policy = finalist["branch_guard"]
    branch_required = branch_policy["mode"] == "required"
    branch_id = "synthetic-branch" if branch_required else None
    branch_order = 0 if branch_required else None
    branch_body = {
        "mode": branch_policy["mode"],
        "observable_id": branch_policy["observable_id"],
        "baseline_branch_id": branch_id,
        "finer_branch_id": branch_id,
        "baseline_mode_order": branch_order,
        "finer_mode_order": branch_order,
        "ambiguous": False,
        "disappeared": False,
    }
    return {
        "candidate_fingerprint": candidate,
        "optimizer_execution_fingerprint": domain_sha256_v2(
            "comsol_mcp.synthetic_robust_optimizer_execution", {"candidate": candidate}
        ),
        "manufacturability": {
            "shape_policy_fingerprint": shape["policy_fingerprint"],
            "minimum_gap_m": shape["minimum_gap"]["effective_value_m"],
            "minimum_thickness_m": shape["geometry_guards"]["minimum_thickness_m"],
            "minimum_radius_m": shape["geometry_guards"]["minimum_radius_m"],
            "topology_preserved": True,
            "selections_preserved": True,
            "positive_dimensions": True,
            "self_intersection_absent": True,
            "evidence_sha256": domain_sha256_v2(
                "comsol_mcp.synthetic_robust_manufacturability", {"candidate": candidate}
            ),
        },
        "fresh_remesh": {
            "candidate_fingerprint": candidate,
            "explicit_rebuild": True,
            "optimizer_state_reused": False,
            "model_sha256": domain_sha256_v2(
                "comsol_mcp.synthetic_robust_fresh_model", {"candidate": candidate}
            ),
            "mesh_sha256": domain_sha256_v2(
                "comsol_mcp.synthetic_robust_fresh_mesh", {"candidate": candidate}
            ),
            "element_count": baseline_count,
            "minimum_element_quality": quality,
            "quality_measure": mesh["quality_measure"],
            "objective_evidence_sha256": domain_sha256_v2(
                "comsol_mcp.synthetic_robust_fresh_objective", {"value": objective_value}
            ),
            "evidence_sha256": domain_sha256_v2(
                "comsol_mcp.synthetic_robust_fresh_remesh", {"candidate": candidate}
            ),
        },
        "mesh_convergence": {
            "levels": [
                {
                    "level_id": mesh["baseline_level_id"],
                    "candidate_fingerprint": candidate,
                    "model_sha256": domain_sha256_v2(
                        "comsol_mcp.synthetic_robust_baseline_model", {"candidate": candidate}
                    ),
                    "mesh_sha256": domain_sha256_v2(
                        "comsol_mcp.synthetic_robust_baseline_mesh", {"candidate": candidate}
                    ),
                    "element_count": baseline_count,
                    "minimum_element_quality": quality,
                    "quality_measure": mesh["quality_measure"],
                    "objective_configuration_fingerprint": objective_configuration,
                    "objective_value": objective_value,
                    "objective_evidence_sha256": domain_sha256_v2(
                        "comsol_mcp.synthetic_robust_baseline_objective",
                        {"value": objective_value},
                    ),
                },
                {
                    "level_id": mesh["finer_level_id"],
                    "candidate_fingerprint": candidate,
                    "model_sha256": domain_sha256_v2(
                        "comsol_mcp.synthetic_robust_finer_model", {"candidate": candidate}
                    ),
                    "mesh_sha256": domain_sha256_v2(
                        "comsol_mcp.synthetic_robust_finer_mesh", {"candidate": candidate}
                    ),
                    "element_count": finer_count,
                    "minimum_element_quality": quality,
                    "quality_measure": mesh["quality_measure"],
                    "objective_configuration_fingerprint": objective_configuration,
                    "objective_value": objective_value,
                    "objective_evidence_sha256": domain_sha256_v2(
                        "comsol_mcp.synthetic_robust_finer_objective",
                        {"value": objective_value},
                    ),
                },
            ],
            "evidence_sha256": domain_sha256_v2(
                "comsol_mcp.synthetic_robust_mesh_convergence", {"candidate": candidate}
            ),
        },
        "branch_guard": {
            **branch_body,
            "evidence_sha256": (
                domain_sha256_v2("comsol_mcp.synthetic_robust_branch", branch_body)
                if branch_required
                else None
            ),
        },
        "off_design_rows": off_design_rows,
        "external_validation_receipt": None,
    }


def _cancel_requested(store: JobStore, job_id: str, attempt: int) -> bool:
    state = store.read_state(job_id)
    return bool(
        state["status"] == "cancel_requested"
        or cancel_request_targets_attempt(store.read_control(job_id), attempt)
    )


def _finalize_licensed_cleanup(
    directory: Path,
    *,
    ownership: Any,
    lease_acquired: bool,
    native_runtime_entered: bool,
    source_unchanged: bool,
) -> dict[str, Any]:
    """Collect observed native, lease, and process cleanup without asserting success."""
    errors: list[str] = []
    client_clear = not native_runtime_entered
    native_path = directory / "native-cleanup.json"
    native_fingerprint = None
    if native_runtime_entered:
        try:
            native = read_json(native_path)
            client_clear = bool(
                native.get("client_clear") is True
                and native.get("source_model_removed") is True
                and native.get("working_model_removed") is True
                and native.get("client_disconnect") in {True, "not_applicable"}
                and not native.get("errors")
            )
            if native.get("errors"):
                errors.append("native_cleanup:reported_errors")
            native_fingerprint = domain_sha256_v2(
                "comsol_mcp.robust_native_cleanup_receipt", native
            )
        except Exception as exc:
            errors.append(f"native_cleanup_receipt:{type(exc).__name__}")

    release_succeeded = not lease_acquired
    if lease_acquired and ownership is not None:
        try:
            released = ownership.release()
            release_succeeded = bool(
                released.get("success") is True and released.get("released") is True
            )
            if not release_succeeded:
                errors.append("lease_release:unproved")
        except Exception as exc:
            errors.append(f"lease_release:{type(exc).__name__}")

    lease_absent = not lease_acquired and ownership is None
    owned_processes_absent = not native_runtime_entered and ownership is None
    inventory_fingerprint = None
    if ownership is not None:
        try:
            status = ownership.status(require_fresh_inventory=True)
            inventory = status.get("process_inventory", {})
            lease_absent = status.get("lease", {}).get("state") == "absent"
            owned_processes_absent = bool(
                inventory.get("complete") is True
                and not status.get("external_solver_processes")
            )
            inventory_fingerprint = domain_sha256_v2(
                "comsol_mcp.robust_cleanup_ownership_status",
                {
                    "inventory": inventory,
                    "lease": status.get("lease"),
                    "external_solver_processes": status.get("external_solver_processes"),
                },
            )
            if not owned_processes_absent:
                errors.append("owned_processes_absent:unproved")
            if not lease_absent:
                errors.append("lease_absent:unproved")
        except Exception as exc:
            errors.append(f"cleanup_inventory:{type(exc).__name__}")

    payload = {
        "source_unchanged": source_unchanged,
        "client_clear": client_clear,
        "owned_processes_absent": owned_processes_absent,
        "lease_released": bool(release_succeeded and lease_absent),
        "native_cleanup_fingerprint": native_fingerprint,
        "inventory_fingerprint": inventory_fingerprint,
        "errors": errors,
    }
    receipt = {
        "schema_name": "comsol_mcp.robust_licensed_cleanup_receipt",
        "schema_version": "1.0.0",
        **payload,
    }
    receipt["receipt_fingerprint"] = domain_sha256_v2(
        "comsol_mcp.robust_licensed_cleanup_receipt", receipt
    )
    atomic_write_json(directory / "licensed-cleanup.json", receipt)
    return {
        key: payload[key]
        for key in (
            "source_unchanged",
            "client_clear",
            "owned_processes_absent",
            "lease_released",
        )
    } | {"cleanup_fingerprint": receipt["receipt_fingerprint"]}


def _record_synthetic_cancel(
    store: JobStore, job_id: str, spec: dict[str, Any], attempt: int, message: str
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
    finalist_receipt = assess_robust_finalist_validation(
        spec["finalist_validation_policy"],
        spec["condition_table"],
        spec["shape_policy"],
        _synthetic_finalist_evidence(
            spec,
            candidate_fp,
            objective["smooth_worst_case_absolute_contrast"],
        ),
    )
    atomic_write_json(directory / "finalist-validation.json", finalist_receipt)
    if "finalist_validation" not in kinds:
        append_robust_shape_row(
            rows_path,
            job_fingerprint=spec["spec_fingerprint"],
            attempt=attempt,
            kind="finalist_validation",
            payload={
                "iteration_id": "it-0",
                "candidate_fingerprint": candidate_fp,
                "policy_fingerprint": finalist_receipt["policy_fingerprint"],
                "receipt_fingerprint": finalist_receipt["receipt_fingerprint"],
                "status": finalist_receipt["disposition"],
                "reason_codes": finalist_receipt["reason_codes"],
            },
        )
    if not finalist_receipt["accepted"]:
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
                    "comsol_mcp.synthetic_robust_rejected_cleanup",
                    {"finalist": finalist_receipt["receipt_fingerprint"]},
                ),
            },
        )
        store.update_state(
            job_id,
            "failed",
            patch={
                "solver_started": False,
                "finalist_validation_fingerprint": finalist_receipt["receipt_fingerprint"],
                "last_error": {
                    "type": "FinalistValidationRejected",
                    "message": "Synthetic finalist evidence did not satisfy caller policy",
                },
            },
            event="robust_finalist_validation_rejected",
        )
        return 1
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
            "finalist_validation_fingerprint": finalist_receipt["receipt_fingerprint"],
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
    return _run_licensed(root, job_id)


def _run_licensed(root: str, job_id: str) -> int:
    """Run the explicit licensed condition phase and fail closed before optimization."""
    store = JobStore(Path(root))
    directory = store.job_dir(job_id)
    spec = store.read_spec(job_id)
    attempt = int(store.read_state(job_id).get("attempt", 1))
    ownership = None
    lease_acquired = False
    native_runtime_entered = False
    source = Path(spec["source_model_path"])
    source_before = source.read_bytes()
    try:
        store.bind_worker_identity(job_id, process_identity(os.getpid()))
        store.update_state(
            job_id,
            patch={"process_tree_contained": bool(contain_current_process_tree())},
            event="worker_containment_recorded",
        )
        state = store.read_state(job_id)
        if _cancel_requested(store, job_id, attempt):
            _record_synthetic_cancel(
                store, job_id, spec, attempt, "Stopped before licensed startup"
            )
            return 0
        if state["status"] == "submitted":
            store.update_state(job_id, "starting", event="worker_started")
        elif state["status"] != "starting":
            raise ValueError(f"licensed robust shape worker cannot start from {state['status']}")
        telemetry = collect_resource_telemetry(stage="pre_mesh", runtime_path=directory)
        admission = evaluate_robust_startup_admission(spec["startup_admission"], telemetry)
        atomic_write_json(directory / "startup-admission.json", admission)
        if not admission["ready"]:
            store.update_state(
                job_id,
                "failed",
                patch={"solver_started": False, "last_error": {"type": "StartupResourceRefused"}},
                event="robust_startup_resource_refused",
            )
            return 1
        from comsol_mcp.tools.ownership import SolverOwnership

        ownership = SolverOwnership(store.root.parent, owner=f"job:{job_id}")
        preflight = ownership.preflight(
            model_path=str(source),
            output_path=str(directory / "robust-working.mph"),
            requested_version=spec["version"],
        )
        if not preflight.get("ready"):
            raise RuntimeError("licensed robust shape preflight was not ready")
        claim = ownership.acquire(mode="robust-shape-condition", model_path=str(source))
        if not claim.get("success") or not claim.get("acquired"):
            raise RuntimeError("licensed robust shape solver ownership could not be acquired")
        lease_acquired = True
        store.update_state(job_id, "smoke_running", event="robust_shape_licensed_started")
        adapter_id = spec["adapter_binding"]["adapter_id"]
        if adapter_id != "lin2025_pedot_cylinder_v1":
            raise RuntimeError(f"licensed robust adapter dispatch is unsupported: {adapter_id}")
        from .robust_shape_native_runtime import execute_lin2025_conditions

        native_runtime_entered = True
        result = execute_lin2025_conditions(
            spec,
            directory,
            attempt=attempt,
            cancel_requested=lambda: _cancel_requested(store, job_id, attempt),
        )
        objective = evaluate_robust_absolute_contrast(
            spec["objective"], spec["condition_table"], result["observations"]
        )
        rows_path = directory / "robust_shape_rows.jsonl"
        append_robust_shape_row(
            rows_path,
            job_fingerprint=spec["spec_fingerprint"],
            attempt=attempt,
            kind="iteration",
            payload={
                "iteration_id": "it-0",
                "iteration_index": 0,
                "candidate_fingerprint": domain_sha256_v2(
                    "comsol_mcp.robust_licensed_baseline_candidate", spec["initial_values"]
                ),
                "aggregate_objective": objective["smooth_worst_case_absolute_contrast"],
                "status": "rejected",
                "robust_objective_fingerprint": objective["receipt_fingerprint"],
                "fresh_forward_fingerprint": objective["receipt_fingerprint"],
                "reason_code": "native_shape_optimizer_pending",
            },
        )
        store.update_state(
            job_id,
            "failed",
            patch={
                "solver_started": True,
                "progress": {
                    "completed": len(result["observations"]),
                    "total": len(spec["condition_table"]["conditions"]),
                },
                "last_error": {
                    "type": "NativeRobustOptimizerPending",
                    "message": (
                        "Licensed condition phase completed; native robust optimizer "
                        "is not yet wired"
                    ),
                },
            },
            event="robust_condition_phase_completed",
        )
        return 1
    except Exception as exc:
        state = store.read_state(job_id)
        if state["status"] not in {"completed", "failed", "cancel_requested", "cancelling"}:
            store.update_state(
                job_id,
                "failed",
                patch={
                    "solver_started": bool(lease_acquired),
                    "last_error": {"type": type(exc).__name__, "message": str(exc)[:512]},
                },
                event="robust_licensed_worker_failed",
            )
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        source_unchanged = source.exists() and source.read_bytes() == source_before
        cleanup_payload = _finalize_licensed_cleanup(
            directory,
            ownership=ownership,
            lease_acquired=lease_acquired,
            native_runtime_entered=native_runtime_entered,
            source_unchanged=source_unchanged,
        )
        rows_path = directory / "robust_shape_rows.jsonl"
        try:
            rows = read_robust_shape_rows(
                rows_path, job_fingerprint=spec["spec_fingerprint"]
            )
            if not any(row["kind"] == "cleanup" for row in rows):
                append_robust_shape_row(
                    rows_path,
                    job_fingerprint=spec["spec_fingerprint"],
                    attempt=attempt,
                    kind="cleanup",
                    payload=cleanup_payload,
                )
        except Exception as cleanup_exc:
            print(
                f"robust cleanup evidence failed: {type(cleanup_exc).__name__}",
                file=sys.stderr,
                flush=True,
            )
        if not source_unchanged:
            print(
                "licensed robust worker detected immutable source drift",
                file=sys.stderr,
                flush=True,
            )


if __name__ == "__main__":
    try:
        raise SystemExit(run(sys.argv[1], sys.argv[2]))
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise
