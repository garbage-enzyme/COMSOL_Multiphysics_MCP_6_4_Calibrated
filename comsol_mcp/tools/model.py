"""Model management tools for COMSOL MCP Server."""

import hashlib
import json
import logging
import os
import shutil
import uuid
from pathlib import Path
from tempfile import mkdtemp
from typing import Optional

from mcp.server.mcpserver import MCPServer

from comsol_mcp.durable.io import publish_file_exclusive

from ..utils.runtime_paths import default_runtime_dir
from ..utils.versioning import (
    generate_latest_path,
    generate_version_path,
    parse_version_info,
)
from .session import session_manager

logger = logging.getLogger(__name__)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _save_model_file(
    model,
    file_path: Optional[str] = None,
    format: Optional[str] = None,
) -> str:
    """Save a model, using clientapi for reliable Unicode ``.mph`` paths."""
    normalized_format = format.casefold() if format else "comsol"
    target = file_path or model.file()
    if not target:
        raise ValueError("file_path is required for a model that has not been saved.")
    path = Path(target).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    overwrite = path.exists()
    staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}.save")
    try:
        if normalized_format not in {"comsol", "mph"}:
            model.save(path=str(staging), format=format)
        else:
            model.java.save(str(staging), True)
        if not staging.is_file():
            raise RuntimeError("model save did not create the staging artifact")
        if overwrite:
            os.replace(staging, path)
        else:
            publish_file_exclusive(staging, path)
    finally:
        staging.unlink(missing_ok=True)
    return str(path)


def _clone_model(
    client,
    model,
    new_name: Optional[str] = None,
    *,
    clone_root: Optional[Path] = None,
    existing_names=(),
):
    """Clone a standalone client model through clientapi Save Copy + load."""
    clone_name = new_name or f"{model.name()}_copy"
    if clone_name in {str(name) for name in existing_names}:
        raise ValueError(f"clone name already exists: {clone_name}")
    root = clone_root or (default_runtime_dir() / "model_clones")
    root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(mkdtemp(prefix="comsol_mcp_clone_", dir=str(root)))
    copy_path = temp_dir / "clone.mph"
    cloned_model = None
    try:
        model.java.save(str(copy_path), True)
        cloned_model = client.load(str(copy_path))
        cloned_model.java.label(clone_name)
    except Exception as exc:
        cleanup_errors = []
        if cloned_model is not None:
            try:
                client.remove(cloned_model)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"clone_remove: {cleanup_exc}")
        try:
            copy_path.unlink(missing_ok=True)
            temp_dir.rmdir()
        except Exception as cleanup_exc:
            cleanup_errors.append(f"backing_remove: {cleanup_exc}")
        if cleanup_errors:
            raise RuntimeError(f"{exc}; clone cleanup failed: {'; '.join(cleanup_errors)}") from exc
        raise
    return cloned_model, str(copy_path)


def _metadata_path(model_path: Path) -> Path:
    return model_path.with_suffix(".metadata.json")


def _save_model_version_bundle(
    model,
    versioned_path: str,
    latest_path: str,
    *,
    description: Optional[str],
) -> dict[str, str]:
    """Publish one Save Copy snapshot as a rollback-capable version/latest bundle."""
    version = Path(versioned_path).resolve()
    latest = Path(latest_path).resolve()
    if version.parent != latest.parent:
        raise ValueError("versioned and latest model paths must share one directory")
    version_metadata = _metadata_path(version)
    latest_metadata = _metadata_path(latest)
    if version.exists() or version_metadata.exists():
        raise FileExistsError("versioned model or metadata already exists")
    version.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    stages = {
        version: version.with_name(f".{version.name}.{token}.stage"),
        version_metadata: version_metadata.with_name(f".{version_metadata.name}.{token}.stage"),
        latest: latest.with_name(f".{latest.name}.{token}.stage"),
        latest_metadata: latest_metadata.with_name(f".{latest_metadata.name}.{token}.stage"),
    }
    backups = {
        target: target.with_name(f".{target.name}.{token}.backup")
        for target in (latest, latest_metadata)
        if target.exists()
    }
    published = []
    committed = False
    try:
        model.java.save(str(stages[version]), True)
        if not stages[version].is_file():
            raise RuntimeError("model Save Copy did not create version staging")
        shutil.copyfile(stages[version], stages[latest])
        metadata = {
            "schema_name": "comsol_mcp.model_version_metadata",
            "schema_version": "1.0.0",
            "description": description,
            "model_name": str(model.name()),
            "snapshot_role": "versioned_and_latest_copy",
        }
        encoded = json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        stages[version_metadata].write_text(encoded, encoding="utf-8")
        stages[latest_metadata].write_text(encoded, encoding="utf-8")
        for target, backup in backups.items():
            os.replace(target, backup)
        for target in (version, version_metadata, latest, latest_metadata):
            publish_file_exclusive(stages[target], target)
            published.append(target)
        committed = True
    except Exception as exc:
        rollback_errors = []
        for target in reversed(published):
            try:
                target.unlink(missing_ok=True)
            except OSError as rollback_exc:
                rollback_errors.append(f"remove {target.name}: {rollback_exc}")
        for target, backup in backups.items():
            if backup.exists():
                try:
                    os.replace(backup, target)
                except OSError as rollback_exc:
                    rollback_errors.append(f"restore {target.name}: {rollback_exc}")
        if rollback_errors:
            raise RuntimeError(
                f"{exc}; version bundle rollback failed: {'; '.join(rollback_errors)}"
            ) from exc
        raise
    finally:
        for stage in stages.values():
            stage.unlink(missing_ok=True)
        if committed:
            for backup in backups.values():
                backup.unlink(missing_ok=True)
    return {
        "version_path": str(version),
        "latest_path": str(latest),
        "version_metadata_path": str(version_metadata),
        "latest_metadata_path": str(latest_metadata),
    }


def _list_model_components(model) -> list[dict[str, str]]:
    """Return component metadata with clientapi strings normalized for JSON."""
    jm = model.java
    components = []
    for comp_tag in jm.component().tags():
        comp = jm.component().get(comp_tag)
        if comp is None:
            continue
        tag = str(comp.tag())
        label = str(comp.label()) if hasattr(comp, "label") else tag
        components.append({"name": tag, "label": label})
    return components


def create_model_component(model, component_name: str, space_dimension: int) -> dict:
    """Create a component without claiming its later geometry dimension is applied."""
    if not isinstance(component_name, str) or not component_name.strip():
        return {"success": False, "error": "component_name must be nonempty"}
    if isinstance(space_dimension, bool) or space_dimension not in {0, 1, 2, 3, 20, 30}:
        return {"success": False, "error": "space_dimension is unsupported"}
    components = model.java.component()
    if component_name in {str(tag) for tag in components.tags()}:
        return {"success": False, "error": f"Component already exists: {component_name}"}
    components.create(component_name, True)
    return {
        "success": True,
        "component": component_name,
        "requested_geometry_space_dimension": space_dimension,
        "space_dimension_applied": False,
        "next_step": "Create a geometry sequence with geometry_create to apply the dimension.",
    }


def register_model_tools(mcp: MCPServer) -> None:
    """Register model management tools with the MCP server."""

    @mcp.tool()
    def model_load(file_path: str, set_current: bool = True) -> dict:
        """
        Load a COMSOL model from a .mph file.

        Args:
            file_path: Absolute or relative path to the .mph model file
            set_current: Whether to set this as the current active model (default: True)

        Returns:
            Model info including name, file path, and version, or error message
        """
        if not session_manager.is_connected:
            return {
                "success": False,
                "error": "No active COMSOL session. Start with comsol_start first.",
            }

        client = session_manager.client
        if client is None:
            return {"success": False, "error": "Client not available."}

        try:
            path = Path(file_path).resolve()
            if not path.exists():
                return {"success": False, "error": f"File not found: {file_path}"}
            if not path.suffix.lower() == ".mph":
                return {"success": False, "error": f"File must be a .mph file: {file_path}"}

            source_hash_before = _sha256_file(path)
            model = client.load(str(path))
            source_hash_after = _sha256_file(path)
            if source_hash_after != source_hash_before:
                try:
                    client.remove(model)
                except Exception:
                    return {
                        "success": False,
                        "error": (
                            "Model source changed while it was being loaded, and the "
                            "loaded model could not be removed. Reset the session."
                        ),
                    }
                return {
                    "success": False,
                    "error": "Model source changed while it was being loaded.",
                }
            name = session_manager.add_model(
                model,
                source_identity={
                    "source_path": str(path),
                    "source_sha256": source_hash_after,
                    "capture": "load_bracketed",
                },
            )

            if set_current:
                session_manager.set_current_model(name)

            version_info = parse_version_info(name)

            return {
                "success": True,
                "model": {
                    "name": name,
                    "file": str(path),
                    "source_sha256": source_hash_after,
                    "comsol_version": model.version(),
                    "is_versioned": version_info is not None,
                    "version_info": version_info,
                },
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to load model: {str(e)}"}

    @mcp.tool()
    def model_create(name: Optional[str] = None, set_current: bool = True) -> dict:
        """
        Create a new empty COMSOL model.

        Args:
            name: Optional name for the model (auto-generated if not provided)
            set_current: Whether to set this as the current active model (default: True)

        Returns:
            Model info including name, or error message
        """
        if not session_manager.is_connected:
            return {
                "success": False,
                "error": "No active COMSOL session. Start with comsol_start first.",
            }

        client = session_manager.client
        if client is None:
            return {"success": False, "error": "Client not available."}

        try:
            model = client.create(name)
            model_name = session_manager.add_model(model)

            if set_current:
                session_manager.set_current_model(model_name)

            return {
                "success": True,
                "model": {
                    "name": model_name,
                    "is_new": True,
                },
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to create model: {str(e)}"}

    @mcp.tool()
    def model_create_component(
        component_name: str = "comp1", space_dimension: int = 3, model_name: Optional[str] = None
    ) -> dict:
        """
        Create a component in the model (required before adding geometry/physics).

        Components are containers for geometry, physics, materials, and mesh.
        Must be created before adding geometry or physics.

        Args:
            component_name: Name for the component (default: 'comp1')
            space_dimension: Requested dimension for the later geometry sequence.
                Components themselves have no applied space dimension; pass this
                value to geometry_create (0, 1, 2, 3, 20, or 30).
            model_name: Model name (default: current model)

        Returns:
            Created component info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }

        try:
            result = create_model_component(model, component_name, space_dimension)
            if result.get("success"):
                result["model"] = model.name()
            return result
        except Exception as e:
            return {"success": False, "error": f"Failed to create component: {str(e)}"}

    @mcp.tool()
    def model_list_components(model_name: Optional[str] = None) -> dict:
        """
        List all components in a model.

        Args:
            model_name: Model name (default: current model)

        Returns:
            List of component names
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }

        try:
            components = _list_model_components(model)

            return {
                "success": True,
                "components": components,
                "count": len(components),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to list components: {str(e)}"}

    @mcp.tool()
    def model_save(
        model_name: Optional[str] = None,
        file_path: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict:
        """
        Save a COMSOL model to file.

        Args:
            model_name: Name of the model to save (default: current model)
            file_path: Path to save to (default: original file path)
            format: Save format - 'Comsol', 'Java', 'Matlab', or 'VBA' (default: Comsol/.mph)

        Returns:
            Save confirmation with file path, or error message
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }

        try:
            saved_path = _save_model_file(model, file_path=file_path, format=format)

            return {
                "success": True,
                "model": model.name(),
                "saved_to": str(saved_path),
                "format": format or "Comsol",
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to save model: {str(e)}"}

    @mcp.tool()
    def model_save_version(
        model_name: Optional[str] = None,
        description: Optional[str] = None,
        base_path: Optional[str] = None,
    ) -> dict:
        """
        Save a model with a timestamp version suffix.

        Creates a new file with structured path:
        <runtime>/models/{model_name}/{model_name}_{timestamp}.mph

        Also saves a 'latest' copy:
        <runtime>/models/{model_name}/{model_name}_latest.mph

        Useful for version control and design iterations.

        Args:
            model_name: Name of the model to save (default: current model)
            description: Optional description persisted in version/latest JSON sidecars
            base_path: Optional model-storage root. Defaults to the runtime
                directory's ``models`` subdirectory.

        Returns:
            Save confirmation with version/latest model and metadata paths, or error message
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }

        try:
            # Get model name for directory structure
            name = model.name()

            # Generate versioned path using new structure
            versioned_path = generate_version_path(name, base_path=base_path)

            latest_path = generate_latest_path(name, base_path=base_path)
            published = _save_model_version_bundle(
                model,
                versioned_path,
                latest_path,
                description=description,
            )

            return {
                "success": True,
                "model": name,
                **published,
                "description": description,
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to save version: {str(e)}"}

    @mcp.tool()
    def model_list() -> dict:
        """
        List all models currently loaded in the COMSOL session.

        Returns:
            List of models with their names, file paths, and status
        """
        if not session_manager.is_connected:
            return {"success": False, "error": "No active COMSOL session."}

        models = session_manager.models
        current = session_manager.current_model

        model_list = []
        for name, model in models.items():
            info = {
                "name": name,
                "is_current": name == current,
            }
            try:
                info["file"] = model.file()
                info["comsol_version"] = model.version()
            except Exception as exc:
                logger.debug("Could not inspect model metadata for %s: %s", name, exc)
            model_list.append(info)

        return {
            "success": True,
            "models": model_list,
            "count": len(model_list),
            "current_model": current,
        }

    @mcp.tool()
    def model_set_current(model_name: str) -> dict:
        """
        Set the current active model for subsequent operations.

        Args:
            model_name: Name of the model to set as current

        Returns:
            Confirmation or error message
        """
        if session_manager.set_current_model(model_name):
            return {
                "success": True,
                "current_model": model_name,
            }
        return {"success": False, "error": f"Model not found: {model_name}"}

    @mcp.tool()
    def model_clone(
        model_name: Optional[str] = None, new_name: Optional[str] = None, set_current: bool = False
    ) -> dict:
        """
        Clone a model to create a copy for comparison or modification.

        Args:
            model_name: Name of the model to clone (default: current model)
            new_name: Name for the cloned model (auto-generated if not provided)
            set_current: Whether to set the clone as current model (default: False)

        Returns:
            Info about the cloned model, or error message
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }

        try:
            client = session_manager.client
            if client is None:
                return {"success": False, "error": "Client not available."}

            cloned_model, cleanup_path = _clone_model(
                client,
                model,
                new_name,
                existing_names=session_manager.models,
            )
            clone_name = session_manager.add_model(
                cloned_model,
                cleanup_path=cleanup_path,
            )

            if set_current:
                session_manager.set_current_model(clone_name)

            return {
                "success": True,
                "original": model.name(),
                "clone": clone_name,
                "is_current": clone_name == session_manager.current_model,
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to clone model: {str(e)}"}

    @mcp.tool()
    def model_remove(model_name: str) -> dict:
        """
        Remove a model from memory.

        Args:
            model_name: Name of the model to remove

        Returns:
            Confirmation or error message
        """
        if session_manager.remove_model(model_name):
            return {
                "success": True,
                "removed": model_name,
                "current_model": session_manager.current_model,
            }
        return {"success": False, "error": f"Failed to remove model: {model_name}"}

    @mcp.tool()
    def model_inspect(model_name: Optional[str] = None) -> dict:
        """
        Get detailed information about a model's structure and contents.

        Args:
            model_name: Name of the model to inspect (default: current model)

        Returns:
            Detailed model structure including parameters, physics, studies, etc.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }

        try:
            info = {
                "name": model.name(),
                "file": model.file(),
                "comsol_version": model.version(),
                "parameters": dict(model.parameters()) if model.parameters() else {},
                "functions": model.functions(),
                "components": model.components(),
                "geometries": model.geometries(),
                "selections": model.selections(),
                "physics": model.physics(),
                "multiphysics": model.multiphysics(),
                "materials": model.materials(),
                "meshes": model.meshes(),
                "studies": model.studies(),
                "solutions": model.solutions(),
                "datasets": model.datasets(),
                "plots": model.plots(),
                "exports": model.exports(),
                "modules": model.modules(),
            }

            problems = model.problems()
            if problems:
                info["problems"] = problems

            return {"success": True, "model": info}
        except Exception as e:
            return {"success": False, "error": f"Failed to inspect model: {str(e)}"}
