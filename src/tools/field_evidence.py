"""Public read-only discovery for bounded Wave Optics field datasets."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from src.evidence.field_discovery import discover_field_datasets

from .session import session_manager


def register_field_evidence_tools(mcp: FastMCP) -> None:
    """Register field-evidence tools that neither solve nor mutate a model."""

    @mcp.tool()
    def wave_optics_field_datasets(
        model_name: str,
        max_datasets: int = 64,
        max_components: int = 32,
    ) -> dict[str, Any]:
        """Discover exact MPh dataset names and clientapi tags on one loaded model."""
        if not isinstance(model_name, str) or not model_name.strip():
            return {"success": False, "error": "model_name must be exact and non-empty"}
        model = session_manager.get_model(model_name)
        if model is None:
            return {"success": False, "error": f"Model not found: {model_name}"}
        try:
            ownership = session_manager.preflight_long_operation()
            if not ownership.get("ready"):
                return {
                    "success": False,
                    "error": "Complete owned-session preflight is required for field dataset discovery",
                    "blockers": ownership.get("blockers", []),
                }
            result = discover_field_datasets(
                model,
                max_datasets=max_datasets,
                max_components=max_components,
            )
            return {
                "success": True,
                "model_name": model_name,
                "ownership_checked": True,
                "solver_started_by_tool": False,
                **result,
            }
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            return {
                "success": False,
                "error": f"Field dataset discovery failed safely: {type(exc).__name__}: {exc}",
            }

__all__ = ["register_field_evidence_tools"]
