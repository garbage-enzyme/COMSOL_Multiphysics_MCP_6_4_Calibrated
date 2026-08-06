"""Minimal solver-free public adapters for bounded research preparation."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer


def register_research_tools(mcp: MCPServer) -> None:
    """Register experimental solver-free research compilation and robustness tools."""

    @mcp.tool()  # type: ignore[untyped-decorator]
    def research_campaign_compile(
        goal: dict[str, Any],
        design_space: dict[str, Any],
        approval: dict[str, Any],
        workflow_capsule: dict[str, Any] | None = None,
        material_catalog: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compile one approved goal and frozen space without starting a solver."""
        try:
            from comsol_mcp.research.compiler import compile_campaign_manifest

            manifest = compile_campaign_manifest(
                goal,
                design_space,
                approval,
                workflow_capsule=workflow_capsule,
                material_catalog=material_catalog,
            )
            return {
                "success": True,
                "campaign_manifest": manifest,
                "solver_started": False,
                "filesystem_modified": False,
            }
        except (TypeError, ValueError) as exc:
            return {
                "success": False,
                "reason_code": "research_campaign_rejected",
                "error": str(exc)[:2048],
                "solver_started": False,
                "filesystem_modified": False,
            }

    @mcp.tool()  # type: ignore[untyped-decorator]
    def research_robustness_plan(
        design_space: dict[str, Any],
        candidate_values: dict[str, Any],
        relative_fraction: float,
    ) -> dict[str, Any]:
        """Create a bounded axis-perturbation matrix without clipping or solving."""
        try:
            from comsol_mcp.research.robustness import axis_perturbation_matrix

            return {
                "success": True,
                "robustness_matrix": axis_perturbation_matrix(
                    design_space,
                    candidate_values,
                    relative_fraction=relative_fraction,
                ),
                "solver_started": False,
                "filesystem_modified": False,
            }
        except (TypeError, ValueError) as exc:
            return {
                "success": False,
                "reason_code": "research_robustness_rejected",
                "error": str(exc)[:2048],
                "solver_started": False,
                "filesystem_modified": False,
            }

    @mcp.tool()  # type: ignore[untyped-decorator]
    def research_optimizer_advance(
        design_space: dict[str, Any],
        campaign_fingerprint: str,
        decision_fingerprint: str,
        created_at: str,
        seed: int = 17001,
        warmup_count: int = 8,
        candidate_pool_count: int = 256,
        checkpoint: dict[str, Any] | None = None,
        feedback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Restore optional state, apply one exact result, and return the next proposal."""
        try:
            from comsol_mcp.research.adaptive_acquisition import (
                GaussianProcessExpectedImprovementOptimizer,
            )

            if checkpoint is None:
                if feedback is not None:
                    raise ValueError("feedback requires a prior optimizer checkpoint")
                optimizer = GaussianProcessExpectedImprovementOptimizer(
                    design_space,
                    seed=seed,
                    warmup_count=warmup_count,
                    candidate_pool_count=candidate_pool_count,
                )
            else:
                optimizer = GaussianProcessExpectedImprovementOptimizer.restore(
                    design_space, checkpoint
                )
                if feedback is not None:
                    required = {
                        "proposal_index",
                        "proposal_fingerprint",
                        "candidate_fingerprint",
                        "status",
                        "score_fingerprint",
                        "losses",
                    }
                    if set(feedback) != required:
                        raise ValueError("feedback fields are incomplete or unknown")
                    proposal = optimizer.proposals.get(feedback["proposal_index"])
                    if (
                        proposal is None
                        or proposal["proposal_fingerprint"] != feedback["proposal_fingerprint"]
                    ):
                        raise ValueError("feedback does not bind an exact prior proposal")
                    optimizer.tell(
                        proposal,
                        candidate_fingerprint=feedback["candidate_fingerprint"],
                        status=feedback["status"],
                        score_fingerprint=feedback["score_fingerprint"],
                        losses=feedback["losses"],
                    )
            if optimizer.state()["remaining_proposals"] <= 0:
                return {
                    "success": True,
                    "state": "exhausted",
                    "proposal": None,
                    "checkpoint": optimizer.checkpoint(
                        campaign_fingerprint=campaign_fingerprint,
                        decision_fingerprint=decision_fingerprint,
                        created_at=created_at,
                    ),
                    "solver_started": False,
                    "filesystem_modified": False,
                }
            proposal = optimizer.ask()
            return {
                "success": True,
                "state": "proposal_ready",
                "proposal": proposal,
                "explanation": optimizer.explain(proposal),
                "checkpoint": optimizer.checkpoint(
                    campaign_fingerprint=campaign_fingerprint,
                    decision_fingerprint=decision_fingerprint,
                    created_at=created_at,
                ),
                "solver_started": False,
                "filesystem_modified": False,
            }
        except (KeyError, TypeError, ValueError) as exc:
            return {
                "success": False,
                "reason_code": "research_optimizer_rejected",
                "error": str(exc)[:2048],
                "solver_started": False,
                "filesystem_modified": False,
            }


__all__ = ["register_research_tools"]
