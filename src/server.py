"""COMSOL MCP Server - Main entry point."""

import logging
import multiprocessing as mp
from weakref import WeakKeyDictionary, WeakSet

from mcp.server.fastmcp import FastMCP

from .tools import register_tool_modules
from .tools.capabilities import startup_capability_summary
from .tools.profiles import ProfileSelection, register_profiled, resolve_profile, tool_names_for_profile
from .resources.model_resources import register_model_resources
from .knowledge.embedded import register_knowledge_tools
from .knowledge.lexical_manual import register_lexical_manual_tools

logging.basicConfig(level=logging.INFO)
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
    register_model_resources(target)
    _resource_servers.add(target)
    logger.info("Registered all resources")


def create_server(name: str = "COMSOL MCP", profile: str | None = None) -> FastMCP:
    """Create a fully registered server without starting its transport."""
    server = FastMCP(name)
    register_all_tools(server, profile)
    register_all_resources(server)
    return server


def main() -> None:
    """Run the MCP server."""
    selection = resolve_profile()
    logger.info("Starting COMSOL MCP Server...")
    logger.info("Capabilities: %s", startup_capability_summary(selection))
    
    register_all_tools(profile=selection)
    register_all_resources()
    
    mcp.run()


def _is_transport_entrypoint() -> bool:
    """Avoid re-running ``main`` when Windows spawn re-imports this module."""
    return __name__ == "__main__" and mp.current_process().name == "MainProcess"


if _is_transport_entrypoint():
    main()
