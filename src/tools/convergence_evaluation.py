"""MCP adapter for bounded solver-free convergence evaluation."""

from __future__ import annotations

from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from src.evidence.convergence_evaluation import (
    build_convergence_ladder,
    evaluate_convergence,
    validate_convergence_ladder,
)


def register_convergence_evaluation_tools(mcp: FastMCP) -> None:
    """Register one read-only convergence evaluation tool."""

    @mcp.tool()
    def convergence_evaluate(
        convergence_policy: dict[str, Any],
        ladder_spec: Optional[dict[str, Any]] = None,
        convergence_ladder: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Compare an ordered configuration ladder at each level's own peak."""
        try:
            if (ladder_spec is None) == (convergence_ladder is None):
                raise ValueError(
                    "provide exactly one of ladder_spec or convergence_ladder"
                )
            if ladder_spec is not None:
                ladder = build_convergence_ladder(**ladder_spec)
            else:
                ladder = validate_convergence_ladder(convergence_ladder)
            evaluation = evaluate_convergence(ladder, convergence_policy)
            return {
                "success": True,
                "scientific_disposition": evaluation["scientific_disposition"],
                "convergence_ladder": ladder,
                "convergence_evaluation": evaluation,
                "artifact_separation": {
                    "ordered_evidence": "convergence_ladder",
                    "policy_decision": "convergence_evaluation",
                },
                "fixed_reference_governs": False,
                "monotonicity_proves_convergence": False,
                "undeclared_configuration_started": False,
                "solver_started": False,
                "filesystem_modified": False,
            }
        except (TypeError, ValueError) as exc:
            return {
                "success": False,
                "scientific_disposition": "invalid_evidence",
                "reason_code": "convergence_input_rejected",
                "error": str(exc)[:2048],
                "undeclared_configuration_started": False,
                "solver_started": False,
                "filesystem_modified": False,
            }


__all__ = ["register_convergence_evaluation_tools"]
