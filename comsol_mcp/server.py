"""COMSOL MCP Server - Main entry point."""

import logging
import multiprocessing as mp
from weakref import WeakKeyDictionary, WeakSet

from mcp.server.fastmcp import FastMCP

from .native_runtime import preload_mcp_native_runtime
from .settings import apply_java_settings
from .tools.profiles import (
    ProfileSelection,
    register_profiled,
    resolve_profile,
    tool_names_for_profile,
)

logger = logging.getLogger(__name__)

mcp = FastMCP("COMSOL MCP")
_tool_servers: WeakKeyDictionary[FastMCP, ProfileSelection] = WeakKeyDictionary()
_resource_servers: WeakSet[FastMCP] = WeakSet()


def register_all_tools(
    server: FastMCP | None = None,
    profile: str | ProfileSelection | None = None,
) -> ProfileSelection:
    """Register one static MCP tool profile once on the selected server."""
    target = server or mcp
    if target in _tool_servers:
        existing = _tool_servers[target]
        if profile is not None:
            requested = profile.name if isinstance(profile, ProfileSelection) else profile
            if resolve_profile(requested).name != existing.name:
                raise ValueError(
                    f"Server already registered with profile {existing.name!r}; "
                    f"cannot change it to {requested!r} without restart"
                )
        return existing
    selection = profile if isinstance(profile, ProfileSelection) else resolve_profile(profile)
    enabled_names = tool_names_for_profile(selection.name)
    from .knowledge.embedded import register_knowledge_tools
    from .knowledge.lexical_manual import register_lexical_manual_tools
    from .tools import register_tool_modules

    register_tool_modules(target, selection)
    register_profiled(target, register_knowledge_tools, enabled_names, selection)
    register_profiled(target, register_lexical_manual_tools, enabled_names, selection)
    _tool_servers[target] = selection
    logger.info("Registered %d tools for profile %s", len(enabled_names), selection.name)
    return selection


def register_all_resources(server: FastMCP | None = None) -> None:
    """Register all MCP resources once on the selected server."""
    target = server or mcp
    if target in _resource_servers:
        return
    from .resources.model_resources import register_model_resources

    register_model_resources(target)
    _resource_servers.add(target)
    logger.info("Registered all resources")


def create_server(
    name: str = "COMSOL MCP",
    profile: str | ProfileSelection | None = None,
) -> FastMCP:
    """Create a fully registered server without starting its transport."""
    apply_java_settings()
    server = FastMCP(name)
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
