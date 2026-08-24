"""Registration-time tolerance of the shared-session feature gate."""

from types import SimpleNamespace

from mcp.server.mcpserver import MCPServer

from comsol_mcp.tools.shared_session import register_shared_session_tools


def _register_with(selection) -> MCPServer:
    server = MCPServer("shared-session-registration-test")
    if selection is not None:
        server.profile_selection = selection
    register_shared_session_tools(server)
    return server


def test_missing_feature_enabled_hook_registers_without_raising():
    selection = SimpleNamespace(name="core")  # no feature_enabled attribute

    server = _register_with(selection)

    assert "shared_server_preflight" in server._tool_manager._tools


def test_non_callable_feature_enabled_hook_disables_the_gate_safely():
    selection = SimpleNamespace(name="core", feature_enabled="not-a-callable")

    server = _register_with(selection)

    assert "shared_server_preflight" in server._tool_manager._tools
