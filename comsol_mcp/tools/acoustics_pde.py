"""Bounded Acoustics and mathematical PDE tools."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Optional

from mcp.server.mcpserver import MCPServer

from comsol_mcp.utils.validation import strict_json_integer

from .physics import _component_sdim, _find_physics_context, _first_component, _resolve_geometry_tag
from .property_transport import JSONValue, validate_properties
from .session import session_manager

_TAG = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_MAX_SELECTION_ITEMS = 256
_MAX_BOUNDARY_CONDITIONS = 32
_MAX_DEPENDENT_VARIABLES = 16
_MAX_LABEL_BYTES = 512

ACOUSTIC_BOUNDARY_CONDITIONS = {
    "SoundHard": (),
    "SoundSoft": (),
    "Pressure": ("p0",),
    "Impedance": ("Zn",),
    "NormalAcceleration": ("nacc",),
    "NormalVelocity": ("nvel",),
    "PlaneWaveRadiation": (),
}

PDE_BOUNDARY_CONDITIONS = {
    "DirichletBoundary": ("r",),
    "FluxBoundary": ("g", "q"),
    "ZeroFluxBoundary": (),
    "PeriodicCondition": (),
}

PDE_BOUNDARY_ALIASES = {
    "dirichlet": "DirichletBoundary",
    "flux": "FluxBoundary",
    "neumann": "FluxBoundary",
    "zero_flux": "ZeroFluxBoundary",
    "no_flux": "ZeroFluxBoundary",
    "periodic": "PeriodicCondition",
}

_PDE_INTERFACES = {
    "coefficient": {
        "type": "CoefficientFormPDE",
        "default_tag": "c",
        "equation_tag": "cfeq1",
        "equation_properties": frozenset({"c", "a", "f", "da", "ea", "al", "be", "ga"}),
    },
    "general": {
        "type": "GeneralFormPDE",
        "default_tag": "g",
        "equation_tag": "gfeq1",
        "equation_properties": frozenset({"Ga", "f", "da", "ea"}),
    },
    "weak": {
        "type": "WeakFormPDE",
        "default_tag": "w",
        "equation_tag": "wfeq1",
        "equation_properties": frozenset({"weak"}),
    },
}

_BOUNDARY_KEYS = frozenset({"type", "boundaries", "selection_name", "properties", "tag", "label"})


def _bounded_tag(value: str, name: str) -> str:
    if not isinstance(value, str) or not _TAG.fullmatch(value):
        raise ValueError(f"{name} must be one exact clientapi tag")
    return value


def _bounded_label(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("label must be nonempty text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("label must be valid UTF-8") from exc
    if size > _MAX_LABEL_BYTES:
        raise ValueError(f"label exceeds {_MAX_LABEL_BYTES} bytes")
    return value


def _java_string_array(values: Sequence[str]) -> Any:
    try:
        import jpype
    except ImportError:
        return [str(value) for value in values]
    if not jpype.isJVMStarted():
        return [str(value) for value in values]
    return jpype.JArray(jpype.JString)([str(value) for value in values])


def _component(model: Any, component_name: Optional[str]) -> Any:
    component = (
        model.java.component(component_name) if component_name else _first_component(model.java)
    )
    if component is None:
        raise ValueError(f"Component not found: {component_name}")
    return component


def _selection_tags(component: Any) -> set[str]:
    return {str(tag) for tag in list(component.selection().tags())}


def _normalized_selection(
    values: Optional[Sequence[int]],
    selection_name: Optional[str],
    *,
    required: bool,
    label: str,
) -> tuple[list[int], Optional[str]]:
    if values is not None and isinstance(values, (str, bytes)):
        raise TypeError(f"{label} must be an integer array")
    entities = (
        [strict_json_integer(value, label, minimum=1) for value in values]
        if values is not None
        else []
    )
    if len(entities) > _MAX_SELECTION_ITEMS:
        raise ValueError(f"{label} may contain at most {_MAX_SELECTION_ITEMS} items")
    if len(entities) != len(set(entities)):
        raise ValueError(f"{label} must not contain duplicate entity numbers")
    named = _bounded_tag(selection_name, "selection_name") if selection_name is not None else None
    if entities and named:
        raise ValueError(f"use either {label} or selection_name, not both")
    if required and not entities and named is None:
        raise ValueError(f"one of {label} or selection_name is required")
    return entities, named


def _apply_selection(node: Any, entities: list[int], named: Optional[str]) -> None:
    if named is not None:
        node.selection().named(named)
    elif entities:
        node.selection().set(entities)


def _validate_named_selection(component: Any, selection_name: Optional[str]) -> None:
    if selection_name is not None and selection_name not in _selection_tags(component):
        raise ValueError(f"Named selection does not exist: {selection_name}")


def _remove_physics(physics_list: Any, tag: str, created: bool) -> bool:
    if not created:
        return True
    try:
        physics_list.remove(tag)
        return True
    except Exception:
        return False


def add_pressure_acoustics_interface(
    model: Any,
    *,
    domain_selection: Optional[Sequence[int]] = None,
    selection_name: Optional[str] = None,
    component_name: Optional[str] = None,
    geometry_name: Optional[str] = None,
    physics_tag: str = "acpr",
) -> dict[str, Any]:
    """Create one Pressure Acoustics interface as a rollback-safe transaction."""
    try:
        tag = _bounded_tag(physics_tag, "physics_tag")
        domains, named = _normalized_selection(
            domain_selection, selection_name, required=False, label="domain_selection"
        )
        component = _component(model, component_name)
        _validate_named_selection(component, named)
        geometry_tag = _resolve_geometry_tag(component.geom(), geometry_name)
        physics_list = component.physics()
        if tag in {str(value) for value in list(physics_list.tags())}:
            raise ValueError(f"Physics tag already exists: {tag}")
    except (TypeError, ValueError) as exc:
        return {"success": False, "error": str(exc)}

    created = False
    try:
        physics = physics_list.create(tag, "PressureAcoustics", geometry_tag)
        created = True
        _apply_selection(physics, domains, named)
    except Exception:
        return {
            "success": False,
            "error": "Pressure Acoustics setup failed.",
            "rolled_back": _remove_physics(physics_list, tag, created),
        }
    return {
        "success": True,
        "physics": {
            "tag": tag,
            "type": "PressureAcoustics",
            "component": str(component.tag()),
            "geometry": geometry_tag,
            "domain_selection": domains or None,
            "selection_name": named,
        },
    }


def _dependent_variables(values: Sequence[str]) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise TypeError("dependent_variables must be a string array")
    result = [_bounded_tag(value, "dependent_variables item") for value in values]
    if not 1 <= len(result) <= _MAX_DEPENDENT_VARIABLES:
        raise ValueError(f"dependent_variables must contain 1-{_MAX_DEPENDENT_VARIABLES} items")
    if len(result) != len(set(result)):
        raise ValueError("dependent_variables must be unique")
    return result


def _pde_properties(form: str, properties: object | None) -> dict[str, JSONValue]:
    normalized = validate_properties(properties)
    allowed = _PDE_INTERFACES[form]["equation_properties"]
    unknown = sorted(set(normalized) - set(allowed))
    if unknown:
        raise ValueError(f"unsupported {form} PDE equation properties: {unknown}")
    return dict(normalized)


def add_pde_interface(
    model: Any,
    form: str,
    *,
    dependent_variables: Sequence[str] = ("u",),
    equation_properties: object | None = None,
    domain_selection: Optional[Sequence[int]] = None,
    selection_name: Optional[str] = None,
    component_name: Optional[str] = None,
    physics_tag: Optional[str] = None,
) -> dict[str, Any]:
    """Create one exact mathematical PDE interface and configure its equation."""
    try:
        normalized_form = str(form).strip().casefold()
        if normalized_form not in _PDE_INTERFACES:
            raise ValueError("form must be 'coefficient', 'general', or 'weak'")
        spec = _PDE_INTERFACES[normalized_form]
        tag = _bounded_tag(
            str(spec["default_tag"]) if physics_tag is None else physics_tag,
            "physics_tag",
        )
        variables = _dependent_variables(dependent_variables)
        properties = _pde_properties(normalized_form, equation_properties)
        domains, named = _normalized_selection(
            domain_selection, selection_name, required=False, label="domain_selection"
        )
        component = _component(model, component_name)
        _validate_named_selection(component, named)
        physics_list = component.physics()
        if tag in {str(value) for value in list(physics_list.tags())}:
            raise ValueError(f"Physics tag already exists: {tag}")
    except (TypeError, ValueError) as exc:
        return {"success": False, "error": str(exc)}

    created = False
    try:
        physics = physics_list.create(tag, spec["type"], _java_string_array(variables))
        created = True
        _apply_selection(physics, domains, named)
        equation = physics.feature().get(spec["equation_tag"])
        for name, value in properties.items():
            equation.set(name, value)
    except Exception:
        return {
            "success": False,
            "error": f"{spec['type']} setup failed.",
            "rolled_back": _remove_physics(physics_list, tag, created),
        }
    return {
        "success": True,
        "physics": {
            "tag": tag,
            "type": spec["type"],
            "component": str(component.tag()),
            "dependent_variables": variables,
            "equation_properties": properties,
            "domain_selection": domains or None,
            "selection_name": named,
        },
    }


def _normalize_boundary_condition(
    condition: Mapping[str, Any],
    *,
    family: str,
) -> dict[str, Any]:
    if not isinstance(condition, Mapping):
        raise TypeError("each boundary condition must be an object")
    unknown_keys = sorted(set(condition) - _BOUNDARY_KEYS)
    if unknown_keys:
        raise ValueError(f"unknown boundary condition fields: {unknown_keys}")
    raw_type = condition.get("type")
    if not isinstance(raw_type, str) or not raw_type.strip():
        raise ValueError("boundary condition type is required")
    if family == "pde":
        feature_type = PDE_BOUNDARY_ALIASES.get(raw_type.strip().casefold(), raw_type.strip())
        reference = PDE_BOUNDARY_CONDITIONS
    else:
        feature_type = raw_type.strip()
        reference = ACOUSTIC_BOUNDARY_CONDITIONS
    if feature_type not in reference:
        raise ValueError(f"unsupported {family} boundary condition: {feature_type}")
    entities, named = _normalized_selection(
        condition.get("boundaries"),
        condition.get("selection_name"),
        required=True,
        label="boundaries",
    )
    properties = validate_properties(condition.get("properties"))
    unknown_properties = sorted(set(properties) - set(reference[feature_type]))
    if unknown_properties:
        raise ValueError(f"unsupported properties for {feature_type}: {unknown_properties}")
    missing_properties = sorted(set(reference[feature_type]) - set(properties))
    if missing_properties:
        raise ValueError(f"missing required properties for {feature_type}: {missing_properties}")
    tag = condition.get("tag")
    if tag is not None:
        tag = _bounded_tag(tag, "boundary tag")
    return {
        "type": feature_type,
        "boundaries": entities,
        "selection_name": named,
        "properties": properties,
        "tag": tag,
        "label": _bounded_label(condition.get("label")),
    }


def configure_boundaries(
    model: Any,
    physics_name: str,
    boundary_conditions: Sequence[Mapping[str, Any]],
    *,
    family: str,
) -> dict[str, Any]:
    """Create exact acoustic or PDE boundary features atomically."""
    try:
        if family not in {"acoustic", "pde"}:
            raise ValueError("family must be 'acoustic' or 'pde'")
        if isinstance(boundary_conditions, (str, bytes)):
            raise TypeError("boundary_conditions must be an object array")
        if not 1 <= len(boundary_conditions) <= _MAX_BOUNDARY_CONDITIONS:
            raise ValueError(f"boundary_conditions must contain 1-{_MAX_BOUNDARY_CONDITIONS} items")
        normalized = [
            _normalize_boundary_condition(condition, family=family)
            for condition in boundary_conditions
        ]
        component, physics = _find_physics_context(model.java, physics_name)
        if physics is None or component is None:
            raise ValueError(f"Physics interface not found: {physics_name}")
        for condition in normalized:
            _validate_named_selection(component, condition["selection_name"])
        feature_list = physics.feature()
        existing = {str(tag) for tag in list(feature_list.tags())}
        reserved = set(existing)
        for condition in normalized:
            tag = condition["tag"]
            if tag is None:
                prefix = condition["type"].lower()
                index = 1
                while f"{prefix}_{index}" in reserved:
                    index += 1
                tag = f"{prefix}_{index}"
            if tag in reserved:
                raise ValueError(f"Physics feature tag already exists or is duplicated: {tag}")
            condition["resolved_tag"] = tag
            reserved.add(tag)
    except (TypeError, ValueError) as exc:
        return {"success": False, "error": str(exc)}

    entity_dimension = max(int(_component_sdim(component)) - 1, 0)
    created: list[str] = []
    results: list[dict[str, Any]] = []
    try:
        for condition in normalized:
            tag = condition["resolved_tag"]
            feature = feature_list.create(tag, condition["type"], entity_dimension)
            created.append(tag)
            _apply_selection(feature, condition["boundaries"], condition["selection_name"])
            for name, value in condition["properties"].items():
                feature.set(name, value)
            label = condition["label"]
            if label is not None:
                feature.label(label)
            results.append(
                {
                    "tag": tag,
                    "type": condition["type"],
                    "entity_dimension": entity_dimension,
                    "boundaries": condition["boundaries"] or None,
                    "selection_name": condition["selection_name"],
                    "properties": condition["properties"],
                }
            )
    except Exception:
        rolled_back = True
        for tag in reversed(created):
            try:
                feature_list.remove(tag)
            except Exception:
                rolled_back = False
        return {
            "success": False,
            "error": f"{family.capitalize()} boundary setup failed.",
            "rolled_back": rolled_back,
            "created_before_failure": len(created),
        }
    return {
        "success": True,
        "physics": str(physics.tag()),
        "family": family,
        "configured_boundaries": results,
        "configured_count": len(results),
    }


def _boundary_reference(reference: Mapping[str, Sequence[str]]) -> dict[str, Any]:
    return {name: {"properties": list(properties)} for name, properties in reference.items()}


def register_acoustics_pde_tools(mcp: MCPServer) -> None:
    """Register constrained Acoustics and PDE tools."""

    @mcp.tool()  # type: ignore[untyped-decorator]
    def physics_get_acoustic_boundary_conditions() -> dict[str, Any]:
        """Return the exact supported Pressure Acoustics boundary contract."""
        return {
            "success": True,
            "boundary_conditions": _boundary_reference(ACOUSTIC_BOUNDARY_CONDITIONS),
        }

    @mcp.tool()  # type: ignore[untyped-decorator]
    def physics_get_pde_boundary_conditions() -> dict[str, Any]:
        """Return the exact supported mathematical PDE boundary contract."""
        return {
            "success": True,
            "boundary_conditions": _boundary_reference(PDE_BOUNDARY_CONDITIONS),
            "aliases": dict(PDE_BOUNDARY_ALIASES),
        }

    @mcp.tool()  # type: ignore[untyped-decorator]
    def physics_add_pressure_acoustics(
        domain_selection: Optional[Sequence[int]] = None,
        selection_name: Optional[str] = None,
        component_name: Optional[str] = None,
        geometry_name: Optional[str] = None,
        physics_tag: str = "acpr",
        model_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """Add the enumerated Pressure Acoustics interface transactionally."""
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }
        return add_pressure_acoustics_interface(
            model,
            domain_selection=domain_selection,
            selection_name=selection_name,
            component_name=component_name,
            geometry_name=geometry_name,
            physics_tag=physics_tag,
        )

    def add_pde(
        form: str,
        dependent_variables: Sequence[str],
        equation_properties: object | None,
        domain_selection: Optional[Sequence[int]],
        selection_name: Optional[str],
        component_name: Optional[str],
        physics_tag: Optional[str],
        model_name: Optional[str],
    ) -> dict[str, Any]:
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }
        return add_pde_interface(
            model,
            form,
            dependent_variables=dependent_variables,
            equation_properties=equation_properties,
            domain_selection=domain_selection,
            selection_name=selection_name,
            component_name=component_name,
            physics_tag=physics_tag,
        )

    @mcp.tool()  # type: ignore[untyped-decorator]
    def physics_add_coefficient_form_pde(
        dependent_variables: Sequence[str] = ("u",),
        equation_properties: Optional[dict[str, JSONValue]] = None,
        domain_selection: Optional[Sequence[int]] = None,
        selection_name: Optional[str] = None,
        component_name: Optional[str] = None,
        physics_tag: str = "c",
        model_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """Add a bounded Coefficient Form PDE interface."""
        return add_pde(
            "coefficient",
            dependent_variables,
            equation_properties,
            domain_selection,
            selection_name,
            component_name,
            physics_tag,
            model_name,
        )

    @mcp.tool()  # type: ignore[untyped-decorator]
    def physics_add_general_form_pde(
        dependent_variables: Sequence[str] = ("u",),
        equation_properties: Optional[dict[str, JSONValue]] = None,
        domain_selection: Optional[Sequence[int]] = None,
        selection_name: Optional[str] = None,
        component_name: Optional[str] = None,
        physics_tag: str = "g",
        model_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """Add a bounded General Form PDE interface."""
        return add_pde(
            "general",
            dependent_variables,
            equation_properties,
            domain_selection,
            selection_name,
            component_name,
            physics_tag,
            model_name,
        )

    @mcp.tool()  # type: ignore[untyped-decorator]
    def physics_add_weak_form_pde(
        dependent_variables: Sequence[str] = ("u",),
        equation_properties: Optional[dict[str, JSONValue]] = None,
        domain_selection: Optional[Sequence[int]] = None,
        selection_name: Optional[str] = None,
        component_name: Optional[str] = None,
        physics_tag: str = "w",
        model_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """Add a bounded Weak Form PDE interface."""
        return add_pde(
            "weak",
            dependent_variables,
            equation_properties,
            domain_selection,
            selection_name,
            component_name,
            physics_tag,
            model_name,
        )

    def configure(
        family: str,
        physics_name: str,
        boundary_conditions: Sequence[Mapping[str, Any]],
        model_name: Optional[str],
    ) -> dict[str, Any]:
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }
        return configure_boundaries(model, physics_name, boundary_conditions, family=family)

    @mcp.tool()  # type: ignore[untyped-decorator]
    def physics_configure_acoustic_boundary(
        physics_name: str,
        boundary_condition: str,
        boundary_selection: Optional[Sequence[int]] = None,
        properties: Optional[dict[str, JSONValue]] = None,
        selection_name: Optional[str] = None,
        feature_tag: Optional[str] = None,
        label: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """Configure one exact Pressure Acoustics boundary condition."""
        return configure(
            "acoustic",
            physics_name,
            [
                {
                    "type": boundary_condition,
                    "boundaries": boundary_selection,
                    "properties": properties,
                    "selection_name": selection_name,
                    "tag": feature_tag,
                    "label": label,
                }
            ],
            model_name,
        )

    @mcp.tool()  # type: ignore[untyped-decorator]
    def physics_setup_acoustic_boundaries(
        physics_name: str,
        boundary_conditions: Sequence[dict[str, Any]],
        model_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """Configure a bounded atomic batch of acoustic boundaries."""
        return configure("acoustic", physics_name, boundary_conditions, model_name)

    @mcp.tool()  # type: ignore[untyped-decorator]
    def physics_configure_pde_boundary(
        physics_name: str,
        boundary_condition: str,
        boundary_selection: Optional[Sequence[int]] = None,
        properties: Optional[dict[str, JSONValue]] = None,
        selection_name: Optional[str] = None,
        feature_tag: Optional[str] = None,
        label: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """Configure one exact mathematical PDE boundary condition."""
        return configure(
            "pde",
            physics_name,
            [
                {
                    "type": boundary_condition,
                    "boundaries": boundary_selection,
                    "properties": properties,
                    "selection_name": selection_name,
                    "tag": feature_tag,
                    "label": label,
                }
            ],
            model_name,
        )

    @mcp.tool()  # type: ignore[untyped-decorator]
    def physics_setup_pde_boundaries(
        physics_name: str,
        boundary_conditions: Sequence[dict[str, Any]],
        model_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """Configure a bounded atomic batch of PDE boundaries."""
        return configure("pde", physics_name, boundary_conditions, model_name)


__all__ = [
    "ACOUSTIC_BOUNDARY_CONDITIONS",
    "PDE_BOUNDARY_ALIASES",
    "PDE_BOUNDARY_CONDITIONS",
    "add_pde_interface",
    "add_pressure_acoustics_interface",
    "configure_boundaries",
    "register_acoustics_pde_tools",
]
