"""MCP adapter for the read-only offline model-identity contract.

The ``model_identity`` tool never starts COMSOL, Java, MPh, or JPype. Its
default session provider reads only already-available in-process state: a
disconnected owned session or a detached shared-session manager yields a
structured unavailable result without constructing any client.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from mcp.server.mcpserver import MCPServer

from comsol_mcp.contracts.model_identity import ModelIdentityInput
from comsol_mcp.contracts.mph_inspection import MphInspectionLimits
from comsol_mcp.evidence.inspection.archive import MphInspectionError
from comsol_mcp.evidence.model_identity import ModelIdentityError
from comsol_mcp.utils.public_errors import public_error

logger = logging.getLogger(__name__)


def _unavailable_snapshot(*reason_codes: str) -> dict[str, Any]:
    return {
        "available": False,
        "reason_codes": list(reason_codes),
        "shared_session": False,
    }


def _passive_live_session_provider() -> dict[str, Any] | None:
    """Return passive in-process session identity without touching COMSOL.

    Reading status of an already-connected owned client is passive; when no
    client is connected this returns an explicit unavailable snapshot and
    never constructs a client or acquires a lease. Any inconsistent or
    unreadable status degrades to structured unavailability: this provider
    always returns a well-formed snapshot and never raises.
    """
    try:
        from comsol_mcp.tools.session import session_manager

        status = session_manager.get_status()
    except Exception:
        logger.info("model_identity session status was unavailable", exc_info=True)
        return _unavailable_snapshot("owned_session_status_unavailable")
    shared_attached = _passive_shared_session_attached()
    if not isinstance(status, Mapping) or not status.get("connected"):
        snapshot = _unavailable_snapshot("no_connected_session")
        snapshot["shared_session"] = shared_attached
        return snapshot
    models = status.get("models")
    current_model = status.get("current_model")
    active_tag = None
    if isinstance(current_model, str) and current_model:
        active_tag = current_model
    elif isinstance(models, list) and models:
        first = models[0]
        if isinstance(first, Mapping) and isinstance(first.get("name"), str):
            active_tag = first["name"]
    if not active_tag:
        # A connected client with no tracked model has no active identity to
        # report; that is a structured absence, never a guessable state.
        snapshot = _unavailable_snapshot("no_active_tracked_model")
        snapshot["shared_session"] = shared_attached
        return snapshot
    revision = None
    if isinstance(models, list):
        for row in models:
            if isinstance(row, Mapping) and row.get("name") == active_tag:
                candidate = row.get("revision_sha256")
                revision = candidate if isinstance(candidate, str) and candidate else None
                break
    comsol_version = status.get("version")
    return {
        "available": True,
        "active_model_tag": active_tag,
        "bound_model_tag": active_tag,
        "label": None,
        "revision": revision,
        "comsol_version": comsol_version if isinstance(comsol_version, str) else None,
        "shared_session": shared_attached,
    }


def _passive_shared_session_attached() -> bool:
    try:
        from comsol_mcp.tools.shared_session import shared_session_manager

        shared_status = shared_session_manager.status()
    except Exception:
        logger.info("model_identity shared-session status was unavailable", exc_info=True)
        return False
    return bool(isinstance(shared_status, Mapping) and shared_status.get("attached"))


def register_model_identity_tools(mcp: MCPServer) -> None:
    """Register the read-only model-identity tool in every profile."""

    @mcp.tool()  # type: ignore[untyped-decorator]
    def model_identity(
        file_path: str,
        source_path: str | None = None,
        checkpoint_path: str | None = None,
        expected_file_sha256: str | None = None,
        expected_source_sha256: str | None = None,
        expected_checkpoint_sha256: str | None = None,
        request_session_identity: bool = False,
        limits: MphInspectionLimits | None = None,
    ) -> dict[str, Any]:
        """Bind one offline `.mph` file to its read-only identity contract."""
        from comsol_mcp.evidence.model_identity import (
            MODEL_IDENTITY_SCHEMA_NAME,
            MODEL_IDENTITY_SCHEMA_VERSION,
            build_model_identity,
        )

        try:
            request = ModelIdentityInput(
                file_path=file_path,
                source_path=source_path,
                checkpoint_path=checkpoint_path,
                expected_file_sha256=expected_file_sha256,
                expected_source_sha256=expected_source_sha256,
                expected_checkpoint_sha256=expected_checkpoint_sha256,
                request_session_identity=request_session_identity,
                limits=limits,
            )
            provider = _passive_live_session_provider if request.request_session_identity else None
            identity = build_model_identity(
                request.file_path,
                source_path=request.source_path,
                checkpoint_path=request.checkpoint_path,
                expected_file_sha256=request.expected_file_sha256,
                expected_source_sha256=request.expected_source_sha256,
                expected_checkpoint_sha256=request.expected_checkpoint_sha256,
                limits=request.limits or MphInspectionLimits(),
                session_provider=provider,
            )
        except (MphInspectionError, ModelIdentityError) as exc:
            logger.info("Model identity refused: %s", exc.reason_code)
            return {
                **public_error(exc.reason_code, "The model identity request was refused."),
                "solver_started": False,
                "filesystem_modified": False,
            }
        except TypeError, ValueError, OSError, OverflowError, RecursionError:
            logger.exception("Model identity failed")
            return {
                **public_error(
                    "model_identity_rejected",
                    "The model identity request was rejected.",
                ),
                "solver_started": False,
                "filesystem_modified": False,
            }
        return {
            "success": True,
            "schema_name": MODEL_IDENTITY_SCHEMA_NAME,
            "schema_version": MODEL_IDENTITY_SCHEMA_VERSION,
            "identity": identity,
            "solver_started": False,
            "filesystem_modified": False,
        }


__all__ = ["register_model_identity_tools"]
