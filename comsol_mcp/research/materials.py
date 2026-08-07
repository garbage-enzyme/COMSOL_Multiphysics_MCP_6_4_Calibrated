"""Provenance-bound research material catalog contracts."""

from __future__ import annotations

import re
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .contracts import _bounded_json, _finite, _identifier, _object, _text, _timestamp

MATERIAL_CATALOG_SCHEMA_NAME = "comsol_mcp.research_material_catalog"
MATERIAL_CATALOG_SCHEMA_VERSION = "1.0.0"
MAX_MATERIALS = 128
MAX_LIST_ITEMS = 128
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_STATES = {"measured", "fitted", "computed", "assumed", "literature_derived"}
_REPRESENTATIONS = {"complex_refractive_index", "complex_permittivity"}
_PHASOR_SIGNS = {"n_plus_ik", "epsilon_imag_positive_loss"}
_INTERPOLATIONS = {"linear", "cubic", "piecewise_constant"}


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def _string_list(value: object, name: str, *, identifiers: bool = False) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_LIST_ITEMS:
        raise ValueError(f"{name} must be a bounded nonempty list")
    normalized = [
        (
            _identifier(item, f"{name}[{index}]")
            if identifiers
            else _text(item, f"{name}[{index}]", maximum=1024)
        )
        for index, item in enumerate(value)
    ]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{name} must contain unique values")
    return sorted(normalized)


def _range(value: object, name: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{name} must contain two bounds")
    lower = _finite(value[0], f"{name}[0]", minimum=0.0)
    upper = _finite(value[1], f"{name}[1]", minimum=0.0)
    if lower >= upper:
        raise ValueError(f"{name} bounds must be ordered")
    return [lower, upper]


def _normalize_entry(value: object, index: int) -> dict[str, Any]:
    name = f"entries[{index}]"
    raw = _object(
        value,
        {
            "material_id",
            "display_name",
            "composition",
            "phase",
            "sample_state",
            "source",
            "validity",
            "optical_data",
            "evidence_status",
            "uncertainty",
            "comsol_mapping",
            "readback_expectations",
            "allowed_formulations",
            "compatibility_constraints",
            "fabrication_constraints",
            "caller_approved",
        },
        name,
    )
    source = _object(
        raw["source"],
        {"citation", "locator", "accessed_at", "source_sha256", "license"},
        f"{name}.source",
    )
    validity = _object(
        raw["validity"],
        {"wavelength_nm", "temperature_k"},
        f"{name}.validity",
    )
    optical = _object(
        raw["optical_data"],
        {
            "representation",
            "table_sha256",
            "phasor_sign",
            "interpolation",
            "extrapolation",
            "passive_expected",
        },
        f"{name}.optical_data",
    )
    if optical["representation"] not in _REPRESENTATIONS:
        raise ValueError(f"{name}.optical_data.representation is unsupported")
    if optical["phasor_sign"] not in _PHASOR_SIGNS:
        raise ValueError(f"{name}.optical_data.phasor_sign is unsupported")
    if optical["interpolation"] not in _INTERPOLATIONS:
        raise ValueError(f"{name}.optical_data.interpolation is unsupported")
    if optical["extrapolation"] != "forbidden":
        raise ValueError(f"{name}.optical_data.extrapolation must be forbidden")
    if not isinstance(optical["passive_expected"], bool):
        raise ValueError(f"{name}.optical_data.passive_expected must be boolean")
    if raw["evidence_status"] not in _EVIDENCE_STATES:
        raise ValueError(f"{name}.evidence_status is unsupported")
    uncertainty = _object(
        raw["uncertainty"],
        {"status", "description"},
        f"{name}.uncertainty",
    )
    if uncertainty["status"] not in {"quantified", "not_reported", "not_applicable"}:
        raise ValueError(f"{name}.uncertainty.status is unsupported")
    mapping = _object(
        raw["comsol_mapping"],
        {"property_group", "function_ids"},
        f"{name}.comsol_mapping",
    )
    if not isinstance(raw["caller_approved"], bool):
        raise ValueError(f"{name}.caller_approved must be boolean")
    strictly_verified = (
        raw["caller_approved"]
        and raw["evidence_status"] != "assumed"
        and optical["passive_expected"]
        and uncertainty["status"] != "not_reported"
    )
    return {
        "material_id": _identifier(raw["material_id"], f"{name}.material_id"),
        "display_name": _text(raw["display_name"], f"{name}.display_name", maximum=256),
        "composition": _text(raw["composition"], f"{name}.composition", maximum=512),
        "phase": _text(raw["phase"], f"{name}.phase", maximum=128),
        "sample_state": _text(raw["sample_state"], f"{name}.sample_state", maximum=512),
        "source": {
            "citation": _text(source["citation"], f"{name}.source.citation", maximum=2048),
            "locator": _text(source["locator"], f"{name}.source.locator", maximum=512),
            "accessed_at": _timestamp(source["accessed_at"], f"{name}.source.accessed_at"),
            "source_sha256": _sha256(source["source_sha256"], f"{name}.source.source_sha256"),
            "license": _text(source["license"], f"{name}.source.license", maximum=256),
        },
        "validity": {
            "wavelength_nm": _range(validity["wavelength_nm"], f"{name}.validity.wavelength_nm"),
            "temperature_k": _range(validity["temperature_k"], f"{name}.validity.temperature_k"),
        },
        "optical_data": {
            "representation": optical["representation"],
            "table_sha256": _sha256(optical["table_sha256"], f"{name}.optical_data.table_sha256"),
            "phasor_sign": optical["phasor_sign"],
            "interpolation": optical["interpolation"],
            "extrapolation": "forbidden",
            "passive_expected": optical["passive_expected"],
        },
        "evidence_status": raw["evidence_status"],
        "uncertainty": {
            "status": uncertainty["status"],
            "description": _text(
                uncertainty["description"], f"{name}.uncertainty.description", maximum=1024
            ),
        },
        "comsol_mapping": {
            "property_group": _identifier(
                mapping["property_group"], f"{name}.comsol_mapping.property_group"
            ),
            "function_ids": _string_list(
                mapping["function_ids"], f"{name}.comsol_mapping.function_ids", identifiers=True
            ),
        },
        "readback_expectations": _string_list(
            raw["readback_expectations"], f"{name}.readback_expectations", identifiers=True
        ),
        "allowed_formulations": _string_list(
            raw["allowed_formulations"], f"{name}.allowed_formulations", identifiers=True
        ),
        "compatibility_constraints": _string_list(
            raw["compatibility_constraints"], f"{name}.compatibility_constraints"
        ),
        "fabrication_constraints": _string_list(
            raw["fabrication_constraints"], f"{name}.fabrication_constraints"
        ),
        "caller_approved": raw["caller_approved"],
        "strictly_verified": strictly_verified,
    }


def normalize_material_catalog(value: object) -> dict[str, Any]:
    """Normalize an approved, immutable material search catalog."""
    raw = _object(
        _bounded_json(value, "material catalog", 1024 * 1024),
        {"schema_name", "schema_version", "catalog_id", "created_at", "entries"},
        "material catalog",
    )
    if (
        raw["schema_name"] != MATERIAL_CATALOG_SCHEMA_NAME
        or raw["schema_version"] != MATERIAL_CATALOG_SCHEMA_VERSION
    ):
        raise ValueError("material catalog schema identity is unsupported")
    entries_value = raw["entries"]
    if not isinstance(entries_value, list) or not 1 <= len(entries_value) <= MAX_MATERIALS:
        raise ValueError("entries must be a bounded nonempty list")
    entries = [_normalize_entry(item, index) for index, item in enumerate(entries_value)]
    material_ids = [item["material_id"] for item in entries]
    if len(material_ids) != len(set(material_ids)):
        raise ValueError("material_id values must be unique")
    body = {
        "schema_name": MATERIAL_CATALOG_SCHEMA_NAME,
        "schema_version": MATERIAL_CATALOG_SCHEMA_VERSION,
        "catalog_id": _identifier(raw["catalog_id"], "catalog_id"),
        "created_at": _timestamp(raw["created_at"], "created_at"),
        "entries": sorted(entries, key=lambda item: item["material_id"]),
        "caller_approved_material_ids": sorted(
            item["material_id"] for item in entries if item["caller_approved"]
        ),
        "strictly_verified_material_ids": sorted(
            item["material_id"] for item in entries if item["strictly_verified"]
        ),
    }
    return {
        **body,
        "catalog_fingerprint": domain_sha256_v2(MATERIAL_CATALOG_SCHEMA_NAME, body),
    }


__all__ = [
    "MATERIAL_CATALOG_SCHEMA_NAME",
    "MATERIAL_CATALOG_SCHEMA_VERSION",
    "normalize_material_catalog",
]
