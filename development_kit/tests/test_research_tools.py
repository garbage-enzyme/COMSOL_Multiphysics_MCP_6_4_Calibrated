"""Public dispatch gates for the minimal experimental research tools."""

from mcp.server.mcpserver import MCPServer

from comsol_mcp.tools.research import register_research_tools
from development_kit.tests.test_research_contracts import _approval, _goal, _space


def _tools():
    server = MCPServer("research-test")
    register_research_tools(server)
    return server._tool_manager._tools


def test_campaign_compile_dispatches_without_solver_or_filesystem_side_effects():
    tool = _tools()["research_campaign_compile"]
    result = tool.fn(_goal(), _space(), _approval())
    assert result["success"] is True
    assert (
        result["campaign_manifest"]["design_space"]["structure_family"] == "periodic_mim_patch_v1"
    )
    assert result["solver_started"] is False
    assert result["filesystem_modified"] is False


def test_robustness_plan_dispatches_and_rejects_boundary_clipping():
    tool = _tools()["research_robustness_plan"]
    accepted = tool.fn(_space(), {"patch_length_x": 100.0, "patch_length_y": 80.0}, 0.01)
    rejected = tool.fn(_space(), {"patch_length_x": 75.0, "patch_length_y": 80.0}, 0.01)
    assert accepted["success"] is True
    assert len(accepted["robustness_matrix"]["points"]) == 5
    assert rejected["success"] is False
    assert rejected["reason_code"] == "research_robustness_rejected"
