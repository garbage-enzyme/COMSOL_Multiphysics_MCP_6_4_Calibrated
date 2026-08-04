"""Profile-independent Settings GUI launcher tool."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from comsol_mcp.settings_gui_launcher import launch_settings_gui


def register_settings_gui_tools(mcp: MCPServer) -> None:
    """Register the solver-free detached Settings GUI launcher."""

    @mcp.tool(name="settings.start")  # type: ignore[untyped-decorator]
    def settings_start() -> dict:  # type: ignore[type-arg]
        """Open the Settings GUI once, then pause for the user to finish editing."""
        return launch_settings_gui()


__all__ = ["register_settings_gui_tools"]
