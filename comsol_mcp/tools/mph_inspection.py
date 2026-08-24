"""MCP adapter for bounded offline `.mph` archive inspection."""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver import MCPServer

from comsol_mcp.contracts.mph_inspection import MphInspectionInput, MphInspectionLimits
from comsol_mcp.evidence.inspection.archive import MphInspectionError
from comsol_mcp.utils.public_errors import public_error

logger = logging.getLogger(__name__)


def register_mph_inspection_tools(mcp: MCPServer) -> None:
    """Register the read-only offline MPH inspection tool in every profile."""

    @mcp.tool()  # type: ignore[untyped-decorator]
    def mph_inspect(
        file_path: str,
        limits: MphInspectionLimits | None = None,
    ) -> dict[str, Any]:
        """Inspect one offline `.mph` archive without starting COMSOL."""
        from comsol_mcp.evidence.inspection.summary import (
            MPH_INSPECTION_SUMMARY_SCHEMA_NAME,
            MPH_INSPECTION_SUMMARY_SCHEMA_VERSION,
            build_mph_inspection_summary,
        )

        try:
            request = MphInspectionInput(file_path=file_path, limits=limits)
            summary = build_mph_inspection_summary(request.file_path, request.limits)
        except MphInspectionError as exc:
            logger.info("MPH inspection refused: %s", exc.reason_code)
            return {
                **public_error(exc.reason_code, "The MPH archive was not inspected."),
                "solver_started": False,
                "filesystem_modified": False,
            }
        except TypeError, ValueError, OSError, OverflowError, RecursionError:
            logger.exception("MPH inspection failed")
            return {
                **public_error(
                    "mph_inspection_rejected",
                    "The MPH inspection request was rejected.",
                ),
                "solver_started": False,
                "filesystem_modified": False,
            }
        return {
            "success": True,
            "schema_name": MPH_INSPECTION_SUMMARY_SCHEMA_NAME,
            "schema_version": MPH_INSPECTION_SUMMARY_SCHEMA_VERSION,
            "summary": summary,
            "solver_started": False,
            "filesystem_modified": False,
        }


__all__ = ["register_mph_inspection_tools"]
