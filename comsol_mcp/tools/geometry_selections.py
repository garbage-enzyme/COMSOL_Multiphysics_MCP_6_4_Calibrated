"""Bounded named geometry selections for stable physics assignment."""

from __future__ import annotations

import re
from typing import Any, Optional

from mcp.server.mcpserver import MCPServer

from comsol_mcp.utils.validation import strict_json_integer

from .physics import _first_component, _resolve_geometry_tag
from .session import session_manager

_TAG = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_MAX_EXPRESSION_BYTES = 4096
_BOX_CONDITIONS = frozenset({"inside", "intersects"})


def _bounded_tag(value: str, name: str) -> str:
    if not isinstance(value, str) or not _TAG.fullmatch(value):
        raise ValueError(f"{name} must be one exact clientapi tag")
    return value


def _bounded_expression(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be a nonempty expression")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8") from exc
    if size > _MAX_EXPRESSION_BYTES:
        raise ValueError(f"{name} exceeds {_MAX_EXPRESSION_BYTES} bytes")
    return value


def _component_and_geometry(
    model: Any, component_name: Optional[str], geometry_name: Optional[str]
) -> tuple[Any, Any, str]:
    jm = model.java
    component = jm.component(component_name) if component_name else _first_component(jm)
    if component is None:
        raise ValueError(f"Component not found: {component_name}")
    geometry_tag = _resolve_geometry_tag(component.geom(), geometry_name)
    geometry = component.geom().get(geometry_tag)
    return component, geometry, geometry_tag


def _selection_tags(component: Any) -> set[str]:
    return {str(tag) for tag in list(component.selection().tags())}


def _remove_selections(selection_list: Any, tags: list[str]) -> bool:
    complete = True
    for tag in reversed(tags):
        try:
            selection_list.remove(tag)
        except Exception:
            complete = False
    return complete


def _normalized_box_request(
    *,
    selection_name: str,
    x_min: str,
    x_max: str,
    y_min: str,
    y_max: str,
    z_min: Optional[str],
    z_max: Optional[str],
    entity_dimension: int,
    condition: str,
) -> dict[str, Any]:
    tag = _bounded_tag(selection_name, "selection_name")
    if (z_min is None) != (z_max is None):
        raise ValueError("z_min and z_max must be supplied together")
    normalized_condition = str(condition).strip().casefold()
    if normalized_condition not in _BOX_CONDITIONS:
        raise ValueError("condition must be 'inside' or 'intersects'")
    dimension = strict_json_integer(entity_dimension, "entity_dimension", minimum=0, maximum=3)
    bounds = {
        "xmin": _bounded_expression(x_min, "x_min"),
        "xmax": _bounded_expression(x_max, "x_max"),
        "ymin": _bounded_expression(y_min, "y_min"),
        "ymax": _bounded_expression(y_max, "y_max"),
    }
    if z_min is not None and z_max is not None:
        bounds["zmin"] = _bounded_expression(z_min, "z_min")
        bounds["zmax"] = _bounded_expression(z_max, "z_max")
    return {
        "tag": tag,
        "bounds": bounds,
        "entity_dimension": dimension,
        "condition": normalized_condition,
    }


def create_box_selection(
    model: Any,
    *,
    selection_name: str,
    x_min: str,
    x_max: str,
    y_min: str,
    y_max: str,
    z_min: Optional[str] = None,
    z_max: Optional[str] = None,
    entity_dimension: int = 1,
    condition: str = "intersects",
    geometry_name: Optional[str] = None,
    component_name: Optional[str] = None,
) -> dict[str, Any]:
    """Create one named Box selection with rollback on setup failure."""
    try:
        request = _normalized_box_request(
            selection_name=selection_name,
            x_min=x_min,
            x_max=x_max,
            y_min=y_min,
            y_max=y_max,
            z_min=z_min,
            z_max=z_max,
            entity_dimension=entity_dimension,
            condition=condition,
        )
        component, geometry, geometry_tag = _component_and_geometry(
            model, component_name, geometry_name
        )
        spatial_dimension = int(geometry.getSDim())
        if request["entity_dimension"] > spatial_dimension:
            raise ValueError("entity_dimension exceeds the geometry spatial dimension")
        if spatial_dimension == 2 and (z_min is not None or z_max is not None):
            raise ValueError("z bounds are not valid for a 2D geometry")
        if request["tag"] in _selection_tags(component):
            raise ValueError(f"Selection tag already exists: {request['tag']}")
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    selections = component.selection()
    created = False
    try:
        selection = selections.create(request["tag"], "Box")
        created = True
        selection.geom(geometry_tag, request["entity_dimension"])
        for name, value in request["bounds"].items():
            selection.set(name, value)
        selection.set("condition", request["condition"])
    except Exception:
        rolled_back = not created or _remove_selections(selections, [request["tag"]])
        return {
            "success": False,
            "error": "Box selection setup failed.",
            "rolled_back": rolled_back,
        }

    result: dict[str, Any] = {
        "success": True,
        "selection": {
            "tag": request["tag"],
            "type": "Box",
            "component": str(component.tag()),
            "geometry": geometry_tag,
            "entity_dimension": request["entity_dimension"],
            "condition": request["condition"],
            "bounds": dict(request["bounds"]),
            "entities": [],
            "entities_evaluated": False,
        },
    }
    try:
        result["selection"]["entities"] = [int(value) for value in selection.entities()]
        result["selection"]["entities_evaluated"] = True
    except Exception:
        result["warning"] = "Selection exists, but entities are unavailable before geometry build."
    return result


def create_side_selections(
    model: Any,
    *,
    x_min: str,
    x_max: str,
    y_min: str,
    y_max: str,
    prefix: str = "side",
    tolerance: str = "1e-9[m]",
    entity_dimension: int = 1,
    geometry_name: Optional[str] = None,
    component_name: Optional[str] = None,
) -> dict[str, Any]:
    """Create four named 2D side selections as one rollback-safe transaction."""
    try:
        selection_prefix = _bounded_tag(prefix, "prefix")
        limits = {
            "x_min": _bounded_expression(x_min, "x_min"),
            "x_max": _bounded_expression(x_max, "x_max"),
            "y_min": _bounded_expression(y_min, "y_min"),
            "y_max": _bounded_expression(y_max, "y_max"),
        }
        tolerance_value = _bounded_expression(tolerance, "tolerance")
        dimension = strict_json_integer(entity_dimension, "entity_dimension", minimum=0, maximum=2)
        component, geometry, _geometry_tag = _component_and_geometry(
            model, component_name, geometry_name
        )
        if int(geometry.getSDim()) != 2:
            raise ValueError("side selections require a 2D geometry")
        tags = [f"{selection_prefix}_{side}" for side in ("left", "right", "bottom", "top")]
        if any(not _TAG.fullmatch(tag) for tag in tags):
            raise ValueError("prefix produces an invalid side selection tag")
        collisions = sorted(set(tags) & _selection_tags(component))
        if collisions:
            raise ValueError(f"Selection tags already exist: {collisions}")
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    expanded_x_min = f"({limits['x_min']})-({tolerance_value})"
    expanded_x_max = f"({limits['x_max']})+({tolerance_value})"
    expanded_y_min = f"({limits['y_min']})-({tolerance_value})"
    expanded_y_max = f"({limits['y_max']})+({tolerance_value})"
    definitions = {
        "left": (
            expanded_x_min,
            f"({limits['x_min']})+({tolerance_value})",
            expanded_y_min,
            expanded_y_max,
        ),
        "right": (
            f"({limits['x_max']})-({tolerance_value})",
            expanded_x_max,
            expanded_y_min,
            expanded_y_max,
        ),
        "bottom": (
            expanded_x_min,
            expanded_x_max,
            expanded_y_min,
            f"({limits['y_min']})+({tolerance_value})",
        ),
        "top": (
            expanded_x_min,
            expanded_x_max,
            f"({limits['y_max']})-({tolerance_value})",
            expanded_y_max,
        ),
    }

    created: list[str] = []
    outcomes: dict[str, Any] = {}
    for side, bounds in definitions.items():
        tag = f"{selection_prefix}_{side}"
        result = create_box_selection(
            model,
            selection_name=tag,
            x_min=bounds[0],
            x_max=bounds[1],
            y_min=bounds[2],
            y_max=bounds[3],
            entity_dimension=dimension,
            condition="inside",
            geometry_name=geometry_name,
            component_name=component_name,
        )
        if not result.get("success"):
            prior_rolled_back = _remove_selections(component.selection(), created)
            failed_side_rolled_back = result.get("rolled_back") is not False
            return {
                "success": False,
                "error": result.get("error") or f"Side selection setup failed at {side}.",
                "failed_side": side,
                "rolled_back": prior_rolled_back and failed_side_rolled_back,
            }
        created.append(tag)
        outcomes[side] = result["selection"]
    return {
        "success": True,
        "selections": outcomes,
        "count": len(outcomes),
        "component": str(component.tag()),
    }


def register_geometry_selection_tools(mcp: MCPServer) -> None:
    """Register bounded named-selection tools."""

    @mcp.tool()  # type: ignore[untyped-decorator]
    def geometry_create_box_selection(
        selection_name: str,
        x_min: str,
        x_max: str,
        y_min: str,
        y_max: str,
        z_min: Optional[str] = None,
        z_max: Optional[str] = None,
        entity_dimension: int = 1,
        condition: str = "intersects",
        geometry_name: Optional[str] = None,
        component_name: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a bounded named Box selection with transactional rollback."""
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }
        return create_box_selection(
            model,
            selection_name=selection_name,
            x_min=x_min,
            x_max=x_max,
            y_min=y_min,
            y_max=y_max,
            z_min=z_min,
            z_max=z_max,
            entity_dimension=entity_dimension,
            condition=condition,
            geometry_name=geometry_name,
            component_name=component_name,
        )

    @mcp.tool()  # type: ignore[untyped-decorator]
    def geometry_create_side_selections(
        x_min: str,
        x_max: str,
        y_min: str,
        y_max: str,
        prefix: str = "side",
        tolerance: str = "1e-9[m]",
        entity_dimension: int = 1,
        geometry_name: Optional[str] = None,
        component_name: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create left, right, bottom, and top selections atomically."""
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }
        return create_side_selections(
            model,
            x_min=x_min,
            x_max=x_max,
            y_min=y_min,
            y_max=y_max,
            prefix=prefix,
            tolerance=tolerance,
            entity_dimension=entity_dimension,
            geometry_name=geometry_name,
            component_name=component_name,
        )


__all__ = [
    "create_box_selection",
    "create_side_selections",
    "register_geometry_selection_tools",
]
