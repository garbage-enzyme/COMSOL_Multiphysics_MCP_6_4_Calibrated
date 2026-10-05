"""Stable MCP Tasks extension (SEP-2663) over the durable job engine.

This module is the standards adapter the handoff requires: it advertises the
``io.modelcontextprotocol/tasks`` extension, gates every task-shaped result on
the *request's* client capability, and serves ``tasks/get``, ``tasks/cancel``,
and ``tasks/update`` through the SDK's public extension hooks.

Why an extension and not a fork
-------------------------------

The reviewed SDK implements the 2026-07-28 core revision but deliberately does
not implement SEP-2663 Tasks. It does, however, expose a public, documented
extension surface (SEP-2133): an :class:`~mcp.server.extension.Extension`
subclass contributes additive request methods and one ``tools/call``
interceptor. That is exactly the shape this adapter needs, so it needs neither a
monkey-patch nor a fork. ``comsol_mcp.protocol_identity.sdk_extension_capability``
reports whether those hooks are importable, so the claim is checkable.

Wire facts this adapter honours
-------------------------------

* Only ``tools/call`` supports task-augmented execution.
* The result envelope is flat and discriminated by ``resultType``; a task handle
  is ``resultType: "task"`` with ``taskId``/``status``/``createdAt``/
  ``lastUpdatedAt``/``ttlMs``, and ``tasks/get`` answers ``resultType:
  "complete"`` with a status-narrowed detail object.
* The TTL field is ``ttlMs`` and the retrieval method is ``tasks/get``. The
  experimental 2025-11-25 dialect used ``ttl`` and ``tasks/result``; that dialect
  is refused, not reinterpreted.
* ``tasks/result`` was deleted by the redesign and must answer ``-32601``; this
  adapter deliberately does not register it.
* A missing client capability answers ``-32021`` with a ``requiredCapabilities``
  payload.

Deliberate non-claims
---------------------

Advertising the extension is not a claim that every tool is task-capable, and it
is not a claim of native solver support: the actual long-running work remains the
existing durable job engine, launched exactly as before. Ordinary
``job_submit``/``job_status``/``job_tail``/``job_cancel``/``job_resume`` tools
keep working unchanged for clients that never opt in.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from comsol_mcp.jobs.tasks_bridge import (
    DEFAULT_POLL_INTERVAL_MS,
    TASKS_WIRE_GENERATION,
    TaskNotFound,
    TasksBridge,
    TasksGenerationRefused,
    TasksMappingError,
)
from comsol_mcp.protocol_identity import TASKS_EXTENSION_IDENTIFIER

#: Method names owned by this extension. Registered additively; `tools/call`
#: itself is a spec method and is wrapped through the interceptor instead.
TASKS_METHOD_GET = "tasks/get"
TASKS_METHOD_CANCEL = "tasks/cancel"
TASKS_METHOD_UPDATE = "tasks/update"

#: The redesigned extension removed this method. Declared so a test can prove
#: this adapter never registers it and that the surface stays a deliberate set.
REMOVED_TASKS_METHODS = ("tasks/result", "tasks/list")

#: The only protocol revision this extension is served at.
SUPPORTED_PROTOCOL_VERSIONS = frozenset({TASKS_WIRE_GENERATION})

#: Tools whose completion this extension may turn into a durable task handle.
#:
#: The set is deliberately one entry wide. ``job_submit`` is the "start a
#: simulation" entry point, and it already owns durable persistence, admission,
#: duplicate suppression, and cancellation, so a task handle adds a standards
#: wire shape without adding a second scheduler. No other tool is task-augmented
#: in this release; a client that opts in still receives an ordinary result from
#: every tool outside this set.
TASK_CAPABLE_TOOLS = ("job_submit",)


class _TasksParams(BaseModel):
    """Shared params shape for the extension's request methods."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    task_id: str = Field(alias="taskId", min_length=1, max_length=128)


class GetTaskParams(_TasksParams):
    """``tasks/get`` params. A pure read, so it carries no write fields."""


class CancelTaskParams(_TasksParams):
    """``tasks/cancel`` params."""


class UpdateTaskParams(_TasksParams):
    """``tasks/update`` params."""

    input_responses: dict[str, Any] = Field(default_factory=dict, alias="inputResponses")


def missing_capability_error() -> dict[str, Any]:
    """The exact ``-32021`` body required for a non-declaring client."""
    return TasksBridge.refuse_without_capability()


def request_declared_tasks(meta: object) -> bool:
    """Whether one request's ``_meta`` declared the Tasks extension.

    Reads only the reserved per-request capability key. A malformed envelope is
    treated as "not declared" so a bad envelope can never unlock task behaviour.
    """
    from mcp_types import CLIENT_CAPABILITIES_META_KEY

    if not isinstance(meta, Mapping):
        return False
    capabilities = meta.get(CLIENT_CAPABILITIES_META_KEY)
    if not isinstance(capabilities, Mapping):
        return False
    return TasksBridge.client_declared_tasks(capabilities.get("extensions"))


def build_tasks_extension(bridge: TasksBridge) -> Any:
    """Construct the SDK extension object bound to ``bridge``.

    Imported lazily so that merely importing this module never imports the SDK's
    composition tier and never makes cold discovery heavier.
    """
    from mcp.server.extension import Extension, MethodBinding

    class TasksExtension(Extension):
        """The stable Tasks extension as seen by the SDK."""

        identifier = TASKS_EXTENSION_IDENTIFIER

        def settings(self) -> dict[str, Any]:
            # No extension-specific settings are defined by the specification.
            return {}

        def methods(self) -> Sequence[Any]:
            return (
                MethodBinding(
                    method=TASKS_METHOD_GET,
                    params_type=GetTaskParams,
                    handler=self._handle_get,
                    protocol_versions=SUPPORTED_PROTOCOL_VERSIONS,
                ),
                MethodBinding(
                    method=TASKS_METHOD_CANCEL,
                    params_type=CancelTaskParams,
                    handler=self._handle_cancel,
                    protocol_versions=SUPPORTED_PROTOCOL_VERSIONS,
                ),
                MethodBinding(
                    method=TASKS_METHOD_UPDATE,
                    params_type=UpdateTaskParams,
                    handler=self._handle_update,
                    protocol_versions=SUPPORTED_PROTOCOL_VERSIONS,
                ),
            )

        @staticmethod
        def _require_declared(ctx: Any) -> None:
            from mcp.server.mcpserver import require_client_extension

            # The SDK helper raises the exact -32021 error with the required
            # capabilities payload, so the requirement is not re-implemented.
            require_client_extension(ctx, TASKS_EXTENSION_IDENTIFIER)

        async def _handle_get(self, ctx: Any, params: Any) -> dict[str, Any]:
            self._require_declared(ctx)
            try:
                return bridge.get_task(params.task_id)
            except TaskNotFound as exc:
                raise _invalid_params(exc) from exc

        async def _handle_cancel(self, ctx: Any, params: Any) -> dict[str, Any]:
            self._require_declared(ctx)
            try:
                return bridge.cancel_task(params.task_id)
            except TaskNotFound as exc:
                raise _invalid_params(exc) from exc

        async def _handle_update(self, ctx: Any, params: Any) -> dict[str, Any]:
            self._require_declared(ctx)
            try:
                return bridge.update_task(params.task_id, params.input_responses)
            except TaskNotFound as exc:
                raise _invalid_params(exc) from exc

        async def intercept_tool_call(self, params: Any, ctx: Any, call_next: Any) -> Any:
            """Return a durable task handle for an opted-in task-capable call.

            Two conditions must both hold, and each is checked here rather than
            inferred from prior traffic:

            1. this request declared the Tasks extension in its ``_meta`` client
               capabilities, and
            2. the invoked tool is in :data:`TASK_CAPABLE_TOOLS`.

            When either fails, the call runs exactly as before, so an ordinary
            client — or an opted-in client calling any other tool — never
            receives a task-shaped result.
            """
            if not request_declared_tasks(getattr(ctx, "meta", None)):
                return await call_next(ctx)
            if getattr(params, "name", None) not in TASK_CAPABLE_TOOLS:
                return await call_next(ctx)
            arguments = getattr(params, "arguments", None)
            if not isinstance(arguments, Mapping):
                raise _invalid_params(
                    TasksMappingError(
                        "invalid_spec", "a task-augmented call requires an object of arguments"
                    )
                )
            capabilities = _declared_capabilities(getattr(ctx, "meta", None))
            extensions = capabilities.get("extensions")
            try:
                return bridge.submit_task(
                    arguments,
                    client_extensions=extensions,
                    request_client=_request_client(ctx),
                    ttl_ms=_requested_ttl(capabilities),
                )
            except TasksGenerationRefused as exc:
                raise _invalid_params(exc) from exc
            except TasksMappingError as exc:
                raise _invalid_params(exc) from exc

    return TasksExtension()


def _declared_capabilities(meta: object) -> Mapping[str, Any]:
    """Extract this request's declared client capabilities, or an empty map."""
    from mcp_types import CLIENT_CAPABILITIES_META_KEY

    if not isinstance(meta, Mapping):
        return {}
    capabilities = meta.get(CLIENT_CAPABILITIES_META_KEY)
    return capabilities if isinstance(capabilities, Mapping) else {}


def _request_client(ctx: Any) -> str | None:
    """Return a bounded self-reported client identity for the receipt."""
    from mcp_types import CLIENT_INFO_META_KEY

    meta = getattr(ctx, "meta", None)
    if not isinstance(meta, Mapping):
        return None
    info = meta.get(CLIENT_INFO_META_KEY)
    if not isinstance(info, Mapping):
        return None
    name = info.get("name")
    declared_version = info.get("version")
    if not isinstance(name, str) or not name.strip():
        return None
    label = name.strip()[:120]
    if isinstance(declared_version, str) and declared_version.strip():
        label = f"{label}/{declared_version.strip()[:32]}"
    return label


def _requested_ttl(capabilities: Mapping[str, Any]) -> Any:
    """Read an optional per-request TTL from the extension settings object."""
    settings = capabilities.get("extensions")
    if not isinstance(settings, Mapping):
        return None
    entry = settings.get(TASKS_EXTENSION_IDENTIFIER)
    if not isinstance(entry, Mapping):
        return None
    return entry.get("ttlMs")


def _invalid_params(exc: Exception) -> Exception:
    """Translate an unaddressable task id into the required ``-32602`` error."""
    from mcp.shared.exceptions import MCPError

    return MCPError(code=-32602, message=str(exc))


def advertise_tasks_capability(settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return the server-capability fragment that advertises this extension."""
    return {TASKS_EXTENSION_IDENTIFIER: dict(settings or {})}


__all__ = [
    "DEFAULT_POLL_INTERVAL_MS",
    "REMOVED_TASKS_METHODS",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "TASKS_METHOD_CANCEL",
    "TASKS_METHOD_GET",
    "TASKS_METHOD_UPDATE",
    "TASK_CAPABLE_TOOLS",
    "CancelTaskParams",
    "GetTaskParams",
    "TasksGenerationRefused",
    "TasksMappingError",
    "UpdateTaskParams",
    "advertise_tasks_capability",
    "build_tasks_extension",
    "missing_capability_error",
    "request_declared_tasks",
]
