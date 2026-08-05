"""COMSOL MCP Server - Main entry point."""

import logging
import multiprocessing as mp
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
    except Exception:
        target._tool_manager._tools.clear()
        target._tool_manager._tools.update(original_tools)
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
    except Exception:
        target._resource_manager._resources.clear()
        target._resource_manager._resources.update(original_resources)
        target._resource_manager._templates.clear()
        target._resource_manager._templates.update(original_templates)
        raise
    _resource_servers.add(target)
    logger.info("Registered all resources")


def create_server(
    name: str = "COMSOL MCP",
    profile: str | ProfileSelection | None = None,
) -> MCPServer:
    """Create a fully registered server without starting its transport."""
    apply_java_settings()
    server = MCPServer(name, instructions=SERVER_INSTRUCTIONS, version=__version__)
    register_all_tools(server, profile)
    register_all_resources(server)
    return server


def _preload_native_runtime() -> dict[str, str]:
    """Compatibility wrapper for the auditable native-runtime manifest."""
    return preload_mcp_native_runtime()


def main() -> None:
    """Run the MCP server."""
    logging.basicConfig(level=logging.INFO)
    apply_java_settings()
    native_runtime = _preload_native_runtime()
    selection = resolve_profile()
    from .tools.capabilities import startup_capability_summary

    logger.info("Starting COMSOL MCP Server...")
    logger.info("Preloaded native runtime on main thread: %s", native_runtime)
    logger.info("Capabilities: %s", startup_capability_summary(selection))

    register_all_tools(profile=selection)
    register_all_resources()

    mcp.run()


def _is_transport_entrypoint() -> bool:
    """Avoid re-running ``main`` when Windows spawn re-imports this module."""
    return __name__ == "__main__" and mp.current_process().name == "MainProcess"


if _is_transport_entrypoint():
    main()
