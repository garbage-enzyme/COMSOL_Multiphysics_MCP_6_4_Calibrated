"""MCP control-plane tools for durable reference-power background jobs."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from src.jobs.manager import JobManager
from src.utils.control_plane import measured_call


job_manager = JobManager()


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
    def job_submit(spec: dict[str, Any]) -> dict[str, Any]:
        """Validate and detach one bounded sweep, evidence matrix, spectrum, convergence, or continuation campaign."""
        return _job_call("job_submit", lambda: job_manager.submit(spec))

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
