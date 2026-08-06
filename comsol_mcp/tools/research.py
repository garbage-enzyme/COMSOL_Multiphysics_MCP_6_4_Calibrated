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


__all__ = ["register_research_tools"]
