"""MCP adapter for the read-only offline export-manifest validator.

``offline_export_validate`` checks an existing export manifest and its
CSV/VTU/TXT artifacts entirely offline. It never starts COMSOL, Java, MPh,
or JPype and never promotes an export to FEM evidence.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver import MCPServer

from comsol_mcp.contracts.offline_export import OfflineExportValidateInput
from comsol_mcp.evidence.offline_export import OfflineExportError
from comsol_mcp.utils.public_errors import public_error

logger = logging.getLogger(__name__)


def register_offline_export_tools(mcp: MCPServer) -> None:
    """Register the read-only offline export validation tool in every profile."""

    @mcp.tool()  # type: ignore[untyped-decorator]
    def offline_export_validate(
        manifest_path: str,
        base_directory: str | None = None,
        expected_model_sha256: str | None = None,
        limits: Any = None,
    ) -> dict[str, Any]:
        """Validate one export manifest and its files with COMSOL closed."""
        from comsol_mcp.contracts.offline_export import OfflineExportLimits
        from comsol_mcp.evidence.offline_export import (
            OFFLINE_EXPORT_MANIFEST_SCHEMA_NAME,
            OFFLINE_EXPORT_MANIFEST_SCHEMA_VERSION,
            validate_offline_export_manifest,
        )

        try:
            request = OfflineExportValidateInput(
                manifest_path=manifest_path,
                base_directory=base_directory,
                expected_model_sha256=expected_model_sha256,
                limits=limits,
            )
            bounds = request.limits or OfflineExportLimits()
            verdict = validate_offline_export_manifest(
                request.manifest_path,
                request.base_directory,
                expected_model_sha256=request.expected_model_sha256,
                max_manifest_bytes=bounds.max_manifest_bytes,
                max_artifacts=bounds.max_artifacts,
                max_artifact_bytes=bounds.max_artifact_bytes,
                max_expressions=bounds.max_expressions,
                max_parameter_entries=bounds.max_parameter_entries,
                max_time_values=bounds.max_time_values,
            )
        except (OfflineExportError, TypeError, ValueError, OSError) as exc:
            logger.info("Offline export validation refused: %s", exc)
            return {
                **public_error(
                    "offline_export_rejected",
                    "The offline export manifest was not validated.",
                ),
                "solver_started": False,
                "filesystem_modified": False,
            }
        return {
            "success": True,
            "schema_name": OFFLINE_EXPORT_MANIFEST_SCHEMA_NAME,
            "schema_version": OFFLINE_EXPORT_MANIFEST_SCHEMA_VERSION,
            "verdict": verdict,
            "solver_started": False,
            "filesystem_modified": False,
        }


__all__ = ["register_offline_export_tools"]
