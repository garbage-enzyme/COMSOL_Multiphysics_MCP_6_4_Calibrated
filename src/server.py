"""COMSOL MCP Server - Main entry point."""

import logging
from weakref import WeakSet

from mcp.server.fastmcp import FastMCP

from .tools import register_tool_modules
from .resources.model_resources import register_model_resources
from .knowledge.embedded import register_knowledge_tools

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP("COMSOL MCP")
_tool_servers: WeakSet[FastMCP] = WeakSet()
_resource_servers: WeakSet[FastMCP] = WeakSet()


def register_all_tools(server: FastMCP | None = None) -> None:
    """Register all MCP tools once on the selected server."""
    target = server or mcp
    if target in _tool_servers:
        return
    register_tool_modules(target)
    register_knowledge_tools(target)
    _tool_servers.add(target)
    logger.info("Registered all tools")


def register_all_resources(server: FastMCP | None = None) -> None:
    """Register all MCP resources once on the selected server."""
    target = server or mcp
    if target in _resource_servers:
        return
    register_model_resources(target)
    _resource_servers.add(target)
    logger.info("Registered all resources")


def create_server(name: str = "COMSOL MCP") -> FastMCP:
    """Create a fully registered server without starting its transport."""
    server = FastMCP(name)
    register_all_tools(server)
    register_all_resources(server)
    return server


def main() -> None:
    """Run the MCP server."""
    logger.info("Starting COMSOL MCP Server...")
    
    register_all_tools()
    register_all_resources()
    
    mcp.run()


if __name__ == "__main__":
    main()
