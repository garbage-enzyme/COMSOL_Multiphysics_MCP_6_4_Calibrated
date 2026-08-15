"""Explicit COMSOL control/tag mapping for robust condition execution."""

from __future__ import annotations

from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .derivative_support import _bounded_json, _identifier, _object, _text

SCHEMA_NAME = "comsol_mcp.robust_condition_controls"
SCHEMA_VERSION = "1.2.0"
SOLVER_SELECTION_SCHEMA_VERSION = "1.1.0"
LEGACY_SCHEMA_VERSION = "1.0.0"

_BASE_FIELDS = {
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
    "study_step_property",
    "study_step_array_property",
    "solution_tag",
    "stationary_solver_tag",
    "linear_solver_tag",
    "out_of_core_property",
    "out_of_core_value",
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
}
_SOLVER_SELECTION_FIELDS = {
    "selected_linear_solver_tag",
    "inactive_linear_solver_tags",
}
_COARSE_SOLVER_MEMORY_FIELDS = {
    "coarse_solver_feature_path",
    "coarse_solver_out_of_core_property",
    "coarse_solver_out_of_core_value",
}


def normalize_robust_condition_controls(value: object) -> dict[str, Any]:
    bounded = _bounded_json(value, "robust condition controls", 64 * 1024)
    supplied = bounded.pop("controls_fingerprint", None) if isinstance(bounded, dict) else None
    version = bounded.get("schema_version") if isinstance(bounded, dict) else None
    if version == LEGACY_SCHEMA_VERSION:
        fields = _BASE_FIELDS
    elif version == SOLVER_SELECTION_SCHEMA_VERSION:
        fields = _BASE_FIELDS | _SOLVER_SELECTION_FIELDS
    elif version == SCHEMA_VERSION:
        fields = _BASE_FIELDS | _SOLVER_SELECTION_FIELDS | _COARSE_SOLVER_MEMORY_FIELDS
    else:
        raise ValueError("robust condition controls schema is unsupported")
    raw = _object(
        bounded,
        fields,
        "robust condition controls",
    )
    if raw["schema_name"] != SCHEMA_NAME:
        raise ValueError("robust condition controls schema is unsupported")
    ports = raw["periodic_port_tags"]
    if (
        not isinstance(ports, list)
        or len(ports) != 2
        or any(not isinstance(item, str) for item in ports)
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
    selection: dict[str, Any] = {}
    if version in {SOLVER_SELECTION_SCHEMA_VERSION, SCHEMA_VERSION}:
        selected = _identifier(raw["selected_linear_solver_tag"], "selected_linear_solver_tag")
        inactive = raw["inactive_linear_solver_tags"]
        if (
            not isinstance(inactive, list)
            or not 1 <= len(inactive) <= 8
            or any(not isinstance(item, str) for item in inactive)
        ):
            raise ValueError("inactive_linear_solver_tags must be a bounded nonempty list")
        normalized_inactive = [
            _identifier(item, f"inactive_linear_solver_tags[{index}]")
            for index, item in enumerate(inactive)
        ]
        if len(set(normalized_inactive)) != len(normalized_inactive):
            raise ValueError("inactive_linear_solver_tags must be unique")
        if selected in normalized_inactive:
            raise ValueError("selected linear solver cannot also be inactive")
        selection = {
            "selected_linear_solver_tag": selected,
            "inactive_linear_solver_tags": normalized_inactive,
        }
    coarse_solver_memory: dict[str, Any] = {}
    if version == SCHEMA_VERSION:
        feature_path = raw["coarse_solver_feature_path"]
        if (
            not isinstance(feature_path, list)
            or not 2 <= len(feature_path) <= 8
            or any(not isinstance(item, str) for item in feature_path)
        ):
            raise ValueError("coarse_solver_feature_path must be a bounded feature path")
        normalized_path = [
            _identifier(item, f"coarse_solver_feature_path[{index}]")
            for index, item in enumerate(feature_path)
        ]
        if normalized_path[0] != selection["selected_linear_solver_tag"]:
            raise ValueError("coarse solver path must begin at the selected linear solver")
        coarse_value = _text(
            raw["coarse_solver_out_of_core_value"],
            "coarse_solver_out_of_core_value",
            maximum=16,
        )
        if coarse_value not in {"auto", "off", "on"}:
            raise ValueError("coarse solver out-of-core value is unsupported")
        coarse_solver_memory = {
            "coarse_solver_feature_path": normalized_path,
            "coarse_solver_out_of_core_property": _identifier(
                raw["coarse_solver_out_of_core_property"],
                "coarse_solver_out_of_core_property",
            ),
            "coarse_solver_out_of_core_value": coarse_value,
        }
    body = {
        "schema_name": SCHEMA_NAME,
        "schema_version": version,
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
        "elevation_parameter": _identifier(raw["elevation_parameter"], "elevation_parameter"),
        "azimuth_parameter": _identifier(raw["azimuth_parameter"], "azimuth_parameter"),
        "study_tag": _identifier(raw["study_tag"], "study_tag"),
        "study_step_tag": _identifier(raw["study_step_tag"], "study_step_tag"),
        "study_step_property": _identifier(raw["study_step_property"], "study_step_property"),
        "study_step_array_property": (
            None
            if raw["study_step_array_property"] is None
            else _identifier(raw["study_step_array_property"], "study_step_array_property")
        ),
        "solution_tag": _identifier(raw["solution_tag"], "solution_tag"),
        "stationary_solver_tag": _identifier(raw["stationary_solver_tag"], "stationary_solver_tag"),
        "linear_solver_tag": _identifier(raw["linear_solver_tag"], "linear_solver_tag"),
        "out_of_core_property": _identifier(raw["out_of_core_property"], "out_of_core_property"),
        "out_of_core_value": _text(raw["out_of_core_value"], "out_of_core_value", maximum=16),
        "dataset_tag": _identifier(raw["dataset_tag"], "dataset_tag"),
        "angle_property": _identifier(raw["angle_property"], "angle_property"),
        "azimuth_property": _identifier(raw["azimuth_property"], "azimuth_property"),
        "polarization_property": _identifier(raw["polarization_property"], "polarization_property"),
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
        **selection,
        **coarse_solver_memory,
    }
    body["controls_fingerprint"] = domain_sha256_v2(SCHEMA_NAME, body)
    if supplied is not None and supplied != body["controls_fingerprint"]:
        raise ValueError("robust condition controls fingerprint is invalid")
    return body


__all__ = [
    "LEGACY_SCHEMA_VERSION",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "SOLVER_SELECTION_SCHEMA_VERSION",
    "normalize_robust_condition_controls",
]
