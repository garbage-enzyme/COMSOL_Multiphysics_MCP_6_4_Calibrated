"""Route Tasks through the same registered guards as ordinary public jobs."""

from __future__ import annotations

from typing import Any

from .tasks_bridge import TasksMappingError


class ProfiledTasksEngine:
    def __init__(self) -> None:
        self.server: Any = None

    def bind(self, server: Any) -> None:
        self.server = server

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self.server._tool_manager._tools.get(name) if self.server is not None else None
        if tool is None:
            raise TasksMappingError(
                "tool_unavailable", "the active profile does not expose this job action"
            )
        result = tool.fn(**arguments)
        if not isinstance(result, dict) or result.get("success") is False:
            code = (
                result.get("reason_code", "job_action_refused")
                if isinstance(result, dict)
                else "job_action_refused"
            )
            raise TasksMappingError("job_action_refused", str(code))
        return result

    def submit(self, raw_spec: dict[str, Any]) -> dict[str, Any]:
        if set(raw_spec) != {"spec"} or not isinstance(raw_spec["spec"], dict):
            raise TasksMappingError(
                "invalid_spec", "job_submit arguments require exactly one spec object"
            )
        return self._call("job_submit", raw_spec)

    def status(self, job_id: str) -> dict[str, Any]:
        return self._call("job_status", {"job_id": job_id})

    def cancel(self, job_id: str, *, expected_attempt: int | None = None) -> dict[str, Any]:
        from comsol_mcp.tools.jobs import _TASK_CANCEL_EXPECTED_ATTEMPT

        token = _TASK_CANCEL_EXPECTED_ATTEMPT.set(expected_attempt)
        try:
            return self._call("job_cancel", {"job_id": job_id})
        finally:
            _TASK_CANCEL_EXPECTED_ATTEMPT.reset(token)
