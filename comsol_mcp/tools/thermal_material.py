"""MCP adapters for solver-free thermal material ledgers."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from comsol_mcp.contracts.thermal_material import (
    ThermalMaterialEvaluationRequest,
    ThermalMaterialLedger,
)


def register_thermal_material_tools(mcp: MCPServer) -> None:
    """Register bounded material-ledger validation and evaluation."""

    @mcp.tool()  # type: ignore[untyped-decorator]
    def thermal_material_validate(ledger: ThermalMaterialLedger) -> dict[str, Any]:
        """Validate and preview one typed temperature/state material ledger."""
        from comsol_mcp.evidence.thermal_material import normalize_thermal_material_ledger

        try:
            return {
                "success": True,
                "ledger": normalize_thermal_material_ledger(ledger),
                "solver_started": False,
                "filesystem_modified": False,
            }
        except ValueError as exc:
            return {
                "success": False,
                "reason_code": "thermal_material_ledger_rejected",
                "error": str(exc)[:2048],
                "solver_started": False,
                "filesystem_modified": False,
            }

    @mcp.tool()  # type: ignore[untyped-decorator]
    def thermal_material_evaluate(
        request: ThermalMaterialEvaluationRequest,
    ) -> dict[str, Any]:
        """Evaluate one exact material state without COMSOL mutation or solve."""
        from comsol_mcp.evidence.thermal_material import evaluate_thermal_material

        try:
            return {
                "success": True,
                "evaluation": evaluate_thermal_material(request),
                "solver_started": False,
                "filesystem_modified": False,
            }
        except ValueError as exc:
            return {
                "success": False,
                "reason_code": "thermal_material_evaluation_rejected",
                "error": str(exc)[:2048],
                "solver_started": False,
                "filesystem_modified": False,
            }


__all__ = ["register_thermal_material_tools"]
