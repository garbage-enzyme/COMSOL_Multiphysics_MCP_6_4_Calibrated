"""MCP control-plane tools for durable reference-power background jobs."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from comsol_mcp.contracts import JobSubmissionSpec, validate_job_submission
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
        "server_identity_sha256": (backend.get("attached_server") or {}).get(
            "identity_sha256"
        ),
        "model_identity_sha256": (backend.get("model") or {}).get(
            "identity_sha256"
        ),
        "external_resources_preserved": detach.get(
            "external_resources_preserved"
        ),
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
        raise ValueError(
            "attached execution is currently supported only for staged_sweep jobs"
        )
    from comsol_mcp.jobs.attached_backend import normalize_attached_execution_request
    from comsol_mcp.jobs.manager import validate_staged_sweep_spec
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
        user_confirmed_automation_exclusive=(
            request["user_confirmed_automation_exclusive"]
        ),
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
        return {
            "success": False,
            "state": "job_submit_failed_after_attached_handoff",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "attached_handoff": _attached_handoff_summary(handoff),
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
        """Cancel one owned durable job; terminal cancelled requires verified process, port, and lease cleanup."""
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
