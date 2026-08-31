"""MCP adapter for bounded offline `.mph` archive inspection."""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver import MCPServer

from comsol_mcp.contracts.mph_inspection import (
    MphDiffInput,
    MphInspectionInput,
    MphInspectionLimits,
)
from comsol_mcp.evidence.inspection.archive import MphInspectionError
from comsol_mcp.utils.public_errors import public_error

logger = logging.getLogger(__name__)


def _public_rejection(exc: MphInspectionError, message: str) -> dict[str, Any]:
    logger.info("MPH inspection refused: %s", exc.reason_code)
    return {
        **public_error(exc.reason_code, message),
        "solver_started": False,
        "filesystem_modified": False,
    }


def _internal_rejection(exc: Exception, action: str) -> dict[str, Any]:
    logger.exception("MPH inspection failed")
    return {
        **public_error(
            "mph_inspection_rejected",
            f"The MPH {action} request was rejected.",
        ),
        "solver_started": False,
        "filesystem_modified": False,
    }


def register_mph_inspection_tools(mcp: MCPServer) -> None:
    """Register the read-only offline MPH inspection tools in every profile."""

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
            return _public_rejection(exc, "The MPH archive was not inspected.")
        except (TypeError, ValueError, OSError, OverflowError, RecursionError) as exc:
            return _internal_rejection(exc, "inspection")
        return {
            "success": True,
            "schema_name": MPH_INSPECTION_SUMMARY_SCHEMA_NAME,
            "schema_version": MPH_INSPECTION_SUMMARY_SCHEMA_VERSION,
            "summary": summary,
            "solver_started": False,
            "filesystem_modified": False,
        }

    @mcp.tool()  # type: ignore[untyped-decorator]
    def mph_diff(
        left_path: str,
        right_path: str,
        limits: MphInspectionLimits | None = None,
    ) -> dict[str, Any]:
        """Compare two offline `.mph` archives without starting COMSOL."""
        from comsol_mcp.evidence.inspection.diff import (
            MPH_DIFF_SCHEMA_NAME,
            MPH_DIFF_SCHEMA_VERSION,
            build_mph_diff,
        )

        try:
            request = MphDiffInput(left_path=left_path, right_path=right_path, limits=limits)
            diff = build_mph_diff(request.left_path, request.right_path, request.limits)
        except MphInspectionError as exc:
            return _public_rejection(exc, "The MPH archives were not compared.")
        except (TypeError, ValueError, OSError, OverflowError, RecursionError) as exc:
            return _internal_rejection(exc, "diff")
        return {
            "success": True,
            "schema_name": MPH_DIFF_SCHEMA_NAME,
            "schema_version": MPH_DIFF_SCHEMA_VERSION,
            "diff": diff,
            "solver_started": False,
            "filesystem_modified": False,
        }


__all__ = ["register_mph_inspection_tools"]
