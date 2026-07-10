"""Tests for MCP server construction without starting a transport."""

from src.server import create_server, register_all_resources, register_all_tools


def test_server_registration_is_idempotent():
    server = create_server("registration-test")
    tool_names = set(server._tool_manager._tools)
    resource_names = set(server._resource_manager._resources)

    assert "comsol_start" in tool_names
    assert "model_create" in tool_names
    assert "study_solve" in tool_names
    assert resource_names

    register_all_tools(server)
    register_all_resources(server)

    assert set(server._tool_manager._tools) == tool_names
    assert set(server._resource_manager._resources) == resource_names
