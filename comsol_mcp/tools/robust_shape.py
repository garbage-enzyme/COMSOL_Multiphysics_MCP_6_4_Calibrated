"""Experimental bounded public tools for durable robust shape workflows."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from comsol_mcp.durable import domain_sha256_v2

from .jobs import job_manager

_PLAN_SCHEMA = "comsol_mcp.robust_shape_plan_preview"
_SUMMARY_SCHEMA = "comsol_mcp.robust_shape_evidence_summary"
_VERIFY_SCHEMA = "comsol_mcp.robust_shape_evidence_verification"


def _submission(
    *,
    submission_manifest_path: str,
    submission_manifest_sha256: str,
    cores: int,
    version: str,
    max_mesh_elements: int,
    comsol_temporary_directory: str,
    condition_execution_limit: int | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "job_type": "robust_shape_optimization",
        "submission_manifest_path": submission_manifest_path,
        "submission_manifest_sha256": submission_manifest_sha256,
        "cores": cores,
        "version": version,
        "resource_policy": {"max_mesh_elements": max_mesh_elements},
        "comsol_temporary_directory": comsol_temporary_directory,
    }
    if condition_execution_limit is not None:
        body["condition_execution_limit"] = condition_execution_limit
    return body


def _preview_robust_shape(submission: dict[str, Any]) -> dict[str, Any]:
    from comsol_mcp.jobs.robust_shape_optimization import expand_robust_shape_manifest

    spec = expand_robust_shape_manifest(submission)
    conditions = [
        row
        for row in spec["condition_table"]["conditions"]
        if row["active"] and row["objective_role"] == "objective"
    ]
    budget = spec["native_optimizer"]["budget"]
    body = {
        "schema_name": _PLAN_SCHEMA,
        "schema_version": "1.0.0",
        "job_type": spec["job_type"],
        "spec_fingerprint": spec["spec_fingerprint"],
        "adapter_id": spec["adapter_binding"]["adapter_id"],
        "support_state": spec["support"]["support_state"],
        "required_products": list(spec["support"]["required_products"]),
        "entitlement": {
            "live_checked": False,
            "state": "declared_pending_submit_preflight",
        },
        "condition_inventory": {
            "declared": len(conditions),
            "execution_limit": spec.get("condition_execution_limit"),
            "table_fingerprint": spec["condition_table"]["condition_table_fingerprint"],
        },
        "variable_inventory": {
            "count": len(spec["support"]["variables"]),
            "ids": [item["variable_id"] for item in spec["support"]["variables"]],
        },
        "objective": {
            "kind": spec["objective"]["kind"],
            "direction": spec["objective"]["direction"],
            "configuration_fingerprint": spec["objective"]["objective_fingerprint"],
        },
        "cost_envelope": {
            "cores": budget["cores"],
            "max_solves": budget["max_solves"],
            "max_iterations": budget["max_iterations"],
            "max_wall_time_seconds": budget["max_wall_time_seconds"],
            "max_disk_bytes": budget["max_disk_bytes"],
            "max_mesh_elements_per_model": spec["resource_policy"]["rules"]["max_mesh_elements"],
        },
        "effects": {
            "submitted": False,
            "filesystem_modified": False,
            "solver_started": False,
            "source_model_mutated": False,
        },
    }
    return {**body, "preview_sha256": domain_sha256_v2(_PLAN_SCHEMA, body)}


def _submit_robust_shape(submission: dict[str, Any], *, manager: Any) -> dict[str, Any]:
    result: object = manager.submit(submission)
    if not isinstance(result, dict):
        raise TypeError("durable job authority returned an invalid response")
    return result


def _robust_job(manager: Any, job_id: str) -> tuple[dict[str, Any], dict[str, Any], Path]:
    spec = manager.store.read_spec(job_id)
    if spec.get("job_type") != "robust_shape_optimization":
        raise ValueError("job is not a robust shape optimization job")
    state = manager.store.read_state(job_id)
    return spec, state, manager.store.job_dir(job_id)


def _read_rows(spec: dict[str, Any], directory: Path) -> list[dict[str, Any]]:
    from comsol_mcp.jobs.robust_shape_rows import read_robust_shape_rows_readonly

    path = directory / "robust_shape_rows.jsonl"
    if not path.is_file():
        return []
    return read_robust_shape_rows_readonly(path, job_fingerprint=spec["spec_fingerprint"])


def _row_summary(row: dict[str, Any]) -> dict[str, Any]:
    payload = row["payload"]
    identity = None
    status = None
    for key in ("condition_id", "iteration_id", "trial_id"):
        if key in payload:
            identity = payload[key]
            break
    for key in ("status", "evidence_state"):
        if key in payload:
            status = payload[key]
            break
    return {
        "sequence": row["sequence"],
        "attempt": row["attempt"],
        "kind": row["kind"],
        "identity": identity,
        "status": status,
        "row_sha256": row["row_sha256"],
    }


def inspect_robust_shape_evidence(
    job_id: str,
    *,
    limit: int = 50,
    manager: Any = job_manager,
) -> dict[str, Any]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit must be an integer from 1 to 200")
    spec, state, directory = _robust_job(manager, job_id)
    rows = _read_rows(spec, directory)
    kinds = Counter(row["kind"] for row in rows)
    condition_rows = [row for row in rows if row["kind"] == "condition"]
    declared_ids = {
        row["condition_id"]
        for row in spec["condition_table"]["conditions"]
        if row["active"] and row["objective_role"] == "objective"
    }
    completed = {
        row["payload"]["condition_id"]
        for row in condition_rows
        if row["payload"]["status"] in {"completed", "skipped"}
        and row["payload"]["condition_id"] in declared_ids
    }
    declared = len(declared_ids)
    body = {
        "schema_name": _SUMMARY_SCHEMA,
        "schema_version": "1.0.0",
        "job_id": job_id,
        "job_status": state["status"],
        "attempt": state.get("attempt"),
        "spec_fingerprint": spec["spec_fingerprint"],
        "row_count": len(rows),
        "kind_counts": {kind: kinds[kind] for kind in sorted(kinds)},
        "condition_progress": {
            "declared": declared,
            "completed": len(completed),
            "pending": declared - len(completed),
        },
        "last_row_sha256": rows[-1]["row_sha256"] if rows else None,
        "recent_rows": [_row_summary(row) for row in rows[-limit:]],
        "redaction": {
            "artifact_paths_included": False,
            "material_values_included": False,
            "raw_errors_included": False,
        },
        "solver_started": False,
        "filesystem_modified": False,
    }
    return {**body, "summary_sha256": domain_sha256_v2(_SUMMARY_SCHEMA, body)}


def _spec_fingerprint_valid(spec: dict[str, Any]) -> bool:
    expected = spec.get("spec_fingerprint")
    body = {key: value for key, value in spec.items() if key != "spec_fingerprint"}
    actual = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return expected == actual


def verify_robust_shape_evidence(
    job_id: str,
    *,
    manager: Any = job_manager,
) -> dict[str, Any]:
    spec, state, directory = _robust_job(manager, job_id)
    rows = _read_rows(spec, directory)
    declared_rows = [
        row
        for row in spec["condition_table"]["conditions"]
        if row["active"] and row["objective_role"] == "objective"
    ]
    declared_ids = [row["condition_id"] for row in declared_rows]
    completed_rows = [
        row
        for row in rows
        if row["kind"] == "condition" and row["payload"]["status"] in {"completed", "skipped"}
    ]
    completed_ids = [row["payload"]["condition_id"] for row in completed_rows]
    completed_orders = [row["payload"]["condition_order"] for row in completed_rows]
    gradients = [row for row in rows if row["kind"] == "gradient"]
    accepted_iterations = [
        row for row in rows if row["kind"] == "iteration" and row["payload"]["status"] == "accepted"
    ]
    controls = (
        spec.get("adapter_configuration", {}).get("configuration", {}).get("condition_controls", {})
    )
    shape_application_required = controls.get("schema_version") == "1.5.0"
    finalists = [row for row in rows if row["kind"] == "finalist_validation"]
    checkpoints = [row for row in rows if row["kind"] == "checkpoint"]
    cleanup = [row for row in rows if row["kind"] == "cleanup"]
    terminal_cleanup = cleanup[-1:]
    checks = {
        "spec_fingerprint_valid": _spec_fingerprint_valid(spec),
        "row_chain_valid": True,
        "condition_ids_declared": set(completed_ids) <= set(declared_ids),
        "completed_conditions_unique": len(completed_ids) == len(set(completed_ids)),
        "completed_condition_order_valid": completed_orders == sorted(completed_orders),
        "all_conditions_complete": completed_ids == declared_ids,
        "validated_gradient_present": any(
            row["payload"]["evidence_state"] == "gradient_validated" for row in gradients
        ),
        "accepted_iterations_fresh_forward_bound": bool(accepted_iterations)
        and all(row["payload"]["fresh_forward_fingerprint"] for row in accepted_iterations),
        "accepted_iterations_shape_application_bound": (
            not shape_application_required
            or (
                bool(accepted_iterations)
                and all(
                    row["payload"]["shape_application_fingerprint"] for row in accepted_iterations
                )
            )
        ),
        "validated_finalist_present": any(
            row["payload"]["status"] == "validated" for row in finalists
        ),
        "checkpoint_present": bool(checkpoints),
        "cleanup_present": bool(cleanup),
        "cleanup_all_true": bool(terminal_cleanup)
        and all(
            all(
                row["payload"][key]
                for key in (
                    "source_unchanged",
                    "client_clear",
                    "owned_processes_absent",
                    "lease_released",
                )
            )
            for row in terminal_cleanup
        ),
    }
    complete = all(checks.values())
    terminal_consistent = state["status"] != "completed" or complete
    checks["terminal_state_consistent"] = terminal_consistent
    reason_codes = [key for key, passed in checks.items() if not passed]
    body = {
        "schema_name": _VERIFY_SCHEMA,
        "schema_version": "1.0.0",
        "job_id": job_id,
        "job_status": state["status"],
        "spec_fingerprint": spec["spec_fingerprint"],
        "row_count": len(rows),
        "last_row_sha256": rows[-1]["row_sha256"] if rows else None,
        "checks": checks,
        "verified_complete": not reason_codes,
        "reason_codes": reason_codes,
        "solver_started": False,
        "filesystem_modified": False,
    }
    return {**body, "verification_sha256": domain_sha256_v2(_VERIFY_SCHEMA, body)}


def _call(callback: Callable[[], dict[str, Any]], *, reason_code: str) -> dict[str, Any]:
    try:
        return callback()
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        return {
            "success": False,
            "reason_code": reason_code,
            "error_type": type(exc).__name__,
            "error": "The robust shape request was rejected by its bounded contract.",
            "solver_started": False,
        }


def register_robust_shape_tools(mcp: MCPServer) -> None:
    """Register the experimental bounded robust-shape workflow surface."""

    @mcp.tool()  # type: ignore[untyped-decorator]
    def robust_shape_plan_preview(
        submission_manifest_path: str,
        submission_manifest_sha256: str,
        cores: int,
        version: str,
        max_mesh_elements: int,
        comsol_temporary_directory: str,
        condition_execution_limit: int | None = None,
    ) -> dict[str, Any]:
        """Normalize one robust campaign without admission, mutation, or solver startup."""
        submission = _submission(
            submission_manifest_path=submission_manifest_path,
            submission_manifest_sha256=submission_manifest_sha256,
            cores=cores,
            version=version,
            max_mesh_elements=max_mesh_elements,
            comsol_temporary_directory=comsol_temporary_directory,
            condition_execution_limit=condition_execution_limit,
        )
        return _call(
            lambda: {"success": True, "preview": _preview_robust_shape(submission)},
            reason_code="robust_shape_preview_rejected",
        )

    @mcp.tool()  # type: ignore[untyped-decorator]
    def robust_shape_job_submit(
        submission_manifest_path: str,
        submission_manifest_sha256: str,
        cores: int,
        version: str,
        max_mesh_elements: int,
        comsol_temporary_directory: str,
        condition_execution_limit: int | None = None,
    ) -> dict[str, Any]:
        """Submit one normalized robust job through the durable job authority."""
        submission = _submission(
            submission_manifest_path=submission_manifest_path,
            submission_manifest_sha256=submission_manifest_sha256,
            cores=cores,
            version=version,
            max_mesh_elements=max_mesh_elements,
            comsol_temporary_directory=comsol_temporary_directory,
            condition_execution_limit=condition_execution_limit,
        )
        return _call(
            lambda: _submit_robust_shape(submission, manager=job_manager),
            reason_code="robust_shape_submit_rejected",
        )

    @mcp.tool()  # type: ignore[untyped-decorator]
    def robust_shape_evidence_inspect(job_id: str, limit: int = 50) -> dict[str, Any]:
        """Return a bounded path-redacted summary of one robust evidence journal."""
        return _call(
            lambda: {
                "success": True,
                "summary": inspect_robust_shape_evidence(job_id, limit=limit),
            },
            reason_code="robust_shape_evidence_inspection_rejected",
        )

    @mcp.tool()  # type: ignore[untyped-decorator]
    def robust_shape_evidence_verify(job_id: str) -> dict[str, Any]:
        """Verify robust evidence identity, ordering, completeness, and cleanup."""
        return _call(
            lambda: {
                "success": True,
                "verification": verify_robust_shape_evidence(job_id),
            },
            reason_code="robust_shape_evidence_verification_rejected",
        )


__all__ = [
    "inspect_robust_shape_evidence",
    "register_robust_shape_tools",
    "verify_robust_shape_evidence",
]
