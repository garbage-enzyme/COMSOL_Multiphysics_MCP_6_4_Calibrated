"""Public read-only discovery for bounded Wave Optics field datasets."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from src.evidence.field_bundle import validate_field_evidence_request
from src.evidence.field_dataset import collect_existing_dataset_field_evidence
from src.evidence.field_discovery import discover_field_datasets

from .ownership import ownership_manager
from .session import session_manager


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _loaded_source_path(model: Any) -> Path:
    try:
        value = model.file()
    except Exception as exc:
        raise ValueError("loaded model source path is unavailable") from exc
    if value is None:
        raise ValueError("loaded model source path is unavailable")
    path = Path(str(value)).expanduser().resolve()
    if not path.is_file() or path.suffix.casefold() != ".mph":
        raise ValueError("loaded model source must be one existing MPH file")
    return path


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

    @mcp.tool()
    def wave_optics_field_extract(
        model_name: str,
        request: dict[str, Any],
        view_id: str,
    ) -> dict[str, Any]:
        """Extract one existing solved dataset into an owned bounded NPZ manifest."""
        if not isinstance(model_name, str) or not model_name.strip():
            return {"success": False, "error": "model_name must be exact and non-empty"}
        model = session_manager.get_model(model_name)
        if model is None:
            return {"success": False, "error": f"Model not found: {model_name}"}
        try:
            normalized = validate_field_evidence_request(request)
            matches = [
                view for view in normalized["views"] if view["view_id"] == view_id
            ]
            if len(matches) != 1:
                raise ValueError("view_id must identify exactly one requested view")
            view = matches[0]
            if view["source"]["kind"] != "existing_dataset":
                raise ValueError(
                    "public existing-dataset extraction cannot read a validation-matrix source"
                )
            if normalized["render"]["png"]:
                raise ValueError(
                    "PNG rendering is not yet public; request NPZ and manifest only"
                )
            ownership = session_manager.preflight_long_operation()
            if not ownership.get("ready"):
                return {
                    "success": False,
                    "error": "Complete owned-session preflight is required for field extraction",
                    "blockers": ownership.get("blockers", []),
                }
            source_path = _loaded_source_path(model)
            source_before = _sha256_file(source_path)
            expected_source = view["source"]["source_model_sha256"]
            if source_before != expected_source:
                raise ValueError("loaded source SHA-256 does not match the field request")

            relative_root = Path("field_evidence") / normalized["request_fingerprint"]
            artifact_root = ownership_manager.runtime_dir / relative_root
            result = collect_existing_dataset_field_evidence(
                model=model,
                request=normalized,
                view_id=view_id,
                artifact_root=artifact_root,
            )
            source_after = _sha256_file(source_path)
            if source_after != source_before:
                raise RuntimeError("loaded source changed during read-only field extraction")
            return {
                "success": True,
                "model_name": model_name,
                "artifact_root_id": relative_root.as_posix(),
                "source_model_sha256": source_before,
                "source_unchanged": True,
                "ownership_checked": True,
                "solver_started_by_tool": False,
                **result,
            }
        except (ValueError, FileExistsError) as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            return {
                "success": False,
                "error": f"Field extraction failed safely: {type(exc).__name__}: {exc}",
            }

__all__ = ["register_field_evidence_tools"]
