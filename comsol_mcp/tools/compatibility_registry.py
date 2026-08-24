"""MCP adapter for the read-only compatibility and skill-layer registry.

``runtime_compatibility_status`` reports active runtime identities, supported
ranges, the selected profile, and skill-layer file hashes. It never selects a
fallback runtime or profile. Bound COMSOL identity appears only when the
caller explicitly requests it and an in-process session is already connected;
the passive provider never constructs a client or starts COMSOL.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from mcp.server.mcpserver import MCPServer

logger = logging.getLogger(__name__)


def _passive_bound_comsol_provider() -> dict[str, Any] | None:
    """Read bound-COMSOL identity from already-available session state only."""
    try:
        from comsol_mcp.tools.session import session_manager

        status = session_manager.get_status()
    except Exception:
        logger.info("compatibility registry session status was unavailable", exc_info=True)
        return {"available": False}
    if not isinstance(status, Mapping) or not status.get("connected"):
        return {"available": False}
    version = status.get("version")
    if not isinstance(version, str) or not version:
        return {"available": False}
    shared_attached = False
    try:
        from comsol_mcp.tools.shared_session import shared_session_manager

        shared_status = shared_session_manager.status()
        shared_attached = bool(isinstance(shared_status, Mapping) and shared_status.get("attached"))
    except Exception:
        logger.info("compatibility registry shared-session status was unavailable", exc_info=True)
    return {"available": True, "comsol_build": version, "shared_session": shared_attached}


def register_compatibility_registry_tools(mcp: MCPServer) -> None:
    """Register the read-only compatibility registry tool in every profile."""

    @mcp.tool()  # type: ignore[untyped-decorator]
    def runtime_compatibility_status(
        request_comsol_identity: bool = False,
    ) -> dict[str, Any]:
        """Report runtime/profile/skill identities without guessing."""
        from comsol_mcp.evidence.compatibility_registry import (
            COMPATIBILITY_REGISTRY_SCHEMA_NAME,
            COMPATIBILITY_REGISTRY_SCHEMA_VERSION,
            build_compatibility_registry,
        )

        selection = getattr(mcp, "profile_selection", None)
        profile_name = getattr(selection, "name", None)
        feature_enabled = getattr(selection, "feature_enabled", None)
        enabled_features = tuple(
            feature
            for feature in ("lexical_docs", "semantic_docs", "shared_server")
            if callable(feature_enabled) and feature_enabled(feature)
        )
        if not isinstance(profile_name, str) or not profile_name:
            # A bare server without a startup selection is reported under the
            # documented default profile rather than guessed from the host.
            from comsol_mcp.tools.profiles import DEFAULT_PROFILE

            profile_name = DEFAULT_PROFILE
        try:
            provider = _passive_bound_comsol_provider if request_comsol_identity else None
            registry = build_compatibility_registry(
                profile_name=profile_name,
                enabled_features=enabled_features,
                comsol_bound_provider=provider,
            )
        except (TypeError, ValueError) as exc:
            logger.info("Compatibility registry refused: %s", exc)
            from comsol_mcp.utils.public_errors import public_error

            return {
                **public_error(
                    "compatibility_registry_invalid",
                    "The compatibility registry request was refused.",
                ),
                "solver_started": False,
                "filesystem_modified": False,
            }
        return {
            "success": True,
            "schema_name": COMPATIBILITY_REGISTRY_SCHEMA_NAME,
            "schema_version": COMPATIBILITY_REGISTRY_SCHEMA_VERSION,
            "registry": registry,
            "solver_started": False,
            "filesystem_modified": False,
        }


__all__ = ["register_compatibility_registry_tools"]
