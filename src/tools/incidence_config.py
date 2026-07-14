"""Typed, solver-free PeriodicStructure incidence preview and mutation gates."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
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
_MAX_DERIVED_EVENTS = 256
_INCIDENCE_MUTATION_LOCK = threading.RLock()


def _bounded_text(value: object, *, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > limit or any(ord(char) < 32 for char in normalized):
        raise ValueError(f"{name} is invalid or exceeds {limit} characters")
    return normalized


def _real_scalar(value: Any, *, expression: str) -> float:
    if getattr(value, "shape", None) == () and hasattr(value, "item"):
        # MPh 1.3.1 returns a zero-dimensional NumPy array for a scalar
        # clientapi evaluation.  It advertises ``shape`` but ``len(value)`` is
        # invalid, so unwrap it before handling one-element vectors.
        value = value.item()
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


def _append_event(record: DerivedGeometryRecord, event: dict[str, Any]) -> None:
    record.events.append(event)
    if len(record.events) > _MAX_DERIVED_EVENTS:
        del record.events[:-_MAX_DERIVED_EVENTS]


def _preview_incidence_unlocked(
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
    with _INCIDENCE_MUTATION_LOCK:
        return _preview_incidence_unlocked(
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
        )


def _incidence_nodes(
    model: Any,
    snapshot: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    _component, physics = _component_physics(
        model,
        snapshot["component_tag"],
        snapshot["physics_tag"],
    )
    parent = _get(physics.feature(), snapshot["periodic_structure"]["tag"])
    children = parent.feature()
    ports = {
        item["tag"]: _get(children, item["tag"])
        for item in snapshot["periodic_ports"]
    }
    return parent, ports


def _planned_readback_mismatches(
    snapshot: dict[str, Any],
    planned: dict[str, Any],
) -> list[str]:
    mismatches: list[str] = []
    parent = snapshot["periodic_structure"]
    for name, expected in planned["periodic_structure"]["settings"].items():
        actual = parent["settings"].get(name)
        if actual != expected:
            mismatches.append(
                f"{parent['tag']}.{name}: expected {expected!r}, read {actual!r}"
            )
    ports = {item["tag"]: item for item in snapshot["periodic_ports"]}
    for planned_port in planned["periodic_ports"]:
        actual_port = ports.get(planned_port["tag"])
        if actual_port is None:
            mismatches.append(f"{planned_port['tag']}: port missing on readback")
            continue
        for name, expected in planned_port["settings"].items():
            actual = actual_port["settings"].get(name)
            if actual != expected:
                mismatches.append(
                    f"{planned_port['tag']}.{name}: expected {expected!r}, read {actual!r}"
                )
    return mismatches


def _rollback_plan(before: dict[str, Any], planned: dict[str, Any]) -> dict[str, Any]:
    parent_before = before["periodic_structure"]
    parent_names = planned["periodic_structure"]["settings"]
    missing = [name for name in parent_names if name not in parent_before["settings"]]
    if missing:
        raise ValueError(
            "PeriodicStructure settings required for rollback are unreadable: "
            + ", ".join(missing)
        )
    before_ports = {item["tag"]: item for item in before["periodic_ports"]}
    port_plans = []
    for planned_port in planned["periodic_ports"]:
        tag = planned_port["tag"]
        captured = before_ports.get(tag)
        if captured is None:
            raise ValueError(f"PeriodicPort required for rollback is missing: {tag}")
        missing = [name for name in planned_port["settings"] if name not in captured["settings"]]
        if missing:
            raise ValueError(
                f"PeriodicPort settings required for rollback are unreadable for {tag}: "
                + ", ".join(missing)
            )
        port_plans.append(
            {
                "tag": tag,
                "settings": {
                    name: captured["settings"][name]
                    for name in planned_port["settings"]
                },
            }
        )
    return {
        "periodic_structure": {
            "tag": parent_before["tag"],
            "settings": {
                name: parent_before["settings"][name]
                for name in parent_names
            },
        },
        "periodic_ports": port_plans,
    }


def _set_plan(parent: Any, ports: dict[str, Any], planned: dict[str, Any]) -> None:
    for name, value in planned["periodic_structure"]["settings"].items():
        parent.set(name, value)
    for port in planned["periodic_ports"]:
        node = ports[port["tag"]]
        for name, value in port["settings"].items():
            node.set(name, value)


def _restore_plan(parent: Any, ports: dict[str, Any], planned: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    parent_tag = planned["periodic_structure"]["tag"]
    for name, value in planned["periodic_structure"]["settings"].items():
        try:
            parent.set(name, value)
        except Exception as exc:
            errors.append(f"{parent_tag}.{name}: {str(exc)[:200]}")
    for port in planned["periodic_ports"]:
        node = ports[port["tag"]]
        for name, value in port["settings"].items():
            try:
                node.set(name, value)
            except Exception as exc:
                errors.append(f"{port['tag']}.{name}: {str(exc)[:200]}")
    return errors[:20]


def _apply_incidence_unlocked(
    model: Any,
    record: DerivedGeometryRecord,
    preview: dict[str, Any],
    *,
    expected_state_sha256: str,
) -> dict[str, Any]:
    """Apply, read back, and atomically roll back one incidence preview."""
    component_tag = preview["before"]["component_tag"]
    physics_tag = preview["before"]["physics_tag"]
    current = _incidence_snapshot(model, component_tag, physics_tag)
    current_hash = _incidence_state_hash(record, current)
    if current_hash != expected_state_sha256 or current_hash != preview["pre_state_sha256"]:
        raise ValueError("stale incidence pre-state; preview must be regenerated")
    planned = preview["planned"]
    rollback = _rollback_plan(current, planned)
    parent, ports = _incidence_nodes(model, current)
    try:
        _set_plan(parent, ports, planned)
        after = _incidence_snapshot(model, component_tag, physics_tag)
        readback_mismatches = _planned_readback_mismatches(after, planned)
        if readback_mismatches:
            raise ValueError("incidence readback mismatch: " + "; ".join(readback_mismatches))
    except Exception as exc:
        rollback_write_errors: list[str] = []
        try:
            rollback_parent, rollback_ports = _incidence_nodes(model, current)
            rollback_write_errors = _restore_plan(
                rollback_parent,
                rollback_ports,
                rollback,
            )
        except Exception as rollback_exc:
            rollback_write_errors.append(str(rollback_exc)[:300])
        rollback_snapshot = None
        rollback_readback_mismatches: list[str] = []
        try:
            rollback_snapshot = _incidence_snapshot(model, component_tag, physics_tag)
            rollback_readback_mismatches = _planned_readback_mismatches(
                rollback_snapshot,
                rollback,
            )
        except Exception as rollback_exc:
            rollback_readback_mismatches.append(
                f"rollback snapshot unreadable: {str(rollback_exc)[:300]}"
            )
        rollback_proved = not rollback_readback_mismatches
        if not rollback_proved:
            record.dirty = True
            record.dirty_reason = (
                "incidence rollback unproven: "
                + "; ".join(rollback_readback_mismatches)
            )[:500]
        event = {
            "operation": "periodic_structure_incidence",
            "success": False,
            "rollback_proved": rollback_proved,
            "derived_model_dirty": record.dirty,
        }
        _append_event(record, event)
        return {
            "success": False,
            "error": str(exc)[:500],
            "derived_model_id": record.derived_model_id,
            "pre_state_sha256": current_hash,
            "before": current,
            "rollback_snapshot": rollback_snapshot,
            "rollback_proved": rollback_proved,
            "rollback_write_errors": rollback_write_errors,
            "rollback_readback_mismatches": rollback_readback_mismatches,
            "derived_model_dirty": record.dirty,
            "solver_started": False,
        }
    post_hash = _incidence_state_hash(record, after)
    _append_event(
        record,
        {
            "operation": "periodic_structure_incidence",
            "success": True,
            "pre_state_sha256": current_hash,
            "post_state_sha256": post_hash,
        },
    )
    return {
        "success": True,
        "derived_model_id": record.derived_model_id,
        "pre_state_sha256": current_hash,
        "post_state_sha256": post_hash,
        "before": current,
        "after": after,
        "evaluated_angles": preview["evaluated_angles"],
        "request": preview["request"],
        "reference_edge_ids": preview["reference_edge_ids"],
        "physical_polarization_evidence": "label_only",
        "evidence_codes": [
            *preview["evidence_codes"],
            "parent_and_ports_updated",
            "post_settings_read_back",
        ],
        "rollback_proved": None,
        "derived_model_dirty": False,
        "mutated": True,
        "solver_started": False,
    }


def apply_incidence(
    model: Any,
    record: DerivedGeometryRecord,
    preview: dict[str, Any],
    *,
    expected_state_sha256: str,
) -> dict[str, Any]:
    with _INCIDENCE_MUTATION_LOCK:
        return _apply_incidence_unlocked(
            model,
            record,
            preview,
            expected_state_sha256=expected_state_sha256,
        )


def register_incidence_config_tools(mcp: FastMCP) -> None:
    """Register the derived-model-only incidence preview/apply surface."""

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

    @mcp.tool()
    def wave_optics_incidence_apply(
        derived_model_id: str,
        model_name: str,
        expected_state_sha256: str,
        alpha1_inc: str,
        alpha2_inc: str,
        polarization: Literal["S", "P", "rhcp", "lhcp"],
        physical_polarization_target: str,
        alpha1_unit: Literal["deg", "rad"] = "deg",
        alpha2_unit: Literal["deg", "rad"] = "deg",
        component_tag: str = "comp1",
        physics_tag: str = "ewfd",
    ) -> dict[str, Any]:
        """Atomically apply and read back derived-model PeriodicStructure incidence."""
        try:
            record = _record(derived_model_id, model_name)
            model = session_manager.get_model(model_name)
            if model is None:
                raise ValueError(f"model is not loaded: {model_name}")
            with _INCIDENCE_MUTATION_LOCK:
                preview = _preview_incidence_unlocked(
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
                )
                return _apply_incidence_unlocked(
                    model,
                    record,
                    preview,
                    expected_state_sha256=expected_state_sha256,
                )
        except Exception as exc:
            return {"success": False, "error": str(exc)[:500]}


__all__ = [
    "_incidence_snapshot",
    "_incidence_state_hash",
    "apply_incidence",
    "preview_incidence",
    "register_incidence_config_tools",
]
