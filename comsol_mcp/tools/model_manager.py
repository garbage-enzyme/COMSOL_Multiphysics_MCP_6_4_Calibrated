"""Public MCP tools for the default-off COMSOL Model Manager feature.

The tools are thin adapters. Every policy decision — the feature gate, the
upload setting, destination and revision collisions, the durable journal write,
and the receipt — lives in :mod:`comsol_mcp.shared_session.model_manager` and
:mod:`comsol_mcp.shared_session.dbmodel_operations`, both of which are fully
tested without a solver.

The dispatch callable is resolved at call time rather than at registration time,
so importing this module never imports a heavy COMSOL or MPh stack. A client that
only lists tools pays nothing for this surface.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from comsol_mcp.shared_session.model_manager import (
    FEATURE_NAME,
    ModelManagerContext,
    ModelManagerRuntime,
)
from comsol_mcp.tools.shared_session import shared_session_manager


def _feature_flags(mcp: MCPServer) -> tuple[bool, bool]:
    """Read the feature gate and the upload setting for this server.

    A missing selection or an unreadable setting means the feature is off, which
    is the safe direction for a default-off capability.
    """
    selection = getattr(mcp, "profile_selection", None)
    predicate = getattr(selection, "feature_enabled", None)
    feature_enabled = bool(
        selection is not None and callable(predicate) and predicate(FEATURE_NAME)
    )
    if not feature_enabled:
        return False, False
    upload_enabled = False
    try:
        from comsol_mcp.settings import load_settings

        upload_enabled = bool(load_settings()["model_manager"]["upload_enabled"])
    except Exception:
        # An unreadable setting must not silently grant write access.
        upload_enabled = False
    return feature_enabled, upload_enabled


def _resolve_dispatch() -> Any:
    """Resolve the Model Manager transport, or ``None`` when unavailable.

    The transport needs a live attached COMSOL session. Returning ``None`` lets
    the runtime refuse with a recorded reason instead of raising, so the caller
    still receives a durable receipt.
    """
    adapter = getattr(shared_session_manager, "model_manager_dispatch", None)
    return adapter if callable(adapter) else None


def _runtime(mcp: MCPServer) -> ModelManagerRuntime:
    from comsol_mcp.utils.runtime_paths import default_runtime_dir

    feature_enabled, upload_enabled = _feature_flags(mcp)
    status = shared_session_manager.status()
    attached = bool(status.get("attached"))
    identity = status.get("model_tag") or status.get("server_identity") or None
    if not isinstance(identity, str) or not identity:
        identity = None
    return ModelManagerRuntime(
        feature_enabled=feature_enabled,
        upload_enabled=upload_enabled,
        context=ModelManagerContext(
            attached=attached,
            session_identity=identity,
            dispatch=_resolve_dispatch() if (attached and feature_enabled) else None,
        ),
        runtime_dir=default_runtime_dir(),
    )


def register_model_manager_tools(mcp: MCPServer) -> None:
    """Register the bounded Model Manager operations."""

    @mcp.tool()
    def model_manager_status() -> dict[str, Any]:
        """Report Model Manager gate, attachment, and journalled recovery state."""
        runtime = _runtime(mcp)
        return {
            "success": True,
            "capabilities": runtime.capabilities(),
            "recovery": runtime.recovery_report(),
        }

    @mcp.tool()
    def model_manager_open(uri: str) -> dict[str, Any]:
        """Open one Model Manager model or experiment data item."""
        return _runtime(mcp).open(uri=uri)

    @mcp.tool()
    def model_manager_run(
        uri: str, resource_policy: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Run a Model Manager model with the caller's own resource policy."""
        return _runtime(mcp).run(uri=uri, resource_policy=resource_policy)

    @mcp.tool()
    def model_manager_download(
        uri: str,
        destination: str,
        destination_exists: bool = False,
        existing_sha256: str | None = None,
        incoming_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Download to a caller-selected local .mph destination; never overwrites."""
        return _runtime(mcp).download(
            uri=uri,
            destination=destination,
            destination_exists=destination_exists,
            existing_sha256=existing_sha256,
            incoming_sha256=incoming_sha256,
        )

    @mcp.tool()
    def model_manager_upload(
        uri: str,
        derived_source_identity: dict[str, Any],
        expected_revision: str | None = None,
        observed_revision: str | None = None,
    ) -> dict[str, Any]:
        """Save a derived copy to the Model Manager; requires the upload setting."""
        return _runtime(mcp).upload(
            uri=uri,
            derived_source_identity=derived_source_identity,
            expected_revision=expected_revision,
            observed_revision=observed_revision,
        )


__all__ = ["register_model_manager_tools"]
