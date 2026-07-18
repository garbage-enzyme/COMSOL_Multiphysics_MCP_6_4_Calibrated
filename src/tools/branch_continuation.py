"""MCP adapter for bounded solver-free branch-continuation planning."""

from __future__ import annotations

from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

def register_branch_continuation_tools(mcp: FastMCP) -> None:
    """Register one read-only branch-continuation planning tool."""

    @mcp.tool()
    def branch_continuation_plan(
        continuation_policy: dict[str, Any],
        states_spec: Optional[dict[str, Any]] = None,
        continuation_states: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Plan bounded per-coordinate continuation windows from spectral evidence."""
        from src.evidence.branch_continuation import (
            build_continuation_states,
            plan_branch_continuation,
            validate_continuation_states,
        )

        try:
            if (states_spec is None) == (continuation_states is None):
                raise ValueError(
                    "provide exactly one of states_spec or continuation_states"
                )
            if states_spec is not None:
                states = build_continuation_states(**states_spec)
            else:
                states = validate_continuation_states(continuation_states)
            plan = plan_branch_continuation(states, continuation_policy)
            return {
                "success": True,
                "scientific_disposition": plan["scientific_disposition"],
                "continuation_states": states,
                "branch_continuation_plan": plan,
                "artifact_separation": {
                    "ordered_evidence": "continuation_states",
                    "policy_plan": "branch_continuation_plan",
                },
                "branch_disappearance_claimed": False,
                "undeclared_coordinate_started": False,
                "solver_started": False,
                "filesystem_modified": False,
            }
        except (TypeError, ValueError) as exc:
            return {
                "success": False,
                "scientific_disposition": "invalid_evidence",
                "reason_code": "continuation_input_rejected",
                "error": str(exc)[:2048],
                "branch_disappearance_claimed": False,
                "undeclared_coordinate_started": False,
                "solver_started": False,
                "filesystem_modified": False,
            }


__all__ = ["register_branch_continuation_tools"]
