"""Typed optical-property tensor mapping for immutable material states."""

from __future__ import annotations

from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .derivative_support import _bounded_json, _finite, _identifier, _object, _sha256

OPTICAL_PROPERTY_MAPPING_SCHEMA_NAME = "comsol_mcp.optimization_optical_property_mapping"
OPTICAL_PROPERTY_MAPPING_SCHEMA_VERSION = "1.0.0"

_COMPONENTS = ("xx", "yy", "zz")
_TIME_CONVENTIONS = {"exp_positive_i_omega_t", "exp_negative_i_omega_t"}
_INTERPOLATION_METHODS = {"linear", "piecewise_cubic"}

_MISSING = object()


def normalize_optical_property_mapping(value: object) -> dict[str, Any]:
    """Normalize a diagonal tensor mapping without reading private source bytes."""
    bounded = _bounded_json(value, "optical property mapping", 128 * 1024)
    supplied: Any = _MISSING
    if isinstance(bounded, dict) and "mapping_fingerprint" in bounded:
        supplied = bounded.pop("mapping_fingerprint")
    raw = _object(
        bounded,
        {
            "schema_name",
            "schema_version",
            "mapping_id",
            "active_domain_id",
            "source_sha256",
            "independent_variable",
            "tensor",
            "time_harmonic_convention",
        },
        "optical property mapping",
    )
    if (
        raw["schema_name"] != OPTICAL_PROPERTY_MAPPING_SCHEMA_NAME
        or raw["schema_version"] != OPTICAL_PROPERTY_MAPPING_SCHEMA_VERSION
    ):
        raise ValueError("optical property mapping schema identity is unsupported")
    independent = _object(
        raw["independent_variable"],
        {
            "kind",
            "source_column",
            "source_unit",
            "valid_min",
            "valid_max",
            "interpolation_method",
            "extrapolation",
        },
        "independent_variable",
    )
    if independent["kind"] != "vacuum_wavelength":
        raise ValueError("optical property mapping requires vacuum wavelength")
    minimum = _finite(independent["valid_min"], "independent_variable.valid_min", positive=True)
    maximum = _finite(independent["valid_max"], "independent_variable.valid_max", positive=True)
    if maximum <= minimum:
        raise ValueError("optical property wavelength range must be increasing")
    interpolation = independent["interpolation_method"]
    if not isinstance(interpolation, str) or interpolation not in _INTERPOLATION_METHODS:
        raise ValueError("optical property interpolation method is unsupported")
    if independent["extrapolation"] != "forbidden":
        raise ValueError("optical property extrapolation must be forbidden")

    tensor = _object(
        raw["tensor"],
        {"basis", "form", "off_diagonal_zero", "components"},
        "tensor",
    )
    if (
        tensor["basis"] != "model_cartesian"
        or tensor["form"] != "diagonal"
        or tensor["off_diagonal_zero"] is not True
    ):
        raise ValueError("optical property mapping requires a diagonal model-Cartesian tensor")
    components = tensor["components"]
    if not isinstance(components, list) or len(components) != len(_COMPONENTS):
        raise ValueError("optical property tensor must declare xx, yy, and zz")
    normalized_components = []
    for index, (item, expected) in enumerate(zip(components, _COMPONENTS, strict=True)):
        component = _object(
            item,
            {"component", "source_axis_id", "real_column", "imaginary_column"},
            f"tensor.components[{index}]",
        )
        if component["component"] != expected:
            raise ValueError("optical property tensor component order must be xx, yy, zz")
        normalized_components.append(
            {
                "component": expected,
                "source_axis_id": _identifier(
                    component["source_axis_id"], f"tensor.components[{index}].source_axis_id"
                ),
                "real_column": _identifier(
                    component["real_column"], f"tensor.components[{index}].real_column"
                ),
                "imaginary_column": _identifier(
                    component["imaginary_column"],
                    f"tensor.components[{index}].imaginary_column",
                ),
            }
        )
    time_convention = raw["time_harmonic_convention"]
    if not isinstance(time_convention, str) or time_convention not in _TIME_CONVENTIONS:
        raise ValueError("optical property time-harmonic convention is unsupported")
    body = {
        "schema_name": OPTICAL_PROPERTY_MAPPING_SCHEMA_NAME,
        "schema_version": OPTICAL_PROPERTY_MAPPING_SCHEMA_VERSION,
        "mapping_id": _identifier(raw["mapping_id"], "mapping_id"),
        "active_domain_id": _identifier(raw["active_domain_id"], "active_domain_id"),
        "source_sha256": _sha256(raw["source_sha256"], "source_sha256"),
        "independent_variable": {
            "kind": "vacuum_wavelength",
            "source_column": _identifier(
                independent["source_column"], "independent_variable.source_column"
            ),
            "source_unit": _identifier(
                independent["source_unit"], "independent_variable.source_unit"
            ),
            "valid_min": minimum,
            "valid_max": maximum,
            "interpolation_method": interpolation,
            "extrapolation": "forbidden",
        },
        "tensor": {
            "basis": "model_cartesian",
            "form": "diagonal",
            "off_diagonal_zero": True,
            "components": normalized_components,
        },
        "time_harmonic_convention": time_convention,
    }
    body["mapping_fingerprint"] = domain_sha256_v2(OPTICAL_PROPERTY_MAPPING_SCHEMA_NAME, body)
    if supplied is not _MISSING:
        verified = _sha256(supplied, "mapping_fingerprint")
        if verified != body["mapping_fingerprint"]:
            raise ValueError("optical property mapping fingerprint is invalid")
    return body


__all__ = [
    "OPTICAL_PROPERTY_MAPPING_SCHEMA_NAME",
    "OPTICAL_PROPERTY_MAPPING_SCHEMA_VERSION",
    "normalize_optical_property_mapping",
]
