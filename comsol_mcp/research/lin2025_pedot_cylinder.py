"""Solver-free contract for the verified Lin2025 PEDOT-cylinder fixture."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

SCHEMA_NAME = "comsol_mcp.lin2025_pedot_cylinder_fixture"
SCHEMA_VERSION = "1.0.0"
ADAPTER_ID = "lin2025_pedot_cylinder_v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VARIABLES = ("pedot_cylinder_radius_x", "pedot_cylinder_radius_y")
_STATES = ("OX", "MR")
_DOMAIN_COUNT = 6
_BOUNDARY_COUNT = 32


def _sha(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return result


def _object(value: object, keys: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{name} fields are incomplete or unexpected")
    return dict(value)


def normalize_lin2025_pedot_cylinder_fixture(value: object) -> dict[str, Any]:
    """Normalize an exact source/topology/material contract before ClientAPI work."""
    supplied_fingerprint = value.get("fixture_fingerprint") if isinstance(value, Mapping) else None
    if isinstance(value, Mapping) and "fixture_fingerprint" in value:
        value = {key: item for key, item in value.items() if key != "fixture_fingerprint"}
    raw = _object(
        value,
        {
            "schema_name",
            "schema_version",
            "adapter_id",
            "source_identity",
            "geometry",
            "domains",
            "material_states",
            "mutable_variables",
            "temperature_k",
        },
        "PEDOT-cylinder fixture",
    )
    if raw["schema_name"] != SCHEMA_NAME or raw["schema_version"] != SCHEMA_VERSION:
        raise ValueError("PEDOT-cylinder fixture schema identity is unsupported")
    if raw["adapter_id"] != ADAPTER_ID:
        raise ValueError("PEDOT-cylinder fixture adapter identity is unsupported")
    source = _object(
        raw["source_identity"],
        {"source_sha256", "tree_sha256", "comsol_build"},
        "source_identity",
    )
    source = {
        **source,
        "source_sha256": _sha(source["source_sha256"], "source_sha256"),
        "tree_sha256": _sha(source["tree_sha256"], "tree_sha256"),
    }
    geometry = _object(
        raw["geometry"],
        {"lattice", "period_um", "pedot_height_um", "air_above", "substrate_n"},
        "geometry",
    )
    if geometry["lattice"] != "hexagonal_primitive" or geometry["air_above"] is not True:
        raise ValueError("fixture must preserve the verified hexagonal PEDOT-cylinder topology")
    geometry = {
        **geometry,
        "period_um": _finite(geometry["period_um"], "period_um"),
        "pedot_height_um": _finite(geometry["pedot_height_um"], "pedot_height_um"),
        "substrate_n": _finite(geometry["substrate_n"], "substrate_n"),
    }
    domains = _object(raw["domains"], {"substrate", "pedot_cylinder", "air"}, "domains")
    if any(
        isinstance(domains[key], bool)
        or not isinstance(domains[key], int)
        or domains[key] < 1
        for key in domains
    ):
        raise ValueError("domain IDs must be positive integers")
    states = raw["material_states"]
    if not isinstance(states, list) or tuple(
        item.get("state_id") for item in states if isinstance(item, Mapping)
    ) != _STATES:
        raise ValueError("material states must be ordered OX/MR")
    variables = raw["mutable_variables"]
    if not isinstance(variables, list) or tuple(
        item.get("variable_id") for item in variables if isinstance(item, Mapping)
    ) != _VARIABLES:
        raise ValueError("mutable variables must be ordered cylinder x/y radii")
    normalized = {
        **raw,
        "source_identity": source,
        "geometry": geometry,
        "domains": domains,
        "material_states": [dict(item) for item in states],
        "mutable_variables": [dict(item) for item in variables],
        "temperature_k": _finite(raw["temperature_k"], "temperature_k"),
    }
    normalized["fixture_fingerprint"] = domain_sha256_v2(SCHEMA_NAME, normalized)
    if (
        supplied_fingerprint is not None
        and supplied_fingerprint != normalized["fixture_fingerprint"]
    ):
        raise ValueError("PEDOT-cylinder fixture fingerprint is invalid")
    return normalized


def compile_lin2025_pedot_cylinder_binding(
    fixture: object,
    derivative_support: Mapping[str, Any],
    shape_policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind the Lin2025 fixture to caller-owned derivative and shape contracts."""
    normalized = normalize_lin2025_pedot_cylinder_fixture(fixture)
    if derivative_support.get("adapter_id") != ADAPTER_ID:
        raise ValueError("Lin2025 derivative adapter identity differs from fixture")
    if shape_policy.get("adapter_id") != ADAPTER_ID:
        raise ValueError("Lin2025 shape-policy adapter identity differs from fixture")
    if derivative_support.get("source_identity") != normalized["source_identity"]["source_sha256"]:
        raise ValueError("Lin2025 derivative source identity differs from fixture")
    variables = derivative_support.get("variables")
    if not isinstance(variables, list) or [
        item.get("variable_id") for item in variables
    ] != list(_VARIABLES):
        raise ValueError("Lin2025 derivative variables must be ordered cylinder x/y radii")
    body = {
        "schema_name": f"{SCHEMA_NAME}.binding",
        "schema_version": SCHEMA_VERSION,
        "adapter_id": ADAPTER_ID,
        "fixture_fingerprint": normalized["fixture_fingerprint"],
        "source_sha256": normalized["source_identity"]["source_sha256"],
        "derivative_support_fingerprint": derivative_support.get("support_fingerprint"),
        "shape_policy_fingerprint": shape_policy.get("policy_fingerprint"),
        "variable_ids": list(_VARIABLES),
        "pedot_domain": normalized["domains"]["pedot_cylinder"],
        "material_state_ids": list(_STATES),
        "temperature_k": normalized["temperature_k"],
    }
    body["binding_fingerprint"] = domain_sha256_v2(body["schema_name"], body)
    return body


def validate_lin2025_pedot_cylinder_tree(
    fixture: object, tree_readback: object
) -> dict[str, Any]:
    """Validate a live/read-only derived tree against the selected fixture."""
    normalized = normalize_lin2025_pedot_cylinder_fixture(fixture)
    raw = _object(
        tree_readback,
        {"source_sha256", "domain_map", "domain_z_bounds_um", "material_tags", "feature_tags"},
        "Lin2025 tree readback",
    )
    if raw["source_sha256"] != normalized["source_identity"]["source_sha256"]:
        raise ValueError("Lin2025 tree source identity differs from fixture")
    domain_map = _object(raw["domain_map"], {"substrate", "pedot_cylinder", "air"}, "domain_map")
    for name, expected in normalized["domains"].items():
        if domain_map[name] != expected:
            raise ValueError(f"Lin2025 {name} domain mapping changed")
    bounds = raw["domain_z_bounds_um"]
    if not isinstance(bounds, Mapping) or str(domain_map["pedot_cylinder"]) not in bounds:
        raise ValueError("Lin2025 PEDOT domain z bounds are missing")
    pedot_bounds = bounds[str(domain_map["pedot_cylinder"])]
    expected_bounds = [0.0, normalized["geometry"]["pedot_height_um"]]
    if (
        not isinstance(pedot_bounds, list)
        or len(pedot_bounds) != 2
        or pedot_bounds != expected_bounds
    ):
        raise ValueError("Lin2025 PEDOT cylinder z bounds changed")
    materials = raw["material_tags"]
    if not isinstance(materials, Mapping) or materials.get("pedot_cylinder") not in {"OX", "MR"}:
        raise ValueError("Lin2025 PEDOT material state readback is invalid")
    if any(
        "au" in str(tag).casefold() or "gold" in str(tag).casefold()
        for tag in raw["feature_tags"]
    ):
        raise ValueError("Lin2025 cylinder fixture unexpectedly contains Au")
    return {
        "schema_name": f"{SCHEMA_NAME}.tree_readback",
        "schema_version": SCHEMA_VERSION,
        "fixture_fingerprint": normalized["fixture_fingerprint"],
        "source_sha256": raw["source_sha256"],
        "domain_map": dict(domain_map),
        "pedot_state": materials["pedot_cylinder"],
        "feature_tags": list(raw["feature_tags"]),
        "tree_fingerprint": domain_sha256_v2(f"{SCHEMA_NAME}.tree_readback", raw),
    }


def compile_lin2025_pedot_shape_support(fixture: object, tree_readback: object) -> dict[str, Any]:
    """Compile explicit, read-back geometry selections for native shape controls.

    Boundary/domain IDs are accepted only from a live tree receipt; no local
    or host-derived topology defaults are permitted.
    """
    normalized = normalize_lin2025_pedot_cylinder_fixture(fixture)
    if not isinstance(tree_readback, Mapping):
        raise ValueError("Lin2025 shape tree readback must be an object")
    required = {
        "source_sha256",
        "domain_map",
        "domain_z_bounds_um",
        "material_tags",
        "feature_tags",
        "domain_count",
        "boundary_count",
        "exterior_boundaries",
        "pedot_boundaries",
        "pedot_lateral_boundaries",
        "pedot_cap_boundaries",
        "baseline_radius_um",
        "center_um",
    }
    if set(tree_readback) != required:
        raise ValueError("Lin2025 shape tree readback fields are incomplete")
    validate_lin2025_pedot_cylinder_tree(
        fixture,
        {
            key: tree_readback[key]
            for key in (
                "source_sha256",
                "domain_map",
                "domain_z_bounds_um",
                "material_tags",
                "feature_tags",
            )
        },
    )
    def _ids(value: object, name: str) -> list[int]:
        if not isinstance(value, list) or not value or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 1
            for item in value
        ):
            raise ValueError(f"Lin2025 {name} must be a non-empty positive integer list")
        result = sorted(set(value))
        if result != value:
            raise ValueError(f"Lin2025 {name} must be sorted and unique")
        return result
    domain_count = tree_readback["domain_count"]
    boundary_count = tree_readback["boundary_count"]
    if domain_count != _DOMAIN_COUNT or boundary_count != _BOUNDARY_COUNT:
        raise ValueError("Lin2025 topology counts changed")
    exterior = _ids(tree_readback["exterior_boundaries"], "exterior_boundaries")
    pedot = _ids(tree_readback["pedot_boundaries"], "pedot_boundaries")
    lateral = _ids(
        tree_readback["pedot_lateral_boundaries"], "pedot_lateral_boundaries"
    )
    caps = _ids(tree_readback["pedot_cap_boundaries"], "pedot_cap_boundaries")
    if len(exterior) != 18 or len(pedot) != 6 or len(lateral) != 4 or len(caps) != 2:
        raise ValueError("Lin2025 deformation selection cardinality changed")
    if sorted(lateral + caps) != pedot or set(exterior) & set(pedot):
        raise ValueError("Lin2025 deformation selections overlap or are incomplete")
    if any(item > boundary_count for item in exterior + pedot):
        raise ValueError("Lin2025 deformation selection exceeds the boundary count")
    radius = tree_readback["baseline_radius_um"]
    center = tree_readback["center_um"]
    if not isinstance(radius, (int, float)) or isinstance(radius, bool) or radius <= 0:
        raise ValueError("Lin2025 baseline radius must be positive")
    if not isinstance(center, list) or len(center) != 2 or any(
        not isinstance(item, (int, float)) or isinstance(item, bool) for item in center
    ):
        raise ValueError("Lin2025 cylinder center must contain two numbers")
    body = {
        "schema_name": f"{SCHEMA_NAME}.shape_support",
        "schema_version": SCHEMA_VERSION,
        "adapter_id": ADAPTER_ID,
        "fixture_fingerprint": normalized["fixture_fingerprint"],
        "source_sha256": normalized["source_identity"]["source_sha256"],
        "pedot_domain": normalized["domains"]["pedot_cylinder"],
        "domain_count": domain_count,
        "boundary_count": boundary_count,
        "free_domains": list(range(1, domain_count + 1)),
        "fixed_boundaries": exterior,
        "pedot_boundaries": pedot,
        "pedot_lateral_boundaries": lateral,
        "pedot_cap_boundaries": caps,
        "baseline_radius_um": float(radius),
        "center_um": [float(item) for item in center],
        "variable_ids": list(_VARIABLES),
        "height_preserved": True,
    }
    body["support_fingerprint"] = domain_sha256_v2(body["schema_name"], body)
    return body


__all__ = [
    "ADAPTER_ID",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "normalize_lin2025_pedot_cylinder_fixture",
    "compile_lin2025_pedot_cylinder_binding",
    "validate_lin2025_pedot_cylinder_tree",
    "compile_lin2025_pedot_shape_support",
]
