"""Mesh tools for COMSOL MCP Server."""

from typing import Optional

from mcp.server.fastmcp import FastMCP

from .session import session_manager


def _get_component_java(java_model, component_name=None):
    """Return an explicit component or the model's first component."""
    if component_name:
        return java_model.component(component_name)
    components = java_model.component()
    if components.size() == 0:
        raise ValueError("No components defined in the model.")
    return java_model.component(str(components.tags()[0]))


def _get_geometry_tag(component, geometry_name=None):
    """Return an explicit geometry tag or the component's first geometry."""
    if geometry_name:
        return geometry_name
    geometries = component.geom()
    if geometries.size() == 0:
        raise ValueError("No geometries defined in the component.")
    return str(geometries.tags()[0])


def _mesh_tags(component):
    """Return stable Java tags for all mesh sequences in a component."""
    return [str(tag) for tag in component.mesh().tags()]


def _safe_label(java_node):
    """Return a usable label without leaking Java replacement characters."""
    try:
        label = java_node.label()
        if label and "\ufffd" not in label:
            return str(label)
    except Exception:
        pass
    return None


def register_mesh_tools(mcp: FastMCP) -> None:
    """Register mesh tools with the MCP server."""

    @mcp.tool()
    def mesh_list(
        component_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        List mesh sequences by stable COMSOL Java tag.

        Args:
            component_name: Component name (default: all components)
            model_name: Model name (default: current model)

        Returns:
            Mesh sequence tags, components, and usable labels
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            java_model = model.java
            components = java_model.component()
            mesh_items = []

            for component_tag in components.tags():
                comp = java_model.component(str(component_tag))
                if component_name and comp.tag() != component_name:
                    continue
                for mesh_tag in comp.mesh().tags():
                    java_mesh = comp.mesh(str(mesh_tag))
                    item = {
                        "tag": str(java_mesh.tag()),
                        "component": str(comp.tag()),
                    }
                    label = _safe_label(java_mesh)
                    if label:
                        item["label"] = label
                    mesh_items.append(item)

            if component_name and not any(
                item["component"] == component_name for item in mesh_items
            ):
                _get_component_java(java_model, component_name)

            return {
                "success": True,
                "meshes": mesh_items,
                "count": len(mesh_items),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to list meshes: {str(e)}"}

    @mcp.tool()
    def mesh_create_sequence(
        mesh_name: str = "mesh1",
        geometry_name: Optional[str] = None,
        component_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Create a mesh sequence bound to a geometry.

        Args:
            mesh_name: Mesh sequence tag (default: "mesh1")
            geometry_name: Geometry sequence tag (default: first geometry)
            component_name: Component name (default: first component)
            model_name: Model name (default: current model)

        Returns:
            Created mesh sequence info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            comp = _get_component_java(model.java, component_name)
            existing_tags = _mesh_tags(comp)
            if mesh_name in existing_tags:
                return {
                    "success": False,
                    "error": f"Mesh sequence already exists: {mesh_name}",
                    "available_meshes": existing_tags,
                }

            geom_tag = _get_geometry_tag(comp, geometry_name)
            java_mesh = comp.mesh().create(mesh_name, geom_tag)

            return {
                "success": True,
                "mesh": {
                    "tag": str(java_mesh.tag()),
                    "component": str(comp.tag()),
                    "geometry": geom_tag,
                },
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to create mesh sequence: {str(e)}"
            }

    @mcp.tool()
    def mesh_create(
        mesh_name: Optional[str] = None,
        geometry_name: Optional[str] = None,
        component_name: Optional[str] = None,
        auto_create: bool = True,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Run one or all mesh sequences, creating a default sequence when needed.

        Args:
            mesh_name: Mesh sequence tag (default: all, or "mesh1" when created)
            geometry_name: Geometry tag used when creating a mesh sequence
            component_name: Component name (default: first component)
            auto_create: Create a missing mesh sequence (default: True)
            model_name: Model name (default: current model)

        Returns:
            Mesh generation status
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            java_model = model.java
            comp = _get_component_java(java_model, component_name)
            mesh_tags = _mesh_tags(comp)
            created_sequence = False

            if mesh_name:
                target_tags = [mesh_name]
                if mesh_name not in mesh_tags:
                    if not auto_create:
                        return {
                            "success": False,
                            "error": f"Mesh sequence not found: {mesh_name}",
                            "available_meshes": mesh_tags,
                        }
                    geom_tag = _get_geometry_tag(comp, geometry_name)
                    comp.mesh().create(mesh_name, geom_tag)
                    created_sequence = True
            elif mesh_tags:
                target_tags = mesh_tags
            elif auto_create:
                geom_tag = _get_geometry_tag(comp, geometry_name)
                comp.mesh().create("mesh1", geom_tag)
                target_tags = ["mesh1"]
                created_sequence = True
            else:
                return {
                    "success": False,
                    "error": "No mesh sequences defined in the model.",
                }

            for target_tag in target_tags:
                comp.mesh(target_tag).run()

            return {
                "success": True,
                "meshes": target_tags,
                "component": str(comp.tag()),
                "auto_created": created_sequence,
                "message": f"Mesh created: {', '.join(target_tags)}",
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to create mesh: {str(e)}"}

    @mcp.tool()
    def mesh_info(
        mesh_name: Optional[str] = None,
        component_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Get mesh information using a stable Java tag.

        Args:
            mesh_name: Mesh sequence tag (default: first mesh)
            component_name: Component name (default: first component)
            model_name: Model name (default: current model)

        Returns:
            Mesh tag, component, geometry, and feature information
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            comp = _get_component_java(model.java, component_name)
            mesh_tags = _mesh_tags(comp)
            if not mesh_tags:
                return {"success": False, "error": "No meshes defined in model."}

            target_tag = mesh_name or mesh_tags[0]
            if target_tag not in mesh_tags:
                return {
                    "success": False,
                    "error": f"Mesh not found: {target_tag}",
                    "available_meshes": mesh_tags,
                }

            java_mesh = comp.mesh(target_tag)
            features = []
            feature_list = java_mesh.feature()
            for feature_tag in feature_list.tags():
                feature = java_mesh.feature(str(feature_tag))
                feature_info = {"tag": str(feature.tag())}
                try:
                    feature_info["type"] = str(feature.getType())
                except Exception:
                    pass
                label = _safe_label(feature)
                if label:
                    feature_info["label"] = label
                features.append(feature_info)

            info = {
                "tag": target_tag,
                "component": str(comp.tag()),
                "features": features,
                "feature_count": len(features),
            }
            label = _safe_label(java_mesh)
            if label:
                info["label"] = label

            try:
                info["geometry"] = str(java_mesh.geom())
            except Exception:
                pass

            return {"success": True, "mesh": info}
        except Exception as e:
            return {"success": False, "error": f"Failed to get mesh info: {str(e)}"}
