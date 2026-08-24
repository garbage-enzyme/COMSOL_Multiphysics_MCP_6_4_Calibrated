"""Failure-atomic ClientAPI controls for the Lin2025 PEDOT cylinder fixture."""

from __future__ import annotations

import importlib
import math
from collections.abc import Mapping
from typing import Any, Protocol

from comsol_mcp.durable import domain_sha256_v2

from .derivative_support import normalize_derivative_support
from .lin2025_pedot_cylinder import (
    ADAPTER_ID,
    compile_lin2025_pedot_shape_support,
)

CONTROL_RECEIPT_SCHEMA_NAME = "comsol_mcp.lin2025_pedot_cylinder_control_receipt"
CONTROL_RECEIPT_SCHEMA_VERSION = "1.0.0"
_VARIABLES = ("pedot_cylinder_radius_x", "pedot_cylinder_radius_y")
_STATES = ("OX", "MR")
_UNIT_TO_METRE = {"m": 1.0, "um": 1e-6, "nm": 1e-9}


def _tags(container: Any) -> list[str]:
    return [str(value) for value in list(container.tags())]


def _get(container: Any, tag: str) -> Any:
    try:
        return container.get(tag)
    except Exception:
        return container(tag)


def _metres(value: float, unit: str, name: str) -> float:
    try:
        result = float(value) * _UNIT_TO_METRE[unit]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} uses an unsupported length value") from exc
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError(f"{name} must be finite and positive")
    return result


def _circle_readback(model: Any, component_tag: str, geometry_tag: str) -> dict[str, Any]:
    component = _get(model.java.component(), component_tag)
    geometry = _get(component.geom(), geometry_tag)
    features = geometry.feature()
    if "wp_pedot_cyl" not in _tags(features):
        raise ValueError("Lin2025 PEDOT work plane is absent")
    workplane = _get(features, "wp_pedot_cyl")
    if str(workplane.getType()) != "WorkPlane":
        raise ValueError("Lin2025 PEDOT work-plane type changed")
    plane_features = workplane.geom().feature()
    if "circ_pedot_cyl" not in _tags(plane_features):
        raise ValueError("Lin2025 PEDOT circle is absent")
    circle = _get(plane_features, "circ_pedot_cyl")
    if str(circle.getType()) != "Circle":
        raise ValueError("Lin2025 PEDOT circle type changed")
    try:
        radius = float(circle.getDouble("r"))
        position = [float(value) for value in list(circle.getDoubleArray("pos"))]
    except Exception as exc:
        raise ValueError("Lin2025 PEDOT circle properties are unreadable") from exc
    if not math.isfinite(radius) or radius <= 0.0 or len(position) != 2:
        raise ValueError("Lin2025 PEDOT circle properties are invalid")
    if any(not math.isfinite(value) for value in position):
        raise ValueError("Lin2025 PEDOT circle position is invalid")
    return {"radius_m": radius, "center_m": position}


def _material_readback(model: Any, component_tag: str) -> dict[str, Any]:
    component = _get(model.java.component(), component_tag)
    materials = component.material()
    if "mat_pedot" not in _tags(materials):
        raise ValueError("Lin2025 PEDOT material tag is absent")
    material = _get(materials, "mat_pedot")
    if str(material.getType()) != "Common":
        raise ValueError("Lin2025 PEDOT material type changed")
    try:
        selection = sorted(int(value) for value in list(material.selection().entities()))
        tensor = [
            str(value)
            for value in list(material.propertyGroup("def").getStringArray("relpermittivity"))
        ]
    except Exception as exc:
        raise ValueError("Lin2025 PEDOT material properties are unreadable") from exc
    if selection != [5] or len(tensor) not in {3, 9}:
        raise ValueError("Lin2025 PEDOT material domain or tensor shape changed")
    return {"material_tag": "mat_pedot", "domains": selection, "relpermittivity": tensor}


def _expression_is_zero(expression: str) -> bool:
    """Accept only numeric tensor expressions that evaluate to exactly zero."""
    try:
        return float(expression) == 0.0
    except ValueError:
        return False


class Lin2025PedotControlBackend(Protocol):
    """Minimal atomic surface used by the solver-free control compiler."""

    def snapshot(self) -> Mapping[str, Any]: ...

    def restore(self, snapshot: Mapping[str, Any]) -> None: ...

    def prepare_controls(
        self, derivative_support: Mapping[str, Any], shape_support: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...


class ClientapiLin2025PedotControlBackend:
    """Configure explicit fixed/free/PEDOT selections on one derived model."""

    def __init__(self, model: Any, *, component_tag: str, geometry_tag: str) -> None:
        self.model = model
        self.component_tag = component_tag
        self.geometry_tag = geometry_tag

    def snapshot(self) -> dict[str, Any]:
        component = _get(self.model.java.component(), self.component_tag)
        physics = component.physics()
        physics_state = {}
        for tag in _tags(physics):
            physics_state[tag] = {"features": _tags(_get(physics, tag).feature())}
        return {
            "parameters": dict(self.model.parameters()),
            "physics": physics_state,
            "circle": _circle_readback(self.model, self.component_tag, self.geometry_tag),
            "material": _material_readback(self.model, self.component_tag),
        }

    def restore(self, snapshot: Mapping[str, Any]) -> None:
        component = _get(self.model.java.component(), self.component_tag)
        physics = component.physics()
        expected_physics = snapshot.get("physics", {})
        for tag in list(_tags(physics)):
            if tag not in expected_physics:
                physics.remove(tag)
                continue
            features = _get(physics, tag).feature()
            original = set(expected_physics[tag].get("features", []))
            for feature_tag in list(_tags(features)):
                if feature_tag not in original:
                    features.remove(feature_tag)
        current = dict(self.model.parameters())
        original_parameters = dict(snapshot.get("parameters", {}))
        parameters = self.model.java.param()
        for name in set(current) - set(original_parameters):
            parameters.remove(name)
        for name, expression in original_parameters.items():
            parameters.set(name, expression)
        if _circle_readback(self.model, self.component_tag, self.geometry_tag) != snapshot.get(
            "circle"
        ):
            raise RuntimeError("Lin2025 source geometry changed during rollback")
        if _material_readback(self.model, self.component_tag) != snapshot.get("material"):
            raise RuntimeError("Lin2025 source material changed during rollback")

    def apply_material_state(self, state_id: str, tensor: list[str]) -> dict[str, Any]:
        if state_id not in _STATES:
            raise ValueError("Lin2025 material state must be OX or MR")
        if (
            not isinstance(tensor, list)
            or len(tensor) != 9
            or any(not isinstance(value, str) or not value.strip() for value in tensor)
        ):
            raise ValueError("Lin2025 material tensor must contain nine expressions")
        component = _get(self.model.java.component(), self.component_tag)
        materials = component.material()
        if "mat_pedot" not in _tags(materials):
            raise ValueError("Lin2025 PEDOT material tag is absent")
        material = _get(materials, "mat_pedot")
        if sorted(int(value) for value in list(material.selection().entities())) != [5]:
            raise ValueError("Lin2025 PEDOT material must select domain 5")
        jpype = importlib.import_module("jpype")
        group = material.propertyGroup("def")
        group.set("relpermittivity", jpype.JArray(jpype.JString)(tensor))
        readback = _material_readback(self.model, self.component_tag)
        observed = readback["relpermittivity"]
        off_diagonal_zero = all(
            index in (0, 4, 8) or _expression_is_zero(tensor[index]) for index in range(9)
        )
        if observed != tensor and not (
            off_diagonal_zero and observed == [tensor[0], tensor[4], tensor[8]]
        ):
            raise ValueError("Lin2025 material tensor readback differs from requested state")
        return {
            "state_id": state_id,
            "material_tag": readback["material_tag"],
            "domains": readback["domains"],
            "relpermittivity": readback["relpermittivity"],
        }

    def prepare_controls(
        self, derivative_support: Mapping[str, Any], shape_support: Mapping[str, Any]
    ) -> dict[str, Any]:
        variables = derivative_support["variables"]
        if [item["variable_id"] for item in variables] != list(_VARIABLES):
            raise ValueError("Lin2025 controls must be ordered cylinder x/y radii")
        if derivative_support["adapter_id"] != ADAPTER_ID:
            raise ValueError("Lin2025 control adapter identity changed")
        circle = _circle_readback(self.model, self.component_tag, self.geometry_tag)
        baseline_radius_m = float(shape_support["baseline_radius_um"]) * 1e-6
        center_m = [float(value) * 1e-6 for value in shape_support["center_um"]]
        if not math.isclose(
            circle["radius_m"], baseline_radius_m, rel_tol=1e-12, abs_tol=1e-15
        ) or any(
            not math.isclose(observed, expected, rel_tol=1e-12, abs_tol=1e-15)
            for observed, expected in zip(circle["center_m"], center_m, strict=True)
        ):
            raise ValueError("Lin2025 live circle differs from the shape-support baseline")
        # Validate the no-duplicate-physics precondition before the first
        # model mutation so a rejected request cannot leave written
        # parameters behind.
        component = _get(self.model.java.component(), self.component_tag)
        physics = component.physics()
        if "dg_pedot72" in _tags(physics):
            raise ValueError("Lin2025 PEDOT deformation interface already exists")
        expressions: dict[str, str] = {}
        parameters = self.model.java.param()
        for item in variables:
            baseline_m = _metres(item["baseline"], item["unit"], item["variable_id"])
            if not math.isclose(baseline_m, baseline_radius_m, rel_tol=1e-12, abs_tol=1e-15):
                raise ValueError("Lin2025 derivative baseline differs from live radius")
            expression = f"{item['baseline']:.17g}[{item['unit']}]"
            parameters.set(item["variable_id"], expression)
            expressions[item["variable_id"]] = expression
        deformation = physics.create("dg_pedot72", "DeformedGeometry", self.geometry_tag)
        features = deformation.feature()
        free = features.create("free_pedot72", "FreeDeformation", 3)
        fixed = features.create("fix_pedot72", "PrescribedMeshDisplacement", 2)
        pedot = features.create("pedot_pedot72", "PrescribedMeshDisplacement", 2)
        free.selection().set(shape_support["free_domains"])
        fixed.selection().set(shape_support["fixed_boundaries"])
        pedot.selection().set(shape_support["pedot_boundaries"])
        from comsol_mcp.tools.derived_geometry import _java_boolean

        displacement = [
            (
                f"({variables[0]['variable_id']}-{baseline_radius_m:.17g}[m])"
                f"*(x-{center_m[0]:.17g}[m])/{baseline_radius_m:.17g}[m]"
            ),
            (
                f"({variables[1]['variable_id']}-{baseline_radius_m:.17g}[m])"
                f"*(y-{center_m[1]:.17g}[m])/{baseline_radius_m:.17g}[m]"
            ),
            "0",
        ]
        for index in range(3):
            fixed.setIndex("useDx", _java_boolean(True), index)
            fixed.setIndex("dx", "0", index)
            pedot.setIndex("useDx", _java_boolean(True), index)
            pedot.setIndex("dx", displacement[index], index)
        readback = {
            "physics_tag": "dg_pedot72",
            "physics_type": str(deformation.getType()),
            "free_domains": sorted(int(value) for value in list(free.selection().entities())),
            "fixed_boundaries": sorted(int(value) for value in list(fixed.selection().entities())),
            "pedot_boundaries": sorted(int(value) for value in list(pedot.selection().entities())),
            "displacement": [str(value) for value in list(pedot.getStringArray("dx"))],
            "height_preserved": displacement[2] == "0",
            "center_preserved": True,
        }
        expected = {
            "physics_tag": "dg_pedot72",
            "physics_type": "DeformedGeometry",
            "free_domains": shape_support["free_domains"],
            "fixed_boundaries": shape_support["fixed_boundaries"],
            "pedot_boundaries": shape_support["pedot_boundaries"],
            "displacement": displacement,
            "height_preserved": True,
            "center_preserved": True,
        }
        if (
            readback != expected
            or _circle_readback(self.model, self.component_tag, self.geometry_tag) != circle
        ):
            raise ValueError("Lin2025 PEDOT control readback differs from the request")
        return {"parameters": expressions, "circle": circle, "deformed_geometry": readback}


def prepare_lin2025_pedot_shape_controls(
    backend: Lin2025PedotControlBackend,
    fixture: object,
    tree_readback: object,
    derivative_support: object,
) -> dict[str, Any]:
    """Create exact PEDOT controls and restore the full snapshot on failure."""
    shape_support = compile_lin2025_pedot_shape_support(fixture, tree_readback)
    support = normalize_derivative_support(derivative_support)
    if support["adapter_id"] != ADAPTER_ID:
        raise ValueError("Lin2025 derivative adapter identity differs from the fixture")
    if support["source_identity"] != shape_support["source_sha256"]:
        raise ValueError("Lin2025 derivative source identity differs from the fixture")
    snapshot = dict(backend.snapshot())
    try:
        controls = dict(backend.prepare_controls(support, shape_support))
        if dict(backend.snapshot()) == snapshot:
            raise ValueError("Lin2025 control preparation produced no model change")
    except Exception as exc:
        try:
            backend.restore(snapshot)
            if dict(backend.snapshot()) != snapshot:
                raise RuntimeError("Lin2025 rollback readback differs from the snapshot")
        except Exception as rollback_exc:
            raise RuntimeError(
                "Lin2025 control preparation failed and rollback was uncertain"
            ) from rollback_exc
        raise exc
    body = {
        "schema_name": CONTROL_RECEIPT_SCHEMA_NAME,
        "schema_version": CONTROL_RECEIPT_SCHEMA_VERSION,
        "fixture_fingerprint": shape_support["fixture_fingerprint"],
        "shape_support_fingerprint": shape_support["support_fingerprint"],
        "derivative_support_fingerprint": support["support_fingerprint"],
        "controls": controls,
        "rollback": {"attempted": False, "verified": False},
    }
    body["receipt_fingerprint"] = domain_sha256_v2(CONTROL_RECEIPT_SCHEMA_NAME, body)
    return body


__all__ = [
    "CONTROL_RECEIPT_SCHEMA_NAME",
    "CONTROL_RECEIPT_SCHEMA_VERSION",
    "ClientapiLin2025PedotControlBackend",
    "Lin2025PedotControlBackend",
    "prepare_lin2025_pedot_shape_controls",
]
