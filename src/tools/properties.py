"""Constrained get/set access to properties on existing clientapi features."""

from __future__ import annotations

import re
from typing import Literal

from mcp.server.fastmcp import FastMCP

from .property_transport import JSONValue, normalize_property_value, validate_property_name
from .session import session_manager


ClientAPIContainer = Literal[
    "geometry_feature",
    "physics_feature",
    "mesh_feature",
    "study_step",
    "result_feature",
]

_CONTAINERS = frozenset({
    "geometry_feature", "physics_feature", "mesh_feature", "study_step",
    "result_feature",
})
_TAG = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _validate_target(
    component_name: str,
    container: str,
    feature_tag: str,
    property_name: str,
) -> tuple[str, str, str]:
    if not isinstance(component_name, str) or not _TAG.fullmatch(component_name):
        raise ValueError("component_name must be one exact clientapi tag")
    if container not in _CONTAINERS:
        raise ValueError(f"unsupported clientapi container: {container!r}")
    if not isinstance(feature_tag, str):
        raise TypeError("feature_tag must be a parent/child tag path")
    parts = feature_tag.split("/")
    if len(parts) != 2 or not all(_TAG.fullmatch(part) for part in parts):
        raise ValueError("feature_tag must be an exact parent/child tag path")
    validate_property_name(property_name)
    return parts[0], parts[1], property_name


def _resolve_existing_target(
    model,
    component_name: str,
    container: str,
    feature_tag: str,
    property_name: str,
):
    parent_tag, child_tag, property_name = _validate_target(
        component_name, container, feature_tag, property_name
    )
    jm = model.java
    component = jm.component(component_name)
    if component is None:
        raise ValueError(f"component does not exist: {component_name}")

    if container == "geometry_feature":
        target = component.geom(parent_tag).feature().get(child_tag)
    elif container == "physics_feature":
        target = component.physics(parent_tag).feature().get(child_tag)
    elif container == "mesh_feature":
        target = component.mesh(parent_tag).feature().get(child_tag)
    elif container == "study_step":
        target = jm.study(parent_tag).feature().get(child_tag)
    else:
        target = jm.result(parent_tag).feature().get(child_tag)
    if target is None:
        raise ValueError(f"clientapi target does not exist: {container}:{feature_tag}")

    try:
        property_names = {str(name) for name in target.properties()}
    except Exception as exc:
        raise ValueError(f"cannot inventory target properties: {exc}") from exc
    if property_name not in property_names:
        raise ValueError(
            f"unknown property {property_name!r} on {container}:{feature_tag}"
        )
    return target


def _read_property(target, property_name: str) -> tuple[JSONValue, str]:
    try:
        value_type = str(target.getValueType(property_name))
    except Exception:
        value_type = "String"
    normalized_type = value_type.lower().replace("[]", "array")

    if "matrix" in normalized_type:
        getter = target.getStringMatrix
        value = [[str(item) for item in row] for row in getter(property_name)]
    elif "array" in normalized_type:
        if "double" in normalized_type or "float" in normalized_type:
            value = [float(item) for item in target.getDoubleArray(property_name)]
        elif "int" in normalized_type:
            value = [int(item) for item in target.getIntArray(property_name)]
        elif "bool" in normalized_type:
            value = [bool(item) for item in target.getBooleanArray(property_name)]
        else:
            value = [str(item) for item in target.getStringArray(property_name)]
    elif "double" in normalized_type or "float" in normalized_type:
        value = float(target.getDouble(property_name))
    elif "int" in normalized_type:
        value = int(target.getInt(property_name))
    elif "bool" in normalized_type:
        value = bool(target.getBoolean(property_name))
    else:
        value = str(target.getString(property_name))
    return normalize_property_value(value), value_type


def get_existing_property(
    model,
    component_name: str,
    container: str,
    feature_tag: str,
    property_name: str,
) -> dict:
    """Read one property without reflection or arbitrary method invocation."""
    try:
        target = _resolve_existing_target(
            model, component_name, container, feature_tag, property_name
        )
        value, value_type = _read_property(target, property_name)
        return {
            "success": True,
            "target": f"{component_name}/{container}/{feature_tag}/{property_name}",
            "container": container,
            "feature_tag": feature_tag,
            "property": property_name,
            "value": value,
            "value_type": value_type,
            "maturity": "experimental",
        }
    except (TypeError, ValueError) as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        return {"success": False, "error": f"clientapi property read failed: {exc}"}


def set_existing_property(
    model,
    component_name: str,
    container: str,
    feature_tag: str,
    property_name: str,
    value: JSONValue,
) -> dict:
    """Set one existing property using only the fixed clientapi ``set`` method."""
    try:
        normalized_value = normalize_property_value(value)
        target = _resolve_existing_target(
            model, component_name, container, feature_tag, property_name
        )
        old_value, value_type = _read_property(target, property_name)
        target.set(property_name, normalized_value)
        try:
            new_value, new_value_type = _read_property(target, property_name)
        except Exception:
            new_value, new_value_type = normalized_value, value_type
        return {
            "success": True,
            "target": f"{component_name}/{container}/{feature_tag}/{property_name}",
            "container": container,
            "feature_tag": feature_tag,
            "property": property_name,
            "old_value": old_value,
            "new_value": new_value,
            "value_type": new_value_type,
            "maturity": "experimental",
            "warning": "This constrained setter mutates only the in-memory model.",
        }
    except (TypeError, ValueError) as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        return {"success": False, "error": f"clientapi property set failed: {exc}"}


def register_property_tools(mcp: FastMCP) -> None:
    """Register constrained existing-object property tools."""

    @mcp.tool()
    def clientapi_property_get(
        model_name: str,
        component_name: str,
        container: ClientAPIContainer,
        feature_tag: str,
        property_name: str,
    ) -> dict:
        """Read one existing feature property from an exact parent/child tag path."""
        if not model_name.strip():
            return {"success": False, "error": "model_name must be exact and non-empty"}
        model = session_manager.get_model(model_name)
        if model is None:
            return {"success": False, "error": f"Model not found: {model_name}"}
        return get_existing_property(
            model, component_name, container, feature_tag, property_name
        )

    @mcp.tool()
    def clientapi_property_set(
        model_name: str,
        component_name: str,
        container: ClientAPIContainer,
        feature_tag: str,
        property_name: str,
        value: JSONValue,
    ) -> dict:
        """Set one existing property; cannot create, remove, run, save, or invoke methods."""
        if not model_name.strip():
            return {"success": False, "error": "model_name must be exact and non-empty"}
        model = session_manager.get_model(model_name)
        if model is None:
            return {"success": False, "error": f"Model not found: {model_name}"}
        return set_existing_property(
            model, component_name, container, feature_tag, property_name, value
        )


__all__ = [
    "ClientAPIContainer", "get_existing_property", "register_property_tools",
    "set_existing_property",
]
