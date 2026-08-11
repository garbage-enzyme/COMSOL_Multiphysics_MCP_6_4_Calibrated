"""Immutable multi-condition and material-state optimization contracts."""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .derivative_support import (
    _bounded_json,
    _finite,
    _identifier,
    _object,
    _sha256,
    _text,
)

MATERIAL_STATE_CONFIGURATION_SCHEMA_NAME = "comsol_mcp.optimization_material_state"
MATERIAL_STATE_CONFIGURATION_SCHEMA_VERSION = "1.0.0"
OPTIMIZATION_CONDITION_TABLE_SCHEMA_NAME = "comsol_mcp.optimization_condition_table"
OPTIMIZATION_CONDITION_TABLE_SCHEMA_VERSION = "1.0.0"

MAX_MATERIAL_STATES = 16
MAX_CONDITIONS = 4096
_COMPLETENESS_MODES = {"cartesian_complete", "explicit_sparse"}
_OBJECTIVE_ROLES = {"objective", "constraint", "validation"}


def _optional_finite(value: object, name: str) -> float | None:
    return None if value is None else _finite(value, name)


def normalize_optimization_material_state(value: object) -> dict[str, Any]:
    """Normalize one immutable material state without embedding private data."""
    bounded = _bounded_json(value, "optimization material state", 64 * 1024)
    supplied = None
    if isinstance(bounded, dict) and "state_fingerprint" in bounded:
        supplied = bounded.pop("state_fingerprint")
    raw = _object(
        bounded,
        {
            "schema_name",
            "schema_version",
            "state_id",
            "material_ledger_sha256",
            "optical_property_source_sha256",
            "temperature_k",
            "provenance_disposition",
        },
        "optimization material state",
    )
    if (
        raw["schema_name"] != MATERIAL_STATE_CONFIGURATION_SCHEMA_NAME
        or raw["schema_version"] != MATERIAL_STATE_CONFIGURATION_SCHEMA_VERSION
    ):
        raise ValueError("optimization material state schema identity is unsupported")
    body = {
        "schema_name": MATERIAL_STATE_CONFIGURATION_SCHEMA_NAME,
        "schema_version": MATERIAL_STATE_CONFIGURATION_SCHEMA_VERSION,
        "state_id": _identifier(raw["state_id"], "state_id"),
        "material_ledger_sha256": _sha256(
            raw["material_ledger_sha256"], "material_ledger_sha256"
        ),
        "optical_property_source_sha256": _sha256(
            raw["optical_property_source_sha256"], "optical_property_source_sha256"
        ),
        "temperature_k": _finite(raw["temperature_k"], "temperature_k", positive=True),
        "provenance_disposition": _identifier(
            raw["provenance_disposition"], "provenance_disposition"
        ),
    }
    body["state_fingerprint"] = domain_sha256_v2(
        MATERIAL_STATE_CONFIGURATION_SCHEMA_NAME, body
    )
    if supplied is not None and supplied != body["state_fingerprint"]:
        raise ValueError("optimization material state fingerprint is invalid")
    return body


def _condition_row(value: object, *, index: int, state_ids: set[str]) -> dict[str, Any]:
    name = f"conditions[{index}]"
    raw = _object(
        value,
        {
            "condition_id",
            "order",
            "wavelength_m",
            "incidence_elevation_deg",
            "incidence_azimuth_deg",
            "polarization_basis_id",
            "excitation_sha256",
            "material_state_id",
            "objective_role",
            "observable_id",
            "weight",
            "target",
            "scale",
            "active",
        },
        name,
    )
    if (
        isinstance(raw["order"], bool)
        or not isinstance(raw["order"], int)
        or raw["order"] != index
    ):
        raise ValueError(f"{name}.order must be the canonical zero-based condition order")
    state_id = _identifier(raw["material_state_id"], f"{name}.material_state_id")
    if state_id not in state_ids:
        raise ValueError(f"{name}.material_state_id is not declared by the table")
    role = raw["objective_role"]
    if role not in _OBJECTIVE_ROLES:
        raise ValueError(f"{name}.objective_role is unsupported")
    if not isinstance(raw["active"], bool):
        raise ValueError(f"{name}.active must be boolean")
    return {
        "condition_id": _identifier(raw["condition_id"], f"{name}.condition_id"),
        "order": index,
        "wavelength_m": _finite(raw["wavelength_m"], f"{name}.wavelength_m", positive=True),
        "incidence_elevation_deg": _finite(
            raw["incidence_elevation_deg"], f"{name}.incidence_elevation_deg"
        ),
        "incidence_azimuth_deg": _finite(
            raw["incidence_azimuth_deg"], f"{name}.incidence_azimuth_deg"
        ),
        "polarization_basis_id": _identifier(
            raw["polarization_basis_id"], f"{name}.polarization_basis_id"
        ),
        "excitation_sha256": _sha256(raw["excitation_sha256"], f"{name}.excitation_sha256"),
        "material_state_id": state_id,
        "objective_role": role,
        "observable_id": _identifier(raw["observable_id"], f"{name}.observable_id"),
        "weight": _finite(raw["weight"], f"{name}.weight", positive=True),
        "target": _optional_finite(raw["target"], f"{name}.target"),
        "scale": _finite(raw["scale"], f"{name}.scale", positive=True),
        "active": raw["active"],
    }


def _coordinate(row: Mapping[str, Any]) -> tuple[float, float, float, str, str]:
    return (
        row["wavelength_m"],
        row["incidence_elevation_deg"],
        row["incidence_azimuth_deg"],
        row["polarization_basis_id"],
        row["material_state_id"],
    )


def _validate_completeness(rows: list[dict[str, Any]], completeness: object) -> dict[str, Any]:
    supplied_cardinalities = None
    if isinstance(completeness, Mapping) and "dimension_cardinalities" in completeness:
        completeness = dict(completeness)
        supplied_cardinalities = completeness.pop("dimension_cardinalities")
    raw = _object(completeness, {"mode", "sparse_justification"}, "completeness")
    mode = raw["mode"]
    if mode not in _COMPLETENESS_MODES:
        raise ValueError("completeness.mode is unsupported")
    coordinates = [_coordinate(row) for row in rows]
    if len(coordinates) != len(set(coordinates)):
        raise ValueError("condition coordinates must be unique")
    dimensions = [sorted({coordinate[index] for coordinate in coordinates}) for index in range(5)]
    expected = set(itertools.product(*dimensions))
    if mode == "cartesian_complete":
        if raw["sparse_justification"] is not None:
            raise ValueError("cartesian_complete tables must not declare sparse justification")
        if set(coordinates) != expected:
            raise ValueError("condition table is missing Cartesian combinations")
        justification = None
    else:
        justification = _text(
            raw["sparse_justification"], "completeness.sparse_justification", maximum=1024
        )
    cardinalities = {
        "wavelength": len(dimensions[0]),
        "incidence_elevation": len(dimensions[1]),
        "incidence_azimuth": len(dimensions[2]),
        "polarization_basis": len(dimensions[3]),
        "material_state": len(dimensions[4]),
    }
    if supplied_cardinalities is not None and supplied_cardinalities != cardinalities:
        raise ValueError("condition table dimension cardinalities are invalid")
    return {
        "mode": mode,
        "sparse_justification": justification,
        "dimension_cardinalities": cardinalities,
    }


def normalize_optimization_condition_table(value: object) -> dict[str, Any]:
    """Normalize a bounded, immutable, complete or explicitly sparse condition table."""
    bounded = _bounded_json(value, "optimization condition table", 2 * 1024 * 1024)
    supplied = None
    if isinstance(bounded, dict) and "condition_table_fingerprint" in bounded:
        supplied = bounded.pop("condition_table_fingerprint")
    raw = _object(
        bounded,
        {
            "schema_name",
            "schema_version",
            "table_id",
            "material_states",
            "conditions",
            "completeness",
        },
        "optimization condition table",
    )
    if (
        raw["schema_name"] != OPTIMIZATION_CONDITION_TABLE_SCHEMA_NAME
        or raw["schema_version"] != OPTIMIZATION_CONDITION_TABLE_SCHEMA_VERSION
    ):
        raise ValueError("optimization condition table schema identity is unsupported")
    states_raw = raw["material_states"]
    if not isinstance(states_raw, list) or not 1 <= len(states_raw) <= MAX_MATERIAL_STATES:
        raise ValueError("material_states must be a bounded nonempty list")
    states = [normalize_optimization_material_state(item) for item in states_raw]
    state_ids = [item["state_id"] for item in states]
    if len(state_ids) != len(set(state_ids)):
        raise ValueError("material state IDs must be unique")
    rows_raw = raw["conditions"]
    if not isinstance(rows_raw, list) or not 1 <= len(rows_raw) <= MAX_CONDITIONS:
        raise ValueError("conditions must be a bounded nonempty list")
    state_id_set = set(state_ids)
    rows = [
        _condition_row(item, index=index, state_ids=state_id_set)
        for index, item in enumerate(rows_raw)
    ]
    condition_ids = [item["condition_id"] for item in rows]
    if len(condition_ids) != len(set(condition_ids)):
        raise ValueError("condition IDs must be unique")
    completeness = _validate_completeness(rows, raw["completeness"])
    body = {
        "schema_name": OPTIMIZATION_CONDITION_TABLE_SCHEMA_NAME,
        "schema_version": OPTIMIZATION_CONDITION_TABLE_SCHEMA_VERSION,
        "table_id": _identifier(raw["table_id"], "table_id"),
        "material_states": states,
        "conditions": rows,
        "completeness": completeness,
    }
    body["condition_table_fingerprint"] = domain_sha256_v2(
        OPTIMIZATION_CONDITION_TABLE_SCHEMA_NAME, body
    )
    if supplied is not None and supplied != body["condition_table_fingerprint"]:
        raise ValueError("optimization condition table fingerprint is invalid")
    return body


__all__ = [
    "MATERIAL_STATE_CONFIGURATION_SCHEMA_NAME",
    "MATERIAL_STATE_CONFIGURATION_SCHEMA_VERSION",
    "OPTIMIZATION_CONDITION_TABLE_SCHEMA_NAME",
    "OPTIMIZATION_CONDITION_TABLE_SCHEMA_VERSION",
    "normalize_optimization_condition_table",
    "normalize_optimization_material_state",
]
