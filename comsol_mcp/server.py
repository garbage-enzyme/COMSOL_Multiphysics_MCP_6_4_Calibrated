"""COMSOL MCP Server - Main entry point."""

import logging
import multiprocessing as mp
import os
from typing import Any
from weakref import WeakKeyDictionary, WeakSet

from mcp.server.mcpserver import MCPServer

from . import __version__
from .native_runtime import preload_mcp_native_runtime
from .settings import apply_java_settings
from .tools.profiles import (
    ProfileSelection,
    register_profiled,
    resolve_profile,
    tool_names_for_selection,
)

logger = logging.getLogger(__name__)

SERVER_INSTRUCTIONS = (
    "Start with capabilities and solver_preflight. Do not start COMSOL, solve, or mutate "
    "a model unless the user explicitly requests it. Treat source models as read-only and "
    "write only to derived copies. Keep execution success, evidence integrity, and scientific "
    "validation as separate outcomes."
)

mcp = MCPServer("COMSOL MCP", instructions=SERVER_INSTRUCTIONS, version=__version__)
_tool_servers: WeakKeyDictionary[MCPServer, ProfileSelection] = WeakKeyDictionary()
_resource_servers: WeakSet[MCPServer] = WeakSet()


def register_all_tools(
    server: MCPServer | None = None,
    profile: str | ProfileSelection | None = None,
) -> ProfileSelection:
    """Register one static MCP tool profile once on the selected server."""
    target = server or mcp
    if target in _tool_servers:
        existing = _tool_servers[target]
        if profile is not None:
            requested_selection = (
                profile if isinstance(profile, ProfileSelection) else resolve_profile(profile)
            )
            if (
                requested_selection.name,
                requested_selection.enabled_features,
            ) != (existing.name, existing.enabled_features):
                raise ValueError(
                    "Server already registered with a different startup selection; "
                    "profile or feature gates cannot change without restart"
                )
        return existing
    selection = profile if isinstance(profile, ProfileSelection) else resolve_profile(profile)
    enabled_names = tool_names_for_selection(selection)
    from .knowledge.embedded import register_knowledge_tools
    from .knowledge.lexical_manual import register_lexical_manual_tools
    from .tools import register_tool_modules

    original_tools = dict(target._tool_manager._tools)
    try:
        register_tool_modules(target, selection)
        register_profiled(target, register_knowledge_tools, enabled_names, selection)
        register_profiled(target, register_lexical_manual_tools, enabled_names, selection)
    except Exception as registration_error:
        try:
            target._tool_manager._tools.clear()
            target._tool_manager._tools.update(original_tools)
        except Exception as rollback_error:
            registration_error.add_note(
                f"tool registry rollback failed: {type(rollback_error).__name__}"
            )
        raise
    _tool_servers[target] = selection
    logger.info(
        "Registered %d tools for profile %s with features %s",
        len(enabled_names),
        selection.name,
        selection.enabled_features,
    )
    return selection


def register_all_resources(server: MCPServer | None = None) -> None:
    """Register all MCP resources once on the selected server."""
    target = server or mcp
    if target in _resource_servers:
        return
    from .resources.model_resources import register_model_resources

    original_resources = dict(target._resource_manager._resources)
    original_templates = dict(target._resource_manager._templates)
    try:
        register_model_resources(target)
    except Exception as registration_error:
        try:
            target._resource_manager._resources.clear()
            target._resource_manager._resources.update(original_resources)
            target._resource_manager._templates.clear()
            target._resource_manager._templates.update(original_templates)
        except Exception as rollback_error:
            registration_error.add_note(
                f"resource registry rollback failed: {type(rollback_error).__name__}"
            )
        raise
    _resource_servers.add(target)
    logger.info("Registered all resources")


def _tasks_extensions(engine) -> list:
    """Build the enabled MCP extensions for one server instance.

    The Tasks extension is registered unconditionally rather than behind a
    profile gate: it is additive, its methods answer only to clients that opt in
    per request, and ``job_submit`` already refuses anything the durable engine
    would refuse. Gating it would make the standards surface depend on a startup
    flag without changing what any client is actually permitted to do.

    Construction is deliberately cheap and imports nothing heavy: the mapping
    layer is bound to the lazy job manager, so cold discovery still starts no
    COMSOL client and no worker process.
    """
    from comsol_mcp.jobs.tasks_bridge import TasksBridge, TasksMappingStore
    from comsol_mcp.jobs.tasks_extension import build_tasks_extension
    from comsol_mcp.tools.ownership import _agent_owner_label
    from comsol_mcp.utils.runtime_paths import default_runtime_dir

    # Bind Tasks to the same normalized effective owner as the ordinary
    # ownership tools: settings-configured, environment-overridden, or the
    # derived agent label. A settings-only owner or a derived label used to be
    # silently collapsed to "local", splitting the Tasks mapping store from
    # every other ownership surface. No authentication model is inferred from
    # request metadata; the owner is a local configuration identity.
    store = TasksMappingStore(default_runtime_dir() / "tasks")
    bridge = TasksBridge(engine=engine, store=store, owner=_agent_owner_label())
    return [build_tasks_extension(bridge)]


def _install_discovery_pagination(server: MCPServer) -> bool:
    """Install the experimental ``tools/list`` pager when the setting enables it.

    Default-off by construction: the setting defaults to ``false``, and when it is
    off this does nothing at all, so the ``tools/list`` path stays the untouched
    SDK handler and clients that read only a first page (MCP1, OpenCode) keep the
    complete listing. A ``False`` return means either the setting is off or the
    reviewed SDK no longer exposes the provisional middleware shape; both leave
    the ordinary contract in place rather than a half-installed adapter.
    """
    from comsol_mcp.settings import load_settings

    try:
        enabled = bool(load_settings()["discovery"]["pagination_enabled"])
    except Exception:
        # A settings read failure must never decide to change the wire contract.
        logger.info("Discovery pagination setting was unreadable; staying off")
        return False
    if not enabled:
        return False
    from .tools.tools_list_pagination import install_tools_list_pagination

    installed = install_tools_list_pagination(server)
    logger.info("Experimental tools/list pagination installed=%s", installed)
    return installed


def create_server(
    name: str = "COMSOL MCP",
    profile: str | ProfileSelection | None = None,
) -> MCPServer:
    """Create a fully registered server without starting its transport."""
    from comsol_mcp.jobs.tasks_public_engine import ProfiledTasksEngine

    apply_java_settings()
    engine = ProfiledTasksEngine()
    server = MCPServer(
        name,
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
        extensions=_tasks_extensions(engine),
    )
    register_all_tools(server, profile)
    engine.bind(server)
    register_all_resources(server)
    _install_discovery_pagination(server)
    return server


def _preload_native_runtime() -> dict[str, str]:
    """Compatibility wrapper for the auditable native-runtime manifest."""
    return preload_mcp_native_runtime()


def _startup_transport_gate() -> "tuple[Any, Any] | None":
    """0.8.2 §10 startup gate: one settings snapshot through admission.

    Returns ``(claim, snapshot)`` for a direct Python host, or ``None`` for
    a verified shim-contained worker. Exits with code 2 on any refusal.
    Every decision quotes the same settings snapshot: its identity is bound
    into the admission record, so a settings change during startup is
    detectable instead of being mixed across reads.
    """
    import sys as _sys

    from comsol_mcp.host_admission import (
        HostAdmissionError,
        acquire,
        release,
        transport_setting_errors,
        validate_transport_mode,
        verify_worker_binding,
    )
    from comsol_mcp.settings import load_settings_snapshot, read_settings_digest
    from comsol_mcp.utils.runtime_paths import default_runtime_dir

    def _refuse(exc: HostAdmissionError) -> None:
        logger.error("Host admission refused: %s (%s)", exc, exc.reason_code)
        _sys.stderr.write(f"comsol-mcp: refusing to start: [{exc.reason_code}] {exc}\n")
        _sys.exit(2)

    try:
        snapshot = load_settings_snapshot()
    except Exception as exc:  # noqa: BLE001 - a single refusal path for every unread
        logger.error("Settings snapshot failed: %s", exc)
        _sys.stderr.write(
            f"comsol-mcp: refusing to start: [settings_unreadable] settings cannot be read: {exc}\n"
        )
        _sys.exit(2)

    runtime_dir = default_runtime_dir(settings=snapshot.settings)

    if os.environ.get("COMSOL_MCP_SHIM_WORKER") == "1":
        try:
            binding = verify_worker_binding(
                settings=snapshot.settings,
                settings_identity=snapshot.identity(),
                runtime_dir=runtime_dir,
            )
        except HostAdmissionError as exc:
            _refuse(exc)
        logger.info(
            "Shim worker binding verified: host PID %s, binary %s",
            binding["host_pid"],
            binding["executable"],
        )
        # The launch secret is not needed again; children never inherit it.
        os.environ.pop("COMSOL_MCP_SHIM_WORKER_TOKEN", None)
        os.environ.pop("COMSOL_MCP_SHIM_HOST_PID", None)
        # Independent owners (durable jobs, settings window) must outlive the
        # shim's kill-on-close job, as they outlive a direct Python host.
        from comsol_mcp.process_launch import enable_job_breakaway

        enable_job_breakaway()
        return None

    # A missing settings file means defaults, whose transport is Python;
    # that is not ambiguous for this host. Unreadable or invalid transport
    # configuration is ambiguous and refuses before admission.
    if errors := transport_setting_errors(snapshot.report):
        summary = "; ".join(f"{error.get('path')}: {error.get('message')}" for error in errors)
        _sys.stderr.write(
            "comsol-mcp: refusing to start: [transport_setting_invalid] the configured "
            f"transport is ambiguous ({summary}). Fix settings.json and retry.\n"
        )
        _sys.exit(2)
    try:
        validate_transport_mode(
            snapshot.settings,
            actual_mode="python",
            settings_source=str(snapshot.location.path),
        )
        claim = acquire(
            runtime_dir,
            transport_mode="python",
            settings=snapshot.identity(),
        )
        if read_settings_digest(snapshot.location.path) != snapshot.sha256:
            release(claim)
            raise HostAdmissionError(
                "settings_changed_during_admission",
                "settings.json changed between validation and admission; the "
                "host released admission. Retry after the change is complete.",
                {"settings_path": str(snapshot.location.path)},
            )
    except HostAdmissionError as exc:
        _refuse(exc)
    except OSError as exc:
        _refuse(
            HostAdmissionError(
                "settings_unreadable",
                f"settings.json could not be re-read after admission: {exc}",
            )
        )
    logger.info(
        "Host admission: PID %s, transport python, settings %s",
        claim.document["pid"],
        claim.document["settings"]["sha256"],
    )
    return claim, snapshot


def main() -> None:
    """Run the MCP server.

    B11: resolve the profile before any native/JVM preload so the
    ``comsolless_read_only`` entry point stays a lightweight offline path.
    Other profiles keep the existing main-thread native preload contract.

    0.8.2 §10: validate transport mode and acquire host admission before
    serving. The Python direct host refuses to start when settings select
    the shim, and refuses when another host already holds admission. A
    shim-contained worker runs only after its launch binding verifies.
    """
    logging.basicConfig(level=logging.INFO)

    gate = _startup_transport_gate()
    claim = gate[0] if gate is not None else None

    # Profile selection must precede heavy imports/preload.
    selection = resolve_profile()
    offline = getattr(selection, "name", None) == "comsolless_read_only"
    if offline:
        native_runtime = {
            "status": "skipped",
            "reason": "comsolless_read_only_profile",
        }
    else:
        apply_java_settings()
        native_runtime = _preload_native_runtime()
    from .tools.capabilities import startup_capability_summary

    logger.info("Starting COMSOL MCP Server...")
    logger.info("Profile: %s offline=%s", getattr(selection, "name", selection), offline)
    logger.info("Native runtime: %s", native_runtime)
    logger.info("Capabilities: %s", startup_capability_summary(selection))

    # Use the same factory as embedded and installed callers.  The legacy
    # module-global server does not carry the Tasks extension or its engine.
    # Release host admission on clean exit by removing exactly the record
    # this host published; a crash leaves a stale record that the next
    # acquirer recovers only after verified cleanup.
    _admission_released = False

    def _release_admission() -> None:
        nonlocal _admission_released
        if _admission_released or claim is None:
            return
        _admission_released = True
        try:
            from comsol_mcp.host_admission import release as _release

            if not _release(claim):
                logger.warning("Host admission record changed; it was left in place")
        except Exception as exc:  # noqa: BLE001 - shutdown must not mask the exit path
            logger.warning("Host admission release failed: %s", exc)

    import atexit as _atexit

    _atexit.register(_release_admission)
    try:
        create_server(profile=selection).run()
    finally:
        _release_admission()


def _is_transport_entrypoint() -> bool:
    """Avoid re-running ``main`` when Windows spawn re-imports this module."""
    return __name__ == "__main__" and mp.current_process().name == "MainProcess"


if _is_transport_entrypoint():
    main()
