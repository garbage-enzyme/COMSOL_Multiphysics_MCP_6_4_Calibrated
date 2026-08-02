"""MCP adapters for solver-free thermal radiation evidence."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from comsol_mcp.contracts.thermal_radiation import (
    KirchhoffAssessmentRequest,
    ThermalRadiationRequest,
)


def register_thermal_radiation_tools(mcp: FastMCP) -> None:
    """Register Kirchhoff assessment and thermal radiation evaluation."""

    @mcp.tool()  # type: ignore[untyped-decorator]
    def thermal_kirchhoff_assess(
        request: KirchhoffAssessmentRequest,
    ) -> dict[str, Any]:
        """Assess exact-channel Kirchhoff applicability without inferring unknown facts."""
        from comsol_mcp.evidence.thermal_radiation import build_kirchhoff_assessment

        try:
            return {
                "success": True,
                "assessment": build_kirchhoff_assessment(request),
                "solver_started": False,
                "filesystem_modified": False,
            }
        except (TypeError, ValueError) as exc:
            return {
                "success": False,
                "reason_code": "kirchhoff_assessment_rejected",
                "error": str(exc)[:2048],
                "solver_started": False,
                "filesystem_modified": False,
            }

    @mcp.tool()  # type: ignore[untyped-decorator]
    def thermal_radiation_evaluate(
        request: ThermalRadiationRequest,
    ) -> dict[str, Any]:
        """Integrate bounded thermal radiation and detector-path evidence."""
        from comsol_mcp.evidence.thermal_radiation import evaluate_thermal_radiation

        try:
            return {
                "success": True,
                "evidence": evaluate_thermal_radiation(request),
                "solver_started": False,
                "filesystem_modified": False,
            }
        except (TypeError, ValueError) as exc:
            return {
                "success": False,
                "reason_code": "thermal_radiation_request_rejected",
                "error": str(exc)[:2048],
                "solver_started": False,
                "filesystem_modified": False,
            }


__all__ = ["register_thermal_radiation_tools"]
