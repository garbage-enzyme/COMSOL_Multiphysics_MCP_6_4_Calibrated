"""Caller-supplied diagonal material tensor samples for robust adapters."""

from __future__ import annotations

import math
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .derivative_support import _bounded_json, _finite, _object, _sha256
from .robust_material_mapping import normalize_optical_property_mapping

SCHEMA_NAME = "comsol_mcp.robust_material_tensor_rows"
SCHEMA_VERSION = "1.0.0"
_STATES = ("OX", "MR")
_WAVELENGTH_UNIT_TO_METRE = {"m": 1.0, "cm": 1e-2, "mm": 1e-3, "um": 1e-6, "nm": 1e-9, "pm": 1e-12}
_ROW_FIELDS = {
    "wavelength_m",
    "xx_real",
    "xx_imag",
    "yy_real",
    "yy_imag",
    "zz_real",
    "zz_imag",
}


def normalize_robust_material_tensor_rows(value: object) -> dict[str, Any]:
    bounded = _bounded_json(value, "robust material tensor rows", 512 * 1024)
    supplied = bounded.pop("rows_fingerprint", None) if isinstance(bounded, dict) else None
    raw = _object(
        bounded,
        {"schema_name", "schema_version", "source_sha256", "states"},
        "robust material tensor rows",
    )
    if raw["schema_name"] != SCHEMA_NAME or raw["schema_version"] != SCHEMA_VERSION:
        raise ValueError("robust material tensor rows schema is unsupported")
    states = raw["states"]
    if (
        not isinstance(states, list)
        or tuple(item.get("state_id") for item in states if isinstance(item, dict)) != _STATES
    ):
        raise ValueError("material tensor states must be ordered OX/MR")
    normalized_states: list[dict[str, Any]] = []
    for index, item in enumerate(states):
        state = _object(item, {"state_id", "source_sha256", "rows"}, f"states[{index}]")
        if state["state_id"] != _STATES[index]:
            raise ValueError("material tensor state order is invalid")
        source_sha256 = _sha256(state["source_sha256"], f"states[{index}].source_sha256")
        rows = state["rows"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 4096:
            raise ValueError(f"states[{index}].rows must be a bounded nonempty list")
        normalized_rows = []
        previous = 0.0
        for row_index, row in enumerate(rows):
            item_row = _object(row, _ROW_FIELDS, f"states[{index}].rows[{row_index}]")
            wavelength = _finite(
                item_row["wavelength_m"], f"states[{index}].rows[{row_index}].wavelength_m"
            )
            if wavelength <= previous:
                raise ValueError("material tensor wavelengths must be strictly increasing")
            previous = wavelength
            normalized_rows.append(
                {
                    key: _finite(item_row[key], f"states[{index}].rows[{row_index}].{key}")
                    for key in _ROW_FIELDS
                }
            )
        normalized_states.append(
            {"state_id": state["state_id"], "source_sha256": source_sha256, "rows": normalized_rows}
        )
    body = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "source_sha256": _sha256(raw["source_sha256"], "source_sha256"),
        "states": normalized_states,
    }
    body["rows_fingerprint"] = domain_sha256_v2(SCHEMA_NAME, body)
    if supplied is not None and supplied != body["rows_fingerprint"]:
        raise ValueError("material tensor rows fingerprint is invalid")
    return body


def bind_robust_material_tensor_rows(
    value: object,
    condition_table: object,
    *,
    expected_temperature_k: object,
    finalist_wavelength_relative_offsets: tuple[float, ...] = (),
) -> dict[str, Any]:
    """Bind exact tensor samples to all active condition wavelengths and state sources."""
    rows = normalize_robust_material_tensor_rows(value)
    if not isinstance(condition_table, dict):
        raise ValueError("condition table must be normalized before tensor-row binding")
    temperature = _finite(expected_temperature_k, "expected_temperature_k", positive=True)
    material_states = condition_table.get("material_states")
    conditions = condition_table.get("conditions")
    if not isinstance(material_states, list) or not isinstance(conditions, list):
        raise ValueError("condition table is incomplete for tensor-row binding")
    states_by_id: dict[str, dict[str, Any]] = {}
    for item in material_states:
        if not isinstance(item, dict) or not isinstance(item.get("state_id"), str):
            raise ValueError("condition table material state identity is invalid")
        states_by_id[item["state_id"]] = item
    tensor_by_id = {item["state_id"]: item for item in rows["states"]}
    if set(states_by_id) != set(tensor_by_id):
        raise ValueError("material tensor state IDs differ from the condition table")
    required_wavelengths: dict[str, set[float]] = {state_id: set() for state_id in tensor_by_id}
    for condition in conditions:
        if not isinstance(condition, dict):
            raise ValueError("condition table row is invalid for tensor-row binding")
        if condition.get("active"):
            required_wavelengths[condition["material_state_id"]].add(condition["wavelength_m"])
            if condition.get("objective_role") == "objective":
                for offset in finalist_wavelength_relative_offsets:
                    wavelength = _finite(
                        condition["wavelength_m"] * (1.0 + _finite(offset, "finalist offset")),
                        "finalist wavelength",
                        positive=True,
                    )
                    required_wavelengths[condition["material_state_id"]].add(wavelength)
    for state_id, state in states_by_id.items():
        tensor_state = tensor_by_id[state_id]
        if tensor_state["source_sha256"] != state.get("optical_property_source_sha256"):
            raise ValueError("material tensor source identity differs from the condition table")
        if state.get("temperature_k") != temperature:
            raise ValueError("material-state temperature differs from the adapter fixture")
        mapping = normalize_optical_property_mapping(state.get("optical_property_mapping"))
        if mapping["source_sha256"] != tensor_state["source_sha256"]:
            raise ValueError("material tensor source identity differs from its optical mapping")
        independent = mapping["independent_variable"]
        scale = _WAVELENGTH_UNIT_TO_METRE.get(independent["source_unit"])
        if scale is None:
            raise ValueError("material mapping wavelength unit is unsupported")
        minimum = independent["valid_min"] * scale
        maximum = independent["valid_max"] * scale
        if not all(math.isfinite(bound) and bound > 0.0 for bound in (minimum, maximum)):
            raise ValueError(
                "material mapping wavelength range is not finite and positive in metres"
            )
        for required in required_wavelengths[state_id]:
            if (
                required < minimum
                and not math.isclose(required, minimum, rel_tol=1e-12, abs_tol=1e-18)
            ) or (
                required > maximum
                and not math.isclose(required, maximum, rel_tol=1e-12, abs_tol=1e-18)
            ):
                raise ValueError("active wavelength is outside the declared material mapping range")
        available = [item["wavelength_m"] for item in tensor_state["rows"]]
        if any(
            not any(
                math.isclose(required, sample, rel_tol=1e-12, abs_tol=1e-18) for sample in available
            )
            for required in required_wavelengths[state_id]
        ):
            raise ValueError("material tensor rows do not exactly cover active wavelengths")
    body = {
        "rows_fingerprint": rows["rows_fingerprint"],
        "condition_table_fingerprint": condition_table.get("condition_table_fingerprint"),
        "temperature_k": temperature,
        "state_ids": list(tensor_by_id),
        "active_wavelengths_m": {
            state_id: sorted(required_wavelengths[state_id]) for state_id in tensor_by_id
        },
    }
    return {
        **body,
        "binding_fingerprint": domain_sha256_v2(
            "comsol_mcp.robust_material_tensor_rows_binding", body
        ),
    }


__all__ = [
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "bind_robust_material_tensor_rows",
    "normalize_robust_material_tensor_rows",
]
