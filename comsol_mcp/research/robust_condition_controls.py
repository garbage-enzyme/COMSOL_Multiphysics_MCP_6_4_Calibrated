"""Explicit COMSOL control/tag mapping for robust condition execution."""

from __future__ import annotations

from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .derivative_support import _bounded_json, _identifier, _object, _text

SCHEMA_NAME = "comsol_mcp.robust_condition_controls"
SCHEMA_VERSION = "1.0.0"


def normalize_robust_condition_controls(value: object) -> dict[str, Any]:
    bounded = _bounded_json(value, "robust condition controls", 64 * 1024)
    supplied = bounded.pop("controls_fingerprint", None) if isinstance(bounded, dict) else None
    raw = _object(
        bounded,
        {
            "schema_name",
            "schema_version",
            "component_tag",
            "geometry_tag",
            "physics_tag",
            "periodic_structure_tag",
            "periodic_port_tags",
            "reference_direction_tag",
            "wavelength_parameter",
            "elevation_parameter",
            "azimuth_parameter",
            "study_tag",
            "study_step_tag",
            "solution_tag",
            "dataset_tag",
            "angle_property",
            "azimuth_property",
            "polarization_property",
            "linear_polarization_property",
            "polarization_values",
            "observable_expression",
            "reflectance_expression",
            "transmittance_expression",
            "absorption_expression",
            "evaluated_wavelength_expression",
            "solved_wavelength_expression",
            "mesh_tag",
        },
        "robust condition controls",
    )
    if raw["schema_name"] != SCHEMA_NAME or raw["schema_version"] != SCHEMA_VERSION:
        raise ValueError("robust condition controls schema is unsupported")
    ports = raw["periodic_port_tags"]
    if not isinstance(ports, list) or len(ports) != 2 or any(
        not isinstance(item, str) for item in ports
    ):
        raise ValueError("robust condition controls require exactly two periodic ports")
    polarization_values = raw["polarization_values"]
    if not isinstance(polarization_values, dict) or set(polarization_values) < {
        "x_linear",
        "y_linear",
    }:
        raise ValueError("robust condition controls require x_linear and y_linear mappings")
    values = {
        _identifier(key, "polarization_values key"): _text(
            item, f"polarization_values.{key}", maximum=32
        )
        for key, item in polarization_values.items()
    }
    body = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "component_tag": _identifier(raw["component_tag"], "component_tag"),
        "geometry_tag": _identifier(raw["geometry_tag"], "geometry_tag"),
        "physics_tag": _identifier(raw["physics_tag"], "physics_tag"),
        "periodic_structure_tag": _identifier(
            raw["periodic_structure_tag"], "periodic_structure_tag"
        ),
        "periodic_port_tags": [
            _identifier(item, f"periodic_port_tags[{index}]") for index, item in enumerate(ports)
        ],
        "reference_direction_tag": _identifier(
            raw["reference_direction_tag"], "reference_direction_tag"
        ),
        "wavelength_parameter": _identifier(raw["wavelength_parameter"], "wavelength_parameter"),
        "elevation_parameter": _identifier(
            raw["elevation_parameter"], "elevation_parameter"
        ),
        "azimuth_parameter": _identifier(raw["azimuth_parameter"], "azimuth_parameter"),
        "study_tag": _identifier(raw["study_tag"], "study_tag"),
        "study_step_tag": _identifier(raw["study_step_tag"], "study_step_tag"),
        "solution_tag": _identifier(raw["solution_tag"], "solution_tag"),
        "dataset_tag": _identifier(raw["dataset_tag"], "dataset_tag"),
        "angle_property": _identifier(raw["angle_property"], "angle_property"),
        "azimuth_property": _identifier(raw["azimuth_property"], "azimuth_property"),
        "polarization_property": _identifier(
            raw["polarization_property"], "polarization_property"
        ),
        "linear_polarization_property": _identifier(
            raw["linear_polarization_property"], "linear_polarization_property"
        ),
        "polarization_values": values,
        "observable_expression": _text(
            raw["observable_expression"], "observable_expression", maximum=256
        ),
        "reflectance_expression": _text(
            raw["reflectance_expression"], "reflectance_expression", maximum=256
        ),
        "transmittance_expression": _text(
            raw["transmittance_expression"], "transmittance_expression", maximum=256
        ),
        "absorption_expression": _text(
            raw["absorption_expression"], "absorption_expression", maximum=256
        ),
        "evaluated_wavelength_expression": _text(
            raw["evaluated_wavelength_expression"],
            "evaluated_wavelength_expression",
            maximum=256,
        ),
        "solved_wavelength_expression": _text(
            raw["solved_wavelength_expression"], "solved_wavelength_expression", maximum=256
        ),
        "mesh_tag": _identifier(raw["mesh_tag"], "mesh_tag"),
    }
    body["controls_fingerprint"] = domain_sha256_v2(SCHEMA_NAME, body)
    if supplied is not None and supplied != body["controls_fingerprint"]:
        raise ValueError("robust condition controls fingerprint is invalid")
    return body


__all__ = ["SCHEMA_NAME", "SCHEMA_VERSION", "normalize_robust_condition_controls"]
