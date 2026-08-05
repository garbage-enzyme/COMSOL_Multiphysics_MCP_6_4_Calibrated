"""Geometry tools for COMSOL MCP Server."""

import math
from typing import Optional, Sequence

from mcp.server.mcpserver import MCPServer

from comsol_mcp.path_policy import PathPolicy

from .property_transport import JSONValue, validate_properties
from .session import session_manager


def _finite_vector(
    value: Sequence[float], name: str, length: int, *, positive: bool = False
) -> list[float]:
    if isinstance(value, (str, bytes)) or len(value) != length:
        raise ValueError(f"{name} must contain exactly {length} values")
    normalized = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError(f"{name} must contain only finite numbers")
        try:
            number = float(item)
        except OverflowError as exc:
            raise ValueError(f"{name} must contain only finite numbers") from exc
        if not math.isfinite(number) or (positive and number <= 0.0):
            qualifier = "positive finite numbers" if positive else "finite numbers"
            raise ValueError(f"{name} must contain only {qualifier}")
        normalized.append(number)
    return normalized


def _finite_positive(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive finite number")
    try:
        normalized = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be a positive finite number") from exc
    if not math.isfinite(normalized) or normalized <= 0.0:
        raise ValueError(f"{name} must be a positive finite number")
    return normalized


def _next_feature_tag(feature_list, prefix: str, requested: str | None) -> str:
    existing = {str(item) for item in list(feature_list.tags())}
    if requested:
        if requested in existing:
            raise ValueError(f"Feature tag already exists: {requested}")
        return requested
    index = 1
    while f"{prefix}{index}" in existing:
        index += 1
    return f"{prefix}{index}"


def _get_geometry_node(model, geometry_name: Optional[str], component_name: str = "comp1"):
    """Helper to get geometry node via Java API.

    Returns:
        tuple: (geom_node, error_message) - geom_node is None if error
    """
    jm = model.java

    try:
        comp = jm.component(component_name)
        if comp is None:
            return None, f"Component '{component_name}' not found."

        if geometry_name:
            geom = comp.geom(geometry_name)
            if geom is None:
                return (
                    None,
                    f"Geometry '{geometry_name}' not found in component '{component_name}'.",
                )
        else:
            # clientapi: ComponentGeomListClient supports neither list() nor
            # subscripting; iterate via tags() and get(tag).
            geoms = comp.geom()
            if geoms.size() == 0:
                return None, "No geometry sequences found. Create one first with geometry_create."
            tags = list(geoms.tags())
            geom = geoms.get(tags[0])

        return geom, None
    except Exception as e:
        return None, f"Failed to get geometry: {str(e)}"


def add_geometry_feature(
    model,
    feature_type: str,
    *,
    geometry_name: Optional[str] = None,
    component_name: str = "comp1",
    feature_name: Optional[str] = None,
    properties: Optional[dict] = None,
) -> dict:
    """Create a generic geometry feature through the 6.4 clientapi."""
    if not feature_type.strip():
        return {"success": False, "error": "feature_type must not be empty."}

    try:
        normalized_properties = validate_properties(properties)
    except (TypeError, ValueError) as exc:
        return {"success": False, "error": f"Invalid properties: {exc}"}

    geom, error = _get_geometry_node(model, geometry_name, component_name)
    if error:
        return {"success": False, "error": error}

    feature_list = geom.feature()
    try:
        tag = _next_feature_tag(feature_list, "feat", feature_name)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    feature = feature_list.create(tag, feature_type)
    property_errors = {}
    for name, value in normalized_properties.items():
        try:
            feature.set(name, value)
        except Exception as exc:
            property_errors[name] = str(exc)

    if property_errors:
        try:
            feature_list.remove(tag)
        except Exception:
            return {
                "success": False,
                "error": "Feature properties failed and rollback was incomplete.",
                "property_errors": property_errors,
                "rolled_back": False,
            }
        return {
            "success": False,
            "error": "Feature properties could not be applied.",
            "property_errors": property_errors,
            "rolled_back": True,
        }

    return {
        "success": True,
        "feature": {
            "name": tag,
            "type": feature_type,
            "geometry": geometry_name or str(geom.tag()),
            "component": component_name,
        },
    }


def list_geometry_features(
    model,
    *,
    geometry_name: Optional[str] = None,
    component_name: str = "comp1",
) -> dict:
    """List geometry feature tags and labels through clientapi."""
    geom, error = _get_geometry_node(model, geometry_name, component_name)
    if error:
        return {"success": False, "error": error}

    features = []
    feature_list = geom.feature()
    for raw_tag in list(feature_list.tags()):
        tag = str(raw_tag)
        feature = feature_list.get(tag)
        info = {"tag": tag}
        try:
            info["label"] = str(feature.label())
        except Exception:
            info["label"] = tag
        features.append(info)
    return {
        "success": True,
        "geometry": geometry_name or str(geom.tag()),
        "component": component_name,
        "features": features,
        "count": len(features),
    }


def add_circle_feature(
    model,
    position: Sequence[float],
    radius: float,
    *,
    geometry_name: Optional[str] = None,
    component_name: str = "comp1",
    feature_name: Optional[str] = None,
) -> dict:
    """Add a validated 2D Circle feature through clientapi."""
    try:
        normalized_position = _finite_vector(position, "position", 2)
        normalized_radius = _finite_positive(radius, "radius")
    except (TypeError, ValueError) as exc:
        return {"success": False, "error": str(exc)}

    result = add_geometry_feature(
        model,
        "Circle",
        geometry_name=geometry_name,
        component_name=component_name,
        feature_name=feature_name,
        properties={
            "pos": [str(value) for value in normalized_position],
            "r": str(normalized_radius),
        },
    )
    if result["success"]:
        result["feature"]["position"] = normalized_position
        result["feature"]["radius"] = normalized_radius
    return result


def add_primitive_feature(
    model,
    feature_type: str,
    position: Sequence[float],
    dimensions: Sequence[float],
    *,
    geometry_name: Optional[str] = None,
    component_name: str = "comp1",
    feature_name: Optional[str] = None,
) -> dict:
    """Validate one primitive completely before delegating its atomic creation."""
    dimension = 2 if feature_type == "Rectangle" else 3
    try:
        normalized_position = _finite_vector(position, "position", dimension)
        if feature_type == "Block":
            normalized_dimensions = _finite_vector(dimensions, "size", 3, positive=True)
            properties = {"pos": normalized_position, "size": normalized_dimensions}
        elif feature_type == "Rectangle":
            normalized_dimensions = _finite_vector(dimensions, "size", 2, positive=True)
            properties = {"pos": normalized_position, "size": normalized_dimensions}
        elif feature_type == "Cylinder":
            normalized_dimensions = _finite_vector(
                dimensions, "size", 2, positive=True
            )
            properties = {
                "pos": normalized_position,
                "r": normalized_dimensions[0],
                "h": normalized_dimensions[1],
            }
        elif feature_type == "Sphere":
            normalized_dimensions = _finite_vector(
                dimensions, "size", 1, positive=True
            )
            properties = {"pos": normalized_position, "r": normalized_dimensions[0]}
        else:
            raise ValueError("unsupported primitive feature type")
    except (IndexError, TypeError, ValueError) as exc:
        return {"success": False, "error": str(exc)}
    result = add_geometry_feature(
        model,
        feature_type,
        geometry_name=geometry_name,
        component_name=component_name,
        feature_name=feature_name,
        properties={
            key: [str(item) for item in value] if isinstance(value, list) else str(value)
            for key, value in properties.items()
        },
    )
    if result["success"]:
        result["feature"]["position"] = normalized_position
        if feature_type in {"Block", "Rectangle"}:
            result["feature"]["size"] = normalized_dimensions
        elif feature_type == "Cylinder":
            result["feature"].update(
                radius=normalized_dimensions[0], height=normalized_dimensions[1]
            )
        else:
            result["feature"]["radius"] = normalized_dimensions[0]
    return result


def add_difference_feature(
    model,
    input_object: str,
    objects_to_subtract: Sequence[str],
    *,
    geometry_name: Optional[str] = None,
    component_name: str = "comp1",
    feature_name: Optional[str] = None,
) -> dict:
    """Validate referenced objects and roll back incomplete Difference creation."""
    subtract = (
        list(objects_to_subtract) if not isinstance(objects_to_subtract, (str, bytes)) else []
    )
    if not isinstance(input_object, str) or not input_object or not subtract:
        return {"success": False, "error": "difference inputs must be nonempty tags"}
    if not all(isinstance(item, str) and item for item in subtract):
        return {"success": False, "error": "difference inputs must be nonempty tags"}
    if input_object in subtract or len(subtract) != len(set(subtract)):
        return {"success": False, "error": "difference inputs must be distinct"}
    geom, error = _get_geometry_node(model, geometry_name, component_name)
    if error:
        return {"success": False, "error": error}
    feature_list = geom.feature()
    existing = {str(item) for item in list(feature_list.tags())}
    missing = sorted({input_object, *subtract} - existing)
    if missing:
        return {"success": False, "error": f"difference inputs are missing: {missing}"}
    try:
        tag = _next_feature_tag(feature_list, "dif", feature_name)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    feature = feature_list.create(tag, "Difference")
    try:
        feature.selection("input").set([input_object])
        feature.selection("input2").set(subtract)
    except Exception:
        try:
            feature_list.remove(tag)
        except Exception:
            return {
                "success": False,
                "error": "Difference setup failed and rollback was incomplete.",
                "rolled_back": False,
            }
        return {"success": False, "error": "Difference setup failed.", "rolled_back": True}
    return {
        "success": True,
        "feature": {
            "name": tag,
            "type": "Difference",
            "input_object": input_object,
            "subtracted": subtract,
        },
    }


def add_union_feature(
    model,
    input_objects: Sequence[str],
    *,
    geometry_name: Optional[str] = None,
    component_name: str = "comp1",
    feature_name: Optional[str] = None,
) -> dict:
    """Add a Boolean Union and its input-object selection through clientapi."""
    try:
        objects = list(input_objects) if not isinstance(input_objects, (str, bytes)) else []
    except TypeError:
        objects = []
    if (
        not objects
        or any(not isinstance(item, str) or not item for item in objects)
        or len(objects) != len(set(objects))
    ):
        return {
            "success": False,
            "error": "input_objects must contain distinct non-empty string tags.",
        }

    geom, error = _get_geometry_node(model, geometry_name, component_name)
    if error:
        return {"success": False, "error": error}
    feature_list = geom.feature()
    existing = {str(item) for item in list(feature_list.tags())}
    missing = sorted(set(objects) - existing)
    if missing:
        return {"success": False, "error": f"union inputs are missing: {missing}"}
    try:
        tag = _next_feature_tag(feature_list, "uni", feature_name)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    union = feature_list.create(tag, "Union")
    try:
        union.selection("input").set(objects)
    except Exception:
        try:
            feature_list.remove(tag)
        except Exception:
            return {
                "success": False,
                "error": "Union setup failed and rollback was incomplete.",
                "rolled_back": False,
            }
        return {"success": False, "error": "Union setup failed.", "rolled_back": True}
    return {
        "success": True,
        "feature": {
            "name": tag,
            "type": "Union",
            "geometry": geometry_name or str(geom.tag()),
            "component": component_name,
            "input_objects": objects,
        },
    }


def add_import_feature(
    model,
    file_path: str,
    *,
    geometry_name: Optional[str] = None,
    component_name: str = "comp1",
    feature_name: Optional[str] = None,
    import_type: str = "CAD",
) -> dict:
    """Create a geometry Import feature with an absolute source path."""
    normalized_import_type = import_type.strip().casefold()
    if normalized_import_type not in {"cad", "mesh"}:
        return {"success": False, "error": "import_type must be CAD or mesh."}
    try:
        path = PathPolicy.from_environment().validate_model_read(file_path).normalized_path
    except ValueError as exc:
        return {"success": False, "error": f"Import file is not allowed: {exc}"}

    geom, error = _get_geometry_node(model, geometry_name, component_name)
    if error:
        return {"success": False, "error": error}
    feature_list = geom.feature()
    try:
        tag = _next_feature_tag(feature_list, "imp", feature_name)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    feature = feature_list.create(tag, "Import")
    try:
        feature.set("type", normalized_import_type)
        feature.set("filename", str(path))
    except Exception:
        try:
            feature_list.remove(tag)
        except Exception:
            return {
                "success": False,
                "error": "Import setup failed and rollback was incomplete.",
                "rolled_back": False,
            }
        return {
            "success": False,
            "error": "Import filename could not be applied.",
            "rolled_back": True,
        }
    return {
        "success": True,
        "feature": {
            "name": tag,
            "type": "Import",
            "geometry": geometry_name or str(geom.tag()),
            "component": component_name,
            "file": str(path),
            "import_type": normalized_import_type,
        },
    }


def build_geometry_sequences(
    model,
    *,
    geometry_name: Optional[str],
    component_name: str,
) -> dict:
    """Build one named geometry or every geometry when no name is supplied."""
    if geometry_name:
        geom, error = _get_geometry_node(model, geometry_name, component_name)
        if error:
            return {"success": False, "error": error}
        geometries = [(geometry_name, geom)]
    else:
        component = model.java.component(component_name)
        if component is None:
            return {"success": False, "error": f"Component '{component_name}' not found."}
        geometry_list = component.geom()
        tags = [str(tag) for tag in geometry_list.tags()]
        if not tags:
            return {"success": False, "error": "No geometry sequences found."}
        geometries = [(tag, geometry_list.get(tag)) for tag in tags]
    built = []
    for tag, geometry in geometries:
        try:
            geometry.run()
        except Exception as exc:
            return {
                "success": False,
                "error": f"Failed to build geometry {tag}: {str(exc)}",
                "built": built,
                "failed_geometry": tag,
            }
        built.append(tag)
    return {
        "success": True,
        "geometries": built,
        "count": len(built),
        "message": "Geometry build completed.",
    }


def register_geometry_tools(mcp: MCPServer) -> None:
    """Register geometry tools with the MCP server."""

    @mcp.tool()
    def geometry_list(model_name: Optional[str] = None) -> dict:
        """
        List all geometry sequences in a model.

        Args:
            model_name: Model name (default: current model)

        Returns:
            List of geometry sequence names
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }

        try:
            geometries = model.geometries()
            return {
                "success": True,
                "geometries": geometries,
                "count": len(geometries),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to list geometries: {str(e)}"}

    @mcp.tool()
    def geometry_create(
        geometry_name: Optional[str] = None,
        space_dimension: int = 3,
        component_name: str = "comp1",
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Create a new geometry sequence in the model's component.

        IMPORTANT: A component must exist first. Use model_create_component if needed.

        Args:
            geometry_name: Name for the geometry sequence (default: 'geom1')
            space_dimension: Space dimension - 2 for 2D, 3 for 3D (default: 3)
            component_name: Component name (default: 'comp1')
            model_name: Model name (default: current model)

        Returns:
            Created geometry info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }

        try:
            jm = model.java

            geom_name = geometry_name or "geom1"

            comp = jm.component(component_name)
            if comp is None:
                return {
                    "success": False,
                    "error": (
                        f"Component '{component_name}' not found. Create it first "
                        "with model_create_component."
                    ),
                }

            comp.geom().create(geom_name, space_dimension)

            return {
                "success": True,
                "geometry": geom_name,
                "component": component_name,
                "space_dimension": space_dimension,
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to create geometry: {str(e)}"}

    @mcp.tool()
    def geometry_add_feature(
        feature_type: str,
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        feature_name: Optional[str] = None,
        model_name: Optional[str] = None,
        properties: Optional[dict[str, JSONValue]] = None,
    ) -> dict:
        """
        Add a geometry feature to a geometry sequence.

        Common feature types:
        - Block: Rectangular block (3D)
        - Cylinder: Cylinder (3D)
        - Sphere: Sphere (3D)
        - Cone: Cone (3D)
        - WorkPlane: Working plane for 2D geometry
        - Rectangle: Rectangle (2D)
        - Circle: Circle (2D)
        - Polygon: Polygon from points
        - Import: Import CAD geometry
        - Union, Intersection, Difference: Boolean operations

        Args:
            feature_type: Type of geometry feature (Block, Cylinder, etc.)
            geometry_name: Geometry sequence name (default: first geometry)
            component_name: Component containing the geometry (default: comp1)
            feature_name: Name for the feature (auto-generated if None)
            model_name: Model name (default: current model)
            properties: Feature-specific bounded JSON properties (position, size, etc.)

        Returns:
            Created feature info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }

        try:
            return add_geometry_feature(
                model,
                feature_type,
                geometry_name=geometry_name,
                component_name=component_name,
                feature_name=feature_name,
                properties=properties,
            )
        except Exception as e:
            return {"success": False, "error": f"Failed to add geometry feature: {str(e)}"}

    @mcp.tool()
    def geometry_add_block(
        position: Sequence[float] = (0, 0, 0),
        size: Sequence[float] = (1, 1, 1),
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        feature_name: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Add a block (rectangular cuboid) to the geometry.

        Args:
            position: Base position [x, y, z] in meters (default: origin)
            size: Dimensions [width, depth, height] in meters (default: 1m cube)
            geometry_name: Geometry sequence name (default: first geometry)
            component_name: Component name (default: 'comp1')
            feature_name: Feature name (auto-generated if None)
            model_name: Model name (default: current model)

        Returns:
            Created block info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }

        try:
            return add_primitive_feature(
                model,
                "Block",
                position,
                size,
                geometry_name=geometry_name,
                component_name=component_name,
                feature_name=feature_name,
            )
        except Exception as e:
            return {"success": False, "error": f"Failed to add block: {str(e)}"}

    @mcp.tool()
    def geometry_add_cylinder(
        position: Sequence[float] = (0, 0, 0),
        radius: float = 0.5,
        height: float = 1.0,
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        feature_name: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Add a cylinder to the geometry.

        Args:
            position: Center of base [x, y, z] in meters
            radius: Radius in meters (default: 0.5)
            height: Height in meters (default: 1.0)
            geometry_name: Geometry sequence name (default: first geometry)
            component_name: Component name (default: 'comp1')
            feature_name: Feature name (auto-generated if None)
            model_name: Model name (default: current model)

        Returns:
            Created cylinder info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }

        try:
            return add_primitive_feature(
                model,
                "Cylinder",
                position,
                (radius, height),
                geometry_name=geometry_name,
                component_name=component_name,
                feature_name=feature_name,
            )
        except Exception as e:
            return {"success": False, "error": f"Failed to add cylinder: {str(e)}"}

    @mcp.tool()
    def geometry_add_sphere(
        position: Sequence[float] = (0, 0, 0),
        radius: float = 0.5,
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        feature_name: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Add a sphere to the geometry.

        Args:
            position: Center [x, y, z] in meters
            radius: Radius in meters (default: 0.5)
            geometry_name: Geometry sequence name (default: first geometry)
            component_name: Component name (default: 'comp1')
            feature_name: Feature name (auto-generated if None)
            model_name: Model name (default: current model)

        Returns:
            Created sphere info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }

        try:
            return add_primitive_feature(
                model,
                "Sphere",
                position,
                (radius,),
                geometry_name=geometry_name,
                component_name=component_name,
                feature_name=feature_name,
            )
        except Exception as e:
            return {"success": False, "error": f"Failed to add sphere: {str(e)}"}

    @mcp.tool()
    def geometry_add_rectangle(
        position: Sequence[float] = (0, 0),
        size: Sequence[float] = (1, 1),
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        feature_name: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Add a rectangle to a 2D geometry or work plane.

        Args:
            position: Base position [x, y] in meters
            size: Dimensions [width, height] in meters
            geometry_name: Geometry sequence name (default: first geometry)
            component_name: Component name (default: 'comp1')
            feature_name: Feature name (auto-generated if None)
            model_name: Model name (default: current model)

        Returns:
            Created rectangle info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }

        try:
            return add_primitive_feature(
                model,
                "Rectangle",
                position,
                size,
                geometry_name=geometry_name,
                component_name=component_name,
                feature_name=feature_name,
            )
        except Exception as e:
            return {"success": False, "error": f"Failed to add rectangle: {str(e)}"}

    @mcp.tool()
    def geometry_add_circle(
        position: Sequence[float] = (0, 0),
        radius: float = 0.5,
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        feature_name: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Add a circle to a 2D geometry or work plane.

        Args:
            position: Center [x, y] in meters
            radius: Radius in meters (default: 0.5)
            geometry_name: Geometry sequence name
            component_name: Component containing the geometry (default: comp1)
            feature_name: Optional feature tag
            model_name: Model name (default: current model)

        Returns:
            Created circle info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }

        try:
            return add_circle_feature(
                model,
                position,
                radius,
                geometry_name=geometry_name,
                component_name=component_name,
                feature_name=feature_name,
            )
        except Exception as e:
            return {"success": False, "error": f"Failed to add circle: {str(e)}"}

    @mcp.tool()
    def geometry_boolean_union(
        input_objects: Sequence[str],
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        feature_name: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Create a boolean union of geometry objects.

        Args:
            input_objects: Names of objects to unite
            geometry_name: Geometry sequence name
            component_name: Component containing the geometry (default: comp1)
            feature_name: Optional feature tag
            model_name: Model name (default: current model)

        Returns:
            Created union operation info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }

        try:
            return add_union_feature(
                model,
                input_objects,
                geometry_name=geometry_name,
                component_name=component_name,
                feature_name=feature_name,
            )
        except Exception as e:
            return {"success": False, "error": f"Failed to create union: {str(e)}"}

    @mcp.tool()
    def geometry_boolean_difference(
        input_object: str,
        objects_to_subtract: Sequence[str],
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        feature_name: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Create a boolean difference (subtract objects from another).

        Args:
            input_object: Object to subtract from (e.g., 'blk1')
            objects_to_subtract: Objects to remove (e.g., ['cyl1'])
            geometry_name: Geometry sequence name (default: first geometry)
            component_name: Component name (default: 'comp1')
            feature_name: Feature name (auto-generated if None)
            model_name: Model name (default: current model)

        Returns:
            Created difference operation info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }

        try:
            return add_difference_feature(
                model,
                input_object,
                objects_to_subtract,
                geometry_name=geometry_name,
                component_name=component_name,
                feature_name=feature_name,
            )
        except Exception as e:
            return {"success": False, "error": f"Failed to create difference: {str(e)}"}

    @mcp.tool()
    def geometry_import(
        file_path: str,
        geometry_name: Optional[str] = None,
        import_type: str = "CAD",
        component_name: str = "comp1",
        feature_name: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Import geometry from a CAD file.

        Supported formats: STEP, IGES, STL, NASTRAN, etc.

        Args:
            file_path: Path to the CAD file
            geometry_name: Geometry sequence name
            import_type: Import type (CAD, mesh, etc.)
            component_name: Component containing the geometry (default: comp1)
            feature_name: Optional feature tag
            model_name: Model name (default: current model)

        Returns:
            Import operation info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }

        try:
            result = add_import_feature(
                model,
                file_path,
                geometry_name=geometry_name,
                component_name=component_name,
                feature_name=feature_name,
                import_type=import_type,
            )
            return result
        except Exception as e:
            return {"success": False, "error": f"Failed to import geometry: {str(e)}"}

    @mcp.tool()
    def geometry_build(
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Build the geometry sequence to generate the actual geometry.

        This must be called after adding/modifying geometry features.

        Args:
            geometry_name: Geometry sequence name (default: build all)
            component_name: Component name (default: 'comp1')
            model_name: Model name (default: current model)

        Returns:
            Build status
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }

        try:
            return build_geometry_sequences(
                model,
                geometry_name=geometry_name,
                component_name=component_name,
            )
        except Exception as e:
            return {"success": False, "error": f"Failed to build geometry: {str(e)}"}

    @mcp.tool()
    def geometry_list_features(
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        model_name: Optional[str] = None,
    ) -> dict:
        """
        List all features in a geometry sequence.

        Args:
            geometry_name: Geometry sequence name (default: first geometry)
            component_name: Component containing the geometry (default: comp1)
            model_name: Model name (default: current model)

        Returns:
            List of geometry features with their types
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }

        try:
            return list_geometry_features(
                model,
                geometry_name=geometry_name,
                component_name=component_name,
            )
        except Exception as e:
            return {"success": False, "error": f"Failed to list features: {str(e)}"}
