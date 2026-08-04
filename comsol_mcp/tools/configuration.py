"""MCP adapters for solver-free simulation configuration contracts."""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver import MCPServer

from comsol_mcp.contracts.simulation_configuration import (
    ConfigurationDiffPolicy,
    SimulationConfigurationInput,
)
from comsol_mcp.utils.public_errors import public_error

logger = logging.getLogger(__name__)


def register_configuration_tools(mcp: MCPServer) -> None:
    """Register bounded simulation configuration validation and comparison."""

    @mcp.tool()  # type: ignore[untyped-decorator]
    def simulation_configuration_validate(
        configuration: SimulationConfigurationInput,
    ) -> dict[str, Any]:
        """Normalize one explicit solver-free simulation configuration."""
        from comsol_mcp.evidence.simulation_configuration import (
            normalize_simulation_configuration,
        )

        try:
            return {
                "success": True,
                "configuration": normalize_simulation_configuration(configuration),
                "solver_started": False,
                "filesystem_modified": False,
            }
        except TypeError, ValueError:
            logger.exception("Simulation configuration validation failed")
            return {
                **public_error(
                    "simulation_configuration_rejected",
                    "Simulation configuration validation failed.",
                ),
                "solver_started": False,
                "filesystem_modified": False,
            }

    @mcp.tool()  # type: ignore[untyped-decorator]
    def simulation_configuration_diff(
        left: SimulationConfigurationInput,
        right: SimulationConfigurationInput,
        policy: ConfigurationDiffPolicy | None = None,
    ) -> dict[str, Any]:
        """Classify exact, tolerance, semantic, label, and unavailable differences."""
        from comsol_mcp.evidence.simulation_configuration import (
            compare_simulation_configurations,
        )

        try:
            return {
                "success": True,
                "diff": compare_simulation_configurations(left, right, policy),
                "solver_started": False,
                "filesystem_modified": False,
            }
        except TypeError, ValueError:
            logger.exception("Simulation configuration comparison failed")
            return {
                **public_error(
                    "simulation_configuration_diff_rejected",
                    "Simulation configuration comparison failed.",
                ),
                "solver_started": False,
                "filesystem_modified": False,
            }


__all__ = ["register_configuration_tools"]
