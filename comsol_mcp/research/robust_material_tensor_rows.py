"""Caller-supplied diagonal material tensor samples for robust adapters."""

from __future__ import annotations

from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .derivative_support import _bounded_json, _finite, _object, _sha256

SCHEMA_NAME = "comsol_mcp.robust_material_tensor_rows"
SCHEMA_VERSION = "1.0.0"
_STATES = ("OX", "MR")
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
                    key: _finite(
                        item_row[key], f"states[{index}].rows[{row_index}].{key}"
                    )
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


__all__ = ["SCHEMA_NAME", "SCHEMA_VERSION", "normalize_robust_material_tensor_rows"]
