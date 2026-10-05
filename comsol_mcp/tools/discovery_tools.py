"""MCP registration for solver-free progressive discovery.

The ``catalog`` tool is deliberately dependency-free and profile-independent: it
describes the installed surface, starts nothing, and is the on-demand half of the
progressive discovery contract that keeps cold bootstrap bounded. It never
imports COMSOL, MPh, or an optional heavy dependency.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from .discovery import (
    CATALOG_SCHEMA_NAME,
    CATALOG_SCHEMA_VERSION,
    MAX_CATALOG_TOOLS,
    get_tool_catalog,
)
from .profiles import ProfileSelection


def register_discovery_tools(mcp: MCPServer) -> None:
    """Register the read-only progressive-discovery catalog tool.

    ``mcp`` is the profiled registration surface, not necessarily the raw server,
    so it is also the object that can report the schemas actually advertised to
    clients. It is captured here and read at call time, after every registrar has
    run, so the on-demand view always reflects the finished surface.
    """
    selection = getattr(mcp, "profile_selection", None)

    @mcp.tool()
    def catalog(
        domain: str | None = None,
        tool: str | None = None,
        schema: str | None = None,
        offset: int = 0,
        limit: int = MAX_CATALOG_TOOLS,
    ) -> dict[str, Any]:
        """
        Discover domains and fetch one tool or artifact schema on demand.

        Call this with no selector for the compact domain/tool catalog, the
        profile inventory, and the schema-registry fingerprint, then pass exactly
        one selector to fetch one domain, one tool's input schema, or one named
        artifact schema. It is read-only and never starts COMSOL, Java, MPh, or a
        worker process.

        Args:
            domain: Return one domain's tools with their full input schemas. The
                response states its own offset and whether it is complete, so a
                partial page is never mistaken for the whole domain.
            tool: Return one tool's metadata and its advertised input schema.
            schema: Return one named artifact schema registry entry.
            offset: Row offset for a domain page; 0 starts at the first tool.
            limit: Maximum rows in a domain page.
        """
        active = selection if isinstance(selection, ProfileSelection) else None
        return get_tool_catalog(
            active,
            domain=domain,
            tool=tool,
            schema=schema,
            offset=offset,
            limit=limit,
            surface=mcp,
        )


__all__ = [
    "CATALOG_SCHEMA_NAME",
    "CATALOG_SCHEMA_VERSION",
    "register_discovery_tools",
]
