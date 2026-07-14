"""Typed, solver-free PeriodicStructure incidence preview and mutation gates."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .derived_geometry import DerivedGeometryRecord, _record
from .session import session_manager
from .wave_optics_preflight import (
    _feature_inventory,
    _get,
    _is_kind,
    _label,
    _properties,
    _selection_entities,
    _tags,
)


_ANGLE_UNITS = frozenset({"deg", "rad"})
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SETTING_NAMES = (
    "Polarization",
    "LinearPol",
    "CircularPol",
    "alpha1_inc",
    "alpha2_inc",
)


def _bounded_text(value: object, *, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > limit or any(ord(char) < 32 for char in normalized):
        raise ValueError(f"{name} is invalid or exceeds {limit} characters")
    return normalized


def _real_scalar(value: Any, *, expression: str) -> float:
    while isinstance(value, (list, tuple)) or (
        hasattr(value, "shape") and hasattr(value, "__len__")
    ):
        if len(value) != 1:
            raise ValueError(f"angle expression did not evaluate to one scalar: {expression!r}")
        value = value[0]
    try:
        scalar = complex(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"angle expression is not numeric: {expression!r}") from exc
    if not math.isfinite(scalar.real) or not math.isfinite(scalar.imag):
        raise ValueError(f"angle expression is not finite: {expression!r}")
    if abs(scalar.imag) > 1e-12:
        raise ValueError(f"angle expression is not real: {expression!r}")
    return float(scalar.real)


def _parameter_names(model: Any) -> set[str]:
    try:
        return {str(name) for name in dict(model.parameters(evaluate=False))}
    except Exception:
        return set()


def _evaluate_angle(model: Any, expression: str, unit: str, parameters: set[str]) -> dict[str, Any]:
    if unit not in _ANGLE_UNITS:
        raise ValueError("angle units must be deg or rad")
    try:
        raw = model.evaluate(expression, unit=unit)
    except Exception as exc:
        raise ValueError(f"angle expression could not be evaluated: {expression!r}: {exc}") from exc
    return {
        "expression": expression,
        "expression_kind": (
            "parameter" if _IDENTIFIER.fullmatch(expression) and expression in parameters else "expression"
        ),
        "evaluated_value": _real_scalar(raw, expression=expression),
        "evaluated_unit": unit,
    }


def _component_physics(model: Any, component_tag: str, physics_tag: str) -> tuple[Any, Any]:
    components = model.java.component()
    if component_tag not in _tags(components):
        raise ValueError(f"component tag does not exist: {component_tag}")
    component = _get(components, component_tag)
    physics_container = component.physics()
    if physics_tag not in _tags(physics_container):
        raise ValueError(f"physics tag does not exist: {physics_tag}")
    return component, _get(physics_container, physics_tag)


def _incidence_snapshot(model: Any, component_tag: str, physics_tag: str) -> dict[str, Any]:
    _component, physics = _component_physics(model, component_tag, physics_tag)
    periodic_structures = [
        (tag, feature)
        for tag, feature, kind in _feature_inventory(physics.feature())
        if _is_kind(tag, kind, _label(feature), ("periodicstructure", "periodic structure"))
    ]
    if not periodic_structures:
        raise ValueError("exactly one PeriodicStructure is required; found 0")
    if len(periodic_structures) != 1:
        raise ValueError(
            f"exactly one PeriodicStructure is required; found {len(periodic_structures)}"
        )
    parent_tag, parent = periodic_structures[0]
    children = _feature_inventory(parent.feature())
    ports = [
        (tag, feature)
        for tag, feature, kind in children
        if _is_kind(tag, kind, _label(feature), ("periodicport", "periodic port"))
    ]
    if len(ports) != 2:
        raise ValueError(f"exactly two PeriodicPort children are required; found {len(ports)}")
    references = [
        (tag, feature)
        for tag, feature, kind in children
        if _is_kind(tag, kind, _label(feature), ("referencedirection", "reference direction", "rdir"))
    ]
    selected_references = []
    for tag, feature in references:
        entities, error = _selection_entities(feature)
        if error is not None:
            raise ValueError(f"reference-direction selection is unreadable for {tag}: {error}")
        if entities:
            selected_references.append({"tag": tag, "edge_ids": entities})
    if len(selected_references) != 1:
        raise ValueError(
            "exactly one non-empty rdir1/reference-direction selection is required; "
            f"found {len(selected_references)}"
        )
    return {
        "component_tag": component_tag,
        "physics_tag": physics_tag,
        "periodic_structure": {
            "tag": parent_tag,
            "settings": _properties(parent, _SETTING_NAMES),
        },
        "periodic_ports": [
            {"tag": tag, "settings": _properties(feature, _SETTING_NAMES)}
            for tag, feature in ports
        ],
        "reference_direction": selected_references[0],
    }


def _incidence_state_hash(record: DerivedGeometryRecord, snapshot: dict[str, Any]) -> str:
    payload = {
        "derived_model_id": record.derived_model_id,
        "source_sha256": record.source_sha256,
        "incidence": snapshot,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def preview_incidence(
    model: Any,
    record: DerivedGeometryRecord,
    *,
    alpha1_inc: str,
    alpha2_inc: str,
    alpha1_unit: str,
    alpha2_unit: str,
    polarization: str,
    physical_polarization_target: str,
    component_tag: str,
    physics_tag: str,
) -> dict[str, Any]:
    """Inspect and normalize one incidence request without mutating or solving."""
    if record.dirty:
        raise ValueError(f"derived model is dirty and unusable for validation: {record.dirty_reason}")
    alpha1 = _bounded_text(alpha1_inc, name="alpha1_inc", limit=200)
    alpha2 = _bounded_text(alpha2_inc, name="alpha2_inc", limit=200)
    target = _bounded_text(
        physical_polarization_target,
        name="physical_polarization_target",
        limit=200,
    )
    component = _bounded_text(component_tag, name="component_tag", limit=64)
    physics = _bounded_text(physics_tag, name="physics_tag", limit=64)
    if polarization not in {"S", "P", "rhcp", "lhcp"}:
        raise ValueError("polarization must be one of S, P, rhcp, or lhcp")
    snapshot = _incidence_snapshot(model, component, physics)
    parameters = _parameter_names(model)
    evaluated = {
        "alpha1_inc": _evaluate_angle(model, alpha1, alpha1_unit, parameters),
        "alpha2_inc": _evaluate_angle(model, alpha2, alpha2_unit, parameters),
    }
    parent_settings = {"alpha1_inc": alpha1, "alpha2_inc": alpha2}
    if polarization in {"S", "P"}:
        parent_settings.update({"Polarization": "LinearPol", "LinearPol": polarization})
    else:
        parent_settings.update({"Polarization": "CircularPol", "CircularPol": polarization})
    return {
        "operation": "periodic_structure_incidence",
        "derived_model_id": record.derived_model_id,
        "pre_state_sha256": _incidence_state_hash(record, snapshot),
        "before": snapshot,
        "request": {
            "alpha1_inc": alpha1,
            "alpha2_inc": alpha2,
            "polarization": polarization,
            "caller_physical_polarization_target": target,
        },
        "evaluated_angles": evaluated,
        "planned": {
            "periodic_structure": {
                "tag": snapshot["periodic_structure"]["tag"],
                "settings": parent_settings,
            },
            "periodic_ports": [
                {
                    "tag": port["tag"],
                    "settings": {"alpha1_inc": alpha1, "alpha2_inc": alpha2},
                }
                for port in snapshot["periodic_ports"]
            ],
        },
        "reference_edge_ids": snapshot["reference_direction"]["edge_ids"],
        "physical_polarization_evidence": "label_only",
        "physical_polarization_limitation": (
            "The S/P or circular label and rdir1 do not prove the physical incident field vector."
        ),
        "evidence_codes": [
            "periodic_structure_unique",
            "periodic_ports_exactly_two",
            "reference_direction_nonempty",
            "angle_expressions_evaluated",
            "physical_polarization_label_only",
        ],
        "mutated": False,
        "solver_started": False,
    }


def register_incidence_config_tools(mcp: FastMCP) -> None:
    """Register the derived-model-only incidence preview surface."""

    @mcp.tool()
    def wave_optics_incidence_preview(
        derived_model_id: str,
        model_name: str,
        alpha1_inc: str,
        alpha2_inc: str,
        polarization: Literal["S", "P", "rhcp", "lhcp"],
        physical_polarization_target: str,
        alpha1_unit: Literal["deg", "rad"] = "deg",
        alpha2_unit: Literal["deg", "rad"] = "deg",
        component_tag: str = "comp1",
        physics_tag: str = "ewfd",
    ) -> dict[str, Any]:
        """Preview evaluated PeriodicStructure incidence settings without mutation or solve."""
        try:
            record = _record(derived_model_id, model_name)
            model = session_manager.get_model(model_name)
            if model is None:
                raise ValueError(f"model is not loaded: {model_name}")
            return {
                "success": True,
                **preview_incidence(
                    model,
                    record,
                    alpha1_inc=alpha1_inc,
                    alpha2_inc=alpha2_inc,
                    alpha1_unit=alpha1_unit,
                    alpha2_unit=alpha2_unit,
                    polarization=polarization,
                    physical_polarization_target=physical_polarization_target,
                    component_tag=component_tag,
                    physics_tag=physics_tag,
                ),
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)[:500]}


__all__ = [
    "_incidence_snapshot",
    "_incidence_state_hash",
    "preview_incidence",
    "register_incidence_config_tools",
]
