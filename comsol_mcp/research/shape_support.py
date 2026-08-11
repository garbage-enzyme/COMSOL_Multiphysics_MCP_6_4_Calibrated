"""Shape manufacturability, mesh admission, and retention contracts."""

from __future__ import annotations

from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .derivative_support import _bounded_json, _finite, _identifier, _object

SHAPE_SUPPORT_POLICY_SCHEMA_NAME = "comsol_mcp.shape_support_policy"
SHAPE_SUPPORT_POLICY_SCHEMA_VERSION = "1.0.0"

_GAP_MODES = {"recommended", "explicit", "not_requested"}
_RETENTION_MODES = {"finalist_only", "all"}


def _optional_positive(value: object, name: str) -> float | None:
    return None if value is None else _finite(value, name, positive=True)


def _bounded_integer(value: object, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in the allowed range")
    return value


def normalize_shape_support_policy(value: object) -> dict[str, Any]:
    """Normalize caller-owned geometry, mesh, and model-retention limits."""
    bounded = _bounded_json(value, "shape support policy", 128 * 1024)
    supplied = None
    if isinstance(bounded, dict) and "policy_fingerprint" in bounded:
        supplied = bounded.pop("policy_fingerprint")
    raw = _object(
        bounded,
        {
            "schema_name",
            "schema_version",
            "policy_id",
            "adapter_id",
            "minimum_gap",
            "geometry_guards",
            "mesh_admission",
            "model_retention",
        },
        "shape support policy",
    )
    if (
        raw["schema_name"] != SHAPE_SUPPORT_POLICY_SCHEMA_NAME
        or raw["schema_version"] != SHAPE_SUPPORT_POLICY_SCHEMA_VERSION
    ):
        raise ValueError("shape support policy schema identity is unsupported")

    gap_value = raw["minimum_gap"]
    supplied_gap_derived = None
    if isinstance(gap_value, dict) and {
        "effective_value_m",
        "claim_disposition",
    }.issubset(gap_value):
        gap_value = dict(gap_value)
        supplied_gap_derived = (
            gap_value.pop("effective_value_m"),
            gap_value.pop("claim_disposition"),
        )
    gap = _object(
        gap_value,
        {
            "mode",
            "explicit_value_m",
            "geometry_reference_length_m",
            "mesh_resolution_m",
            "mesh_multiplier",
            "geometry_relative_floor",
        },
        "minimum_gap",
    )
    mode = gap["mode"]
    if mode not in _GAP_MODES:
        raise ValueError("minimum_gap.mode is unsupported")
    explicit = _optional_positive(gap["explicit_value_m"], "minimum_gap.explicit_value_m")
    geometry_length = _optional_positive(
        gap["geometry_reference_length_m"], "minimum_gap.geometry_reference_length_m"
    )
    mesh_resolution = _optional_positive(
        gap["mesh_resolution_m"], "minimum_gap.mesh_resolution_m"
    )
    mesh_multiplier = _optional_positive(
        gap["mesh_multiplier"], "minimum_gap.mesh_multiplier"
    )
    geometry_floor = _optional_positive(
        gap["geometry_relative_floor"], "minimum_gap.geometry_relative_floor"
    )
    if mode == "explicit":
        if explicit is None or any(
            item is not None
            for item in (geometry_length, mesh_resolution, mesh_multiplier, geometry_floor)
        ):
            raise ValueError("explicit minimum gap requires only explicit_value_m")
        effective_gap = explicit
        claim = "caller_explicit"
    elif mode == "recommended":
        if explicit is not None or any(
            item is None
            for item in (geometry_length, mesh_resolution, mesh_multiplier, geometry_floor)
        ):
            raise ValueError("recommended minimum gap requires geometry and mesh inputs")
        geometry_length_value = float(geometry_length)
        mesh_resolution_value = float(mesh_resolution)
        mesh_multiplier_value = float(mesh_multiplier)
        geometry_floor_value = float(geometry_floor)
        if geometry_floor_value > 1.0:
            raise ValueError("minimum_gap.geometry_relative_floor must not exceed one")
        effective_gap = max(
            mesh_resolution_value * mesh_multiplier_value,
            geometry_length_value * geometry_floor_value,
        )
        claim = "adapter_geometry_mesh_recommendation"
    else:
        if any(
            item is not None
            for item in (
                explicit,
                geometry_length,
                mesh_resolution,
                mesh_multiplier,
                geometry_floor,
            )
        ):
            raise ValueError("not_requested minimum gap must not declare recommendation inputs")
        effective_gap = None
        claim = "not_requested"

    geometry = _object(
        raw["geometry_guards"],
        {
            "minimum_thickness_m",
            "minimum_radius_m",
            "preserve_topology",
            "preserve_selections",
            "require_positive_dimensions",
            "reject_self_intersection",
        },
        "geometry_guards",
    )
    boolean_geometry = {
        key: geometry[key]
        for key in (
            "preserve_topology",
            "preserve_selections",
            "require_positive_dimensions",
            "reject_self_intersection",
        )
    }
    if any(not isinstance(item, bool) for item in boolean_geometry.values()):
        raise ValueError("geometry guard switches must be boolean")
    if not all(boolean_geometry.values()):
        raise ValueError("alpha7.2 shape adapters require every invariant geometry guard")

    mesh_value = raw["mesh_admission"]
    supplied_mesh_scope = None
    if isinstance(mesh_value, dict) and "scope" in mesh_value:
        mesh_value = dict(mesh_value)
        supplied_mesh_scope = mesh_value.pop("scope")
    mesh = _object(
        mesh_value,
        {
            "max_elements_per_model",
            "minimum_element_quality",
            "quality_measure",
            "check_after_build",
            "check_after_remesh",
            "check_before_solve",
        },
        "mesh_admission",
    )
    switches = {
        key: mesh[key] for key in ("check_after_build", "check_after_remesh", "check_before_solve")
    }
    if any(not isinstance(item, bool) for item in switches.values()) or not all(switches.values()):
        raise ValueError("mesh admission must check every build, remesh, and pre-solve boundary")
    minimum_quality = _finite(
        mesh["minimum_element_quality"], "mesh_admission.minimum_element_quality", positive=True
    )
    if minimum_quality > 1.0:
        raise ValueError("mesh_admission.minimum_element_quality must not exceed one")
    if supplied_gap_derived is not None and supplied_gap_derived != (effective_gap, claim):
        raise ValueError("minimum gap derived fields are invalid")
    if supplied_mesh_scope is not None and supplied_mesh_scope != "per_model_not_cumulative":
        raise ValueError("mesh admission scope is invalid")

    retention = _object(
        raw["model_retention"], {"mode", "max_retained_models"}, "model_retention"
    )
    retention_mode = retention["mode"]
    if retention_mode not in _RETENTION_MODES:
        raise ValueError("model_retention.mode is unsupported")
    max_retained = _bounded_integer(
        retention["max_retained_models"],
        "model_retention.max_retained_models",
        minimum=1,
        maximum=100_000,
    )
    if retention_mode == "finalist_only" and max_retained != 1:
        raise ValueError("finalist_only retention must retain exactly one model")

    body = {
        "schema_name": SHAPE_SUPPORT_POLICY_SCHEMA_NAME,
        "schema_version": SHAPE_SUPPORT_POLICY_SCHEMA_VERSION,
        "policy_id": _identifier(raw["policy_id"], "policy_id"),
        "adapter_id": _identifier(raw["adapter_id"], "adapter_id"),
        "minimum_gap": {
            "mode": mode,
            "explicit_value_m": explicit,
            "geometry_reference_length_m": geometry_length,
            "mesh_resolution_m": mesh_resolution,
            "mesh_multiplier": mesh_multiplier,
            "geometry_relative_floor": geometry_floor,
            "effective_value_m": effective_gap,
            "claim_disposition": claim,
        },
        "geometry_guards": {
            "minimum_thickness_m": _optional_positive(
                geometry["minimum_thickness_m"], "geometry_guards.minimum_thickness_m"
            ),
            "minimum_radius_m": _optional_positive(
                geometry["minimum_radius_m"], "geometry_guards.minimum_radius_m"
            ),
            **boolean_geometry,
        },
        "mesh_admission": {
            "max_elements_per_model": _bounded_integer(
                mesh["max_elements_per_model"],
                "mesh_admission.max_elements_per_model",
                minimum=1,
                maximum=1_000_000_000,
            ),
            "minimum_element_quality": minimum_quality,
            "quality_measure": _identifier(
                mesh["quality_measure"], "mesh_admission.quality_measure"
            ),
            **switches,
            "scope": "per_model_not_cumulative",
        },
        "model_retention": {"mode": retention_mode, "max_retained_models": max_retained},
    }
    body["policy_fingerprint"] = domain_sha256_v2(SHAPE_SUPPORT_POLICY_SCHEMA_NAME, body)
    if supplied is not None and supplied != body["policy_fingerprint"]:
        raise ValueError("shape support policy fingerprint is invalid")
    return body


__all__ = [
    "SHAPE_SUPPORT_POLICY_SCHEMA_NAME",
    "SHAPE_SUPPORT_POLICY_SCHEMA_VERSION",
    "normalize_shape_support_policy",
]
