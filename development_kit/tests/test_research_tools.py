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


def test_optimizer_advance_round_trips_checkpoint_and_exact_feedback():
    tool = _tools()["research_optimizer_advance"]
    first = tool.fn(
        _space(),
        "a" * 64,
        "b" * 64,
        "2026-08-07T00:00:00Z",
        warmup_count=2,
        candidate_pool_count=8,
    )
    proposal = first["proposal"]
    feedback = {
        "proposal_index": proposal["proposal_index"],
        "proposal_fingerprint": proposal["proposal_fingerprint"],
        "candidate_fingerprint": "c" * 64,
        "status": "completed",
        "score_fingerprint": "d" * 64,
        "losses": {"peak": 1.0},
    }
    second = tool.fn(
        _space(),
        "a" * 64,
        "e" * 64,
        "2026-08-07T00:00:01Z",
        checkpoint=first["checkpoint"],
        feedback=feedback,
    )
    assert first["success"] is True
    assert second["success"] is True
    assert second["state"] == "proposal_ready"
    assert second["proposal"]["proposal_index"] == proposal["proposal_index"] + 1
