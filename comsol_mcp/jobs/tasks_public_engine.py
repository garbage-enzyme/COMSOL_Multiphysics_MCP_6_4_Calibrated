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

    def submit(self, raw_spec: dict[str, Any], request_id: str | None = None) -> dict[str, Any]:
        # The Tasks interceptor forwards the full public argument envelope:
        # a spec object plus, since 0.8.1, an optional bounded request_id
        # that routes the submission through durable response-loss binding.
        # An explicit null request_id is the schema-legal spelling of
        # omission: the envelope may carry the key with a null value.
        envelope = dict(raw_spec)
        declared = envelope.pop("request_id", None)
        if set(envelope) != {"spec"} or not isinstance(envelope["spec"], dict):
            raise TasksMappingError("invalid_spec", "job_submit arguments require one spec object")
        if declared is not None and not isinstance(declared, str):
            raise TasksMappingError("invalid_request_id", "request_id must be a string or null")
        if request_id is None and isinstance(declared, str):
            request_id = declared
        arguments: dict[str, Any] = {"spec": envelope["spec"]}
        if request_id is not None:
            arguments["request_id"] = request_id
        return self._call("job_submit", arguments)

    def resolve_request_replay(
        self, raw_spec: dict[str, Any], request_id: str | None
    ) -> dict[str, Any] | None:
        """Resolve a durably bound replay WITHOUT dispatching (C081D-04).

        The Tasks bridge consults this before new-row capacity admission so
        a same-request retry that needs no new job or mapping row is never
        refused at the boundary. Returns the replay outcome, or None when
        unbound; conflicting bindings refuse like a dispatch would.
        """
        from comsol_mcp.tools import jobs as jobs_module

        manager = getattr(jobs_module, "job_manager", None)
        delegate = getattr(manager, "resolve_request_replay", None)
        if not callable(delegate):
            return None
        outcome = delegate(raw_spec, request_id)
        return outcome if isinstance(outcome, dict) else None

    def status(self, job_id: str) -> dict[str, Any]:
        return self._call("job_status", {"job_id": job_id})

    def cancel(self, job_id: str, *, expected_attempt: int | None = None) -> dict[str, Any]:
        from comsol_mcp.tools.jobs import _TASK_CANCEL_EXPECTED_ATTEMPT

        token = _TASK_CANCEL_EXPECTED_ATTEMPT.set(expected_attempt)
        try:
            return self._call("job_cancel", {"job_id": job_id})
        finally:
            _TASK_CANCEL_EXPECTED_ATTEMPT.reset(token)
