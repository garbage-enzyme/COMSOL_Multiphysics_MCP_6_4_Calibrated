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
    return normalized


__all__ = [
    "ADAPTER_ID",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "normalize_lin2025_pedot_cylinder_fixture",
]
