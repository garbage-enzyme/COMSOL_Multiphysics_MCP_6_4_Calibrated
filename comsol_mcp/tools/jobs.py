"""MCP control-plane tools for durable reference-power background jobs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from comsol_mcp.contracts import JobSubmissionSpec, validate_job_submission
from comsol_mcp.durable import domain_sha256_v2
from comsol_mcp.utils.control_plane import measured_call


class _LazyJobManager:
    """Load the durable worker stack only when a job operation is called."""

    def __init__(self) -> None:
        self._manager: Any = None

    def _get(self) -> Any:
        if self._manager is None:
            from comsol_mcp.jobs.manager import JobManager

            self._manager = JobManager()
        return self._manager

    def __getattr__(self, name: str) -> Any:
        return getattr(self._get(), name)


job_manager: Any = _LazyJobManager()


def _job_point_inventory(spec: dict[str, Any]) -> dict[str, Any]:
    job_type = spec["job_type"]
    if job_type == "staged_sweep":
        return {
            "maximum_points": len(spec["parameter_values"]),
            "stages": ["smoke", "full"],
            "parameter": spec["parameter_name"],
            "expression_count": len(spec["expressions"]),
        }
    if job_type == "validation_matrix":
        return {
            "maximum_points": spec["point_limit"],
            "declared_points": len(spec["points"]),
            "stages": ["matrix"],
        }
    if job_type == "spectral_characterization":
        return {
            "maximum_points": spec["maximum_points"],
            "stages": ["initial", "refinement", "expansion"],
        }
    if job_type == "thermo_optomechanical_replay":
        return {
            "maximum_points": 5,
            "declared_items": 5,
            "stages": [
                "preflight",
                "thermal_structural_solve",
                "state_evidence",
                "deformation_transfer",
                "optical_replay",
            ],
            "declared_optical_points": len(spec["optical_replay"]["wavelengths_m"])
            * len(spec["optical_replay"]["branches"]),
            "control_count": len(spec["validation_controls"]),
        }
    return {
        "maximum_points": spec["maximum_total_points"],
        "declared_items": len(
            spec["levels"] if job_type == "convergence_campaign" else spec["states"]
        ),
        "stages": ["campaign"],
    }


def _preview_job_spec(spec: JobSubmissionSpec | dict[str, Any]) -> dict[str, Any]:
    normalized = validate_job_submission(spec)
    from comsol_mcp.tools.catalog import get_tool_metadata

    source_path = normalized.get("source_model_path")
    manifest_path = normalized.get("submission_manifest_path")
    path_checks: dict[str, Any]
    if source_path is None:
        path_checks = {"source_model_declared": False, "source_model_required": False}
    else:
        source = Path(source_path).expanduser()
        path_checks = {
            "source_model_declared": True,
            "source_model_required": True,
            "absolute": source.is_absolute(),
            "mph_extension": source.suffix.casefold() == ".mph",
            "exists": source.is_file(),
        }
    cores = normalized.get("cores")
    resource_policy = normalized.get("resource_policy")
    execution_backend = normalized.get("execution_backend")
    body = {
        "success": True,
        "schema_name": "comsol_mcp.job_spec_preview",
        "schema_version": "1.0.0",
        "job_type": normalized["job_type"],
        "spec_fingerprint": domain_sha256_v2("job_spec_preview/spec/1.0.0", normalized),
        "inventory": _job_point_inventory(normalized),
        "path_checks": path_checks,
        "submission_manifest": {
            "declared": manifest_path is not None,
            "hash_verified": normalized.get("submission_manifest_sha256") is not None,
            "read_only": True,
        },
        "resource_policy": {
            "declared": resource_policy is not None,
            "requested_cores": cores,
            "wall_time_budget_seconds": normalized.get("wall_time_budget_seconds"),
        },
        "requirements": {
            "submit_tool": "job_submit",
            "submit_profiles": list(get_tool_metadata("job_submit").intended_profiles),
            "source_model_read": source_path is not None,
            "submission_manifest_read": manifest_path is not None,
            "licensed_comsol_on_submit": True,
        },
        "declared_submission_side_effects": [
            "durable_job_filesystem_write",
            "worker_process_start",
            "licensed_solver_may_start",
        ],
        "execution_mode": {
            "declared": execution_backend is not None,
            "compatibility_assessed": False,
            "recommendation_made": False,
        },
        "preview_guarantees": {
            "submitted": False,
            "admission_checked": False,
            "solver_ownership_checked": False,
            "solve_success_implied": False,
            "solver_started": False,
            "filesystem_modified": False,
        },
    }
    return {**body, "preview_sha256": domain_sha256_v2("job_spec_preview/1.0.0", body)}


def __getattr__(name: str) -> Any:
    """Preserve the historical JobManager module attribute lazily.

    Control-plane discovery must not import the durable worker stack, while
    existing callers that construct ``comsol_mcp.tools.jobs.JobManager`` continue to
    receive the real implementation on demand.
    """
    if name == "JobManager":
        from comsol_mcp.jobs.manager import JobManager

        return JobManager
    raise AttributeError(name)


def _attached_handoff_summary(value: dict[str, Any]) -> dict[str, Any]:
    backend = value.get("execution_backend") or {}
    detach = value.get("detach") or {}
    return {
        "state": value.get("state"),
        "backend_identity_sha256": backend.get("backend_identity_sha256"),
        "server_identity_sha256": (backend.get("attached_server") or {}).get("identity_sha256"),
        "model_identity_sha256": (backend.get("model") or {}).get("identity_sha256"),
        "external_resources_preserved": detach.get("external_resources_preserved"),
        "detach_state": detach.get("state"),
    }


def _submit_job(
    spec: JobSubmissionSpec | dict[str, Any],
    *,
    manager: Any = job_manager,
    session_manager: Any = None,
) -> dict[str, Any]:
    spec = validate_job_submission(spec)
    execution_request = spec.get("execution_backend")
    if execution_request is None:
        return manager.submit(spec)
    if spec.get("job_type") != "staged_sweep":
        raise ValueError("attached execution is currently supported only for staged_sweep jobs")
    from comsol_mcp.jobs.attached_backend import normalize_attached_execution_request
    from comsol_mcp.jobs.manager import JobLaunchError, validate_staged_sweep_spec
    from comsol_mcp.tools.shared_session import shared_session_manager

    request = normalize_attached_execution_request(execution_request)
    session_manager = session_manager or shared_session_manager
    standalone_fields = dict(spec)
    standalone_fields.pop("execution_backend")
    validated = validate_staged_sweep_spec(standalone_fields)
    handoff = session_manager.prepare_attached_job_handoff(
        expected_lock_sha256=request["expected_lock_sha256"],
        expected_revision_sha256=request["expected_revision_sha256"],
        source_model_path=validated["source_model_path"],
        user_confirmed_automation_exclusive=(request["user_confirmed_automation_exclusive"]),
    )
    if not handoff.get("success"):
        return {
            "success": False,
            "state": "attached_handoff_failed",
            "attached_handoff": _attached_handoff_summary(handoff),
        }
    expanded = dict(spec)
    expanded["execution_backend"] = handoff["execution_backend"]
    try:
        submitted = manager.submit(expanded)
    except Exception as exc:
        if isinstance(exc, JobLaunchError):
            recovery = {
                "success": False,
                "state": "durable_job_requires_reconciliation",
                "job_id": exc.job_id,
                "action": "inspect_job_status_before_reclaiming_attached_session",
            }
            if exc.state_record_error is not None:
                recovery["state_record_error"] = exc.state_record_error
        else:
            recover = getattr(session_manager, "recover_attached_job_handoff", None)
            if callable(recover):
                try:
                    recovery = recover(handoff["execution_backend"])
                except Exception as recovery_exc:
                    recovery = {
                        "success": False,
                        "state": "attached_handoff_recovery_failed",
                        "error_type": type(recovery_exc).__name__,
                        "error": str(recovery_exc),
                    }
            else:
                recovery = {
                    "success": False,
                    "state": "attached_handoff_recovery_unavailable",
                }
        return {
            "success": False,
            "state": "job_submit_failed_after_attached_handoff",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "attached_handoff": _attached_handoff_summary(handoff),
            "handoff_recovery": recovery,
        }
    return {
        **submitted,
        "attached_handoff": _attached_handoff_summary(handoff),
    }


def _job_call(operation: str, callback, **error_fields: Any) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        try:
            return callback()
        except Exception as exc:
            return {
                "success": False,
                **error_fields,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }

    return measured_call(operation, run)


def register_job_tools(mcp: FastMCP) -> None:
    """Register durable submit/status/tail/cooperative-cancel/resume tools."""

    @mcp.tool()
    def job_submit(spec: JobSubmissionSpec) -> dict[str, Any]:
        """Validate and detach one bounded standalone or attached durable job."""
        return _job_call("job_submit", lambda: _submit_job(spec))

    @mcp.tool()
    def job_spec_preview(spec: JobSubmissionSpec) -> dict[str, Any]:
        """Validate one durable job input without admission, submission, or solver startup."""
        return _job_call("job_spec_preview", lambda: _preview_job_spec(spec))

    @mcp.tool()
    def job_status(job_id: str) -> dict[str, Any]:
        """Read and reconcile durable job state without starting COMSOL."""
        return _job_call(
            "job_status",
            lambda: job_manager.status(job_id),
            job_id=job_id,
        )

    @mcp.tool()
    def job_tail(job_id: str, n: int = 20) -> dict[str, Any]:
        """Return at most 200 trailing event and worker-log lines without solver side effects."""
        return _job_call(
            "job_tail",
            lambda: job_manager.tail(job_id, n),
            job_id=job_id,
        )

    @mcp.tool()
    def job_cancel(job_id: str) -> dict[str, Any]:
        """Cancel one owned job; terminal cancellation requires verified cleanup."""
        return _job_call(
            "job_cancel",
            lambda: job_manager.cancel(job_id),
            job_id=job_id,
        )

    @mcp.tool()
    def job_resume(job_id: str) -> dict[str, Any]:
        """Resume one failed/interrupted/cancelled job with unchanged immutable evidence."""
        return _job_call(
            "job_resume",
            lambda: job_manager.resume(job_id),
            job_id=job_id,
        )
