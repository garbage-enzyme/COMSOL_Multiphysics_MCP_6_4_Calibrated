"""Explicit COMSOL control/tag mapping for robust condition execution."""

from __future__ import annotations

from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .derivative_support import _bounded_json, _finite, _identifier, _object, _text

SCHEMA_NAME = "comsol_mcp.robust_condition_controls"
SCHEMA_VERSION = "1.5.0"
FORWARD_SHAPE_SCHEMA_VERSION = "1.5.0"
NATIVE_SENSITIVITY_SCHEMA_VERSION = "1.4.0"
MESH_REFERENCE_SCHEMA_VERSION = "1.3.0"
COARSE_SOLVER_MEMORY_SCHEMA_VERSION = "1.2.0"
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
_MESH_REFERENCE_FIELDS = {
    "mesh_reference_parameter",
    "mesh_reference_value",
}
_NATIVE_SENSITIVITY_FIELDS = {
    "sensitivity_parametric_sweep_tag",
    "sensitivity_feature_tag",
    "sensitivity_solver_tag",
    "sensitivity_segregated_solver_tag",
    "sensitivity_direct_solver_tags",
    "sensitivity_solution_tags",
    "sensitivity_dataset_tags",
    "derivative_solution_tag",
    "derivative_dataset_tag",
    "sensitivity_gradient_method",
    "sensitivity_solver_regeneration",
    "sensitivity_stationary_nonlinearity",
    "sensitivity_segregated_step_tags",
    "sensitivity_merged_step_tag",
    "sensitivity_removed_step_tag",
    "sensitivity_merged_linear_solver_tag",
    "sensitivity_constraint_group_policy",
}
_FORWARD_SHAPE_FIELDS = {
    "forward_shape_application_mode",
    "forward_deformation_step_tag",
    "forward_deformation_step_type",
    "forward_deformation_physics_tag",
    "forward_solved_shape_expressions",
    "forward_solved_shape_relative_tolerance",
}


def normalize_robust_condition_controls(value: object) -> dict[str, Any]:
    bounded = _bounded_json(value, "robust condition controls", 64 * 1024)
    supplied = bounded.pop("controls_fingerprint", None) if isinstance(bounded, dict) else None
    version = bounded.get("schema_version") if isinstance(bounded, dict) else None
    if version == LEGACY_SCHEMA_VERSION:
        fields = _BASE_FIELDS
    elif version == SOLVER_SELECTION_SCHEMA_VERSION:
        fields = _BASE_FIELDS | _SOLVER_SELECTION_FIELDS
    elif version == COARSE_SOLVER_MEMORY_SCHEMA_VERSION:
        fields = _BASE_FIELDS | _SOLVER_SELECTION_FIELDS | _COARSE_SOLVER_MEMORY_FIELDS
    elif version == MESH_REFERENCE_SCHEMA_VERSION:
        base_fields = _BASE_FIELDS | _SOLVER_SELECTION_FIELDS | _MESH_REFERENCE_FIELDS
        present_coarse = _COARSE_SOLVER_MEMORY_FIELDS & set(bounded)
        if present_coarse and present_coarse != _COARSE_SOLVER_MEMORY_FIELDS:
            raise ValueError("coarse solver memory controls must be supplied together")
        fields = base_fields | present_coarse
    elif version == NATIVE_SENSITIVITY_SCHEMA_VERSION:
        base_fields = (
            _BASE_FIELDS
            | _SOLVER_SELECTION_FIELDS
            | _MESH_REFERENCE_FIELDS
            | _NATIVE_SENSITIVITY_FIELDS
        )
        present_coarse = _COARSE_SOLVER_MEMORY_FIELDS & set(bounded)
        if present_coarse and present_coarse != _COARSE_SOLVER_MEMORY_FIELDS:
            raise ValueError("coarse solver memory controls must be supplied together")
        fields = base_fields | present_coarse
    elif version == SCHEMA_VERSION:
        base_fields = (
            _BASE_FIELDS
            | _SOLVER_SELECTION_FIELDS
            | _MESH_REFERENCE_FIELDS
            | _NATIVE_SENSITIVITY_FIELDS
            | _FORWARD_SHAPE_FIELDS
        )
        present_coarse = _COARSE_SOLVER_MEMORY_FIELDS & set(bounded)
        if present_coarse and present_coarse != _COARSE_SOLVER_MEMORY_FIELDS:
            raise ValueError("coarse solver memory controls must be supplied together")
        fields = base_fields | present_coarse
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
    normalized_polarization_values = {
        _identifier(key, "polarization_values key"): _text(
            item, f"polarization_values.{key}", maximum=32
        )
        for key, item in polarization_values.items()
    }
    selection: dict[str, Any] = {}
    if version in {
        SOLVER_SELECTION_SCHEMA_VERSION,
        COARSE_SOLVER_MEMORY_SCHEMA_VERSION,
        MESH_REFERENCE_SCHEMA_VERSION,
        SCHEMA_VERSION,
    }:
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
    if version == COARSE_SOLVER_MEMORY_SCHEMA_VERSION or (
        version in {MESH_REFERENCE_SCHEMA_VERSION, SCHEMA_VERSION}
        and _COARSE_SOLVER_MEMORY_FIELDS <= set(raw)
    ):
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
    mesh_reference: dict[str, Any] = {}
    if version in {MESH_REFERENCE_SCHEMA_VERSION, SCHEMA_VERSION}:
        mesh_reference = {
            "mesh_reference_parameter": _identifier(
                raw["mesh_reference_parameter"], "mesh_reference_parameter"
            ),
            "mesh_reference_value": _text(
                raw["mesh_reference_value"], "mesh_reference_value", maximum=64
            ),
        }
    native_sensitivity: dict[str, Any] = {}
    if version in {NATIVE_SENSITIVITY_SCHEMA_VERSION, SCHEMA_VERSION}:
        direct_tags = raw["sensitivity_direct_solver_tags"]
        solution_tags = raw["sensitivity_solution_tags"]
        dataset_tags = raw["sensitivity_dataset_tags"]
        for name, candidate_tags, expected_length in (
            ("sensitivity_direct_solver_tags", direct_tags, 2),
            ("sensitivity_solution_tags", solution_tags, 3),
            ("sensitivity_dataset_tags", dataset_tags, 2),
        ):
            if (
                not isinstance(candidate_tags, list)
                or len(candidate_tags) != expected_length
                or any(not isinstance(item, str) for item in candidate_tags)
            ):
                raise ValueError(f"{name} must contain exactly {expected_length} tags")
        normalized_direct = [
            _identifier(item, f"sensitivity_direct_solver_tags[{index}]")
            for index, item in enumerate(direct_tags)
        ]
        normalized_solutions = [
            _identifier(item, f"sensitivity_solution_tags[{index}]")
            for index, item in enumerate(solution_tags)
        ]
        normalized_datasets = [
            _identifier(item, f"sensitivity_dataset_tags[{index}]")
            for index, item in enumerate(dataset_tags)
        ]
        if (
            len(set(normalized_direct)) != len(normalized_direct)
            or len(set(normalized_solutions)) != len(normalized_solutions)
            or len(set(normalized_datasets)) != len(normalized_datasets)
        ):
            raise ValueError("native sensitivity tags must be unique within each tag class")
        derivative_solution = _identifier(raw["derivative_solution_tag"], "derivative_solution_tag")
        derivative_dataset = _identifier(raw["derivative_dataset_tag"], "derivative_dataset_tag")
        if (
            normalized_solutions[0] != raw["solution_tag"]
            or normalized_datasets[0] != raw["dataset_tag"]
            or derivative_solution != normalized_solutions[1]
            or derivative_dataset != normalized_datasets[1]
        ):
            raise ValueError("native sensitivity derivative identities are inconsistent")
        gradient_method = _text(
            raw["sensitivity_gradient_method"], "sensitivity_gradient_method", maximum=16
        )
        regeneration = _text(
            raw["sensitivity_solver_regeneration"],
            "sensitivity_solver_regeneration",
            maximum=32,
        )
        nonlinearity = _text(
            raw["sensitivity_stationary_nonlinearity"],
            "sensitivity_stationary_nonlinearity",
            maximum=16,
        )
        if gradient_method != "adjoint":
            raise ValueError("native sensitivity gradient method must be adjoint")
        if regeneration != "replace_existing_auto_sequence":
            raise ValueError("native sensitivity solver regeneration policy is unsupported")
        if nonlinearity != "auto":
            raise ValueError("native sensitivity stationary nonlinearity must be auto")
        native_sensitivity = {
            "sensitivity_parametric_sweep_tag": _identifier(
                raw["sensitivity_parametric_sweep_tag"], "sensitivity_parametric_sweep_tag"
            ),
            "sensitivity_feature_tag": _identifier(
                raw["sensitivity_feature_tag"], "sensitivity_feature_tag"
            ),
            "sensitivity_solver_tag": _identifier(
                raw["sensitivity_solver_tag"], "sensitivity_solver_tag"
            ),
            "sensitivity_segregated_solver_tag": _identifier(
                raw["sensitivity_segregated_solver_tag"],
                "sensitivity_segregated_solver_tag",
            ),
            "sensitivity_direct_solver_tags": normalized_direct,
            "sensitivity_solution_tags": normalized_solutions,
            "sensitivity_dataset_tags": normalized_datasets,
            "derivative_solution_tag": derivative_solution,
            "derivative_dataset_tag": derivative_dataset,
            "sensitivity_gradient_method": gradient_method,
            "sensitivity_solver_regeneration": regeneration,
            "sensitivity_stationary_nonlinearity": nonlinearity,
        }
        step_tags = raw["sensitivity_segregated_step_tags"]
        if (
            not isinstance(step_tags, list)
            or len(step_tags) != 2
            or any(not isinstance(item, str) for item in step_tags)
        ):
            raise ValueError("sensitivity_segregated_step_tags must contain exactly two tags")
        normalized_steps = [
            _identifier(item, f"sensitivity_segregated_step_tags[{index}]")
            for index, item in enumerate(step_tags)
        ]
        if len(set(normalized_steps)) != 2:
            raise ValueError("native sensitivity segregated step tags must be unique")
        merged_step = _identifier(raw["sensitivity_merged_step_tag"], "sensitivity_merged_step_tag")
        removed_step = _identifier(
            raw["sensitivity_removed_step_tag"], "sensitivity_removed_step_tag"
        )
        merged_solver = _identifier(
            raw["sensitivity_merged_linear_solver_tag"],
            "sensitivity_merged_linear_solver_tag",
        )
        group_policy = _text(
            raw["sensitivity_constraint_group_policy"],
            "sensitivity_constraint_group_policy",
            maximum=64,
        )
        if [merged_step, removed_step] != normalized_steps:
            raise ValueError(
                "native sensitivity merged and removed step identities are inconsistent"
            )
        if merged_solver != raw["linear_solver_tag"]:
            raise ValueError("native sensitivity merged group must use the selected direct solver")
        if group_policy != "merge_material_coordinates_into_wave_optics":
            raise ValueError("native sensitivity constraint group policy is unsupported")
        native_sensitivity.update(
            {
                "sensitivity_segregated_step_tags": normalized_steps,
                "sensitivity_merged_step_tag": merged_step,
                "sensitivity_removed_step_tag": removed_step,
                "sensitivity_merged_linear_solver_tag": merged_solver,
                "sensitivity_constraint_group_policy": group_policy,
            }
        )
    forward_shape: dict[str, Any] = {}
    if version == SCHEMA_VERSION:
        mode = _text(
            raw["forward_shape_application_mode"],
            "forward_shape_application_mode",
            maximum=32,
        )
        if mode != "deformation_stage":
            raise ValueError("forward shape application mode is unsupported")
        step_type = _text(
            raw["forward_deformation_step_type"],
            "forward_deformation_step_type",
            maximum=32,
        )
        if step_type != "Stationary":
            raise ValueError("forward deformation step type is unsupported")
        expressions = raw["forward_solved_shape_expressions"]
        if (
            not isinstance(expressions, list)
            or not 1 <= len(expressions) <= 4
            or any(not isinstance(item, str) or not item.strip() for item in expressions)
        ):
            raise ValueError("forward solved-shape expressions must be a bounded nonempty list")
        tolerance = _finite(
            raw["forward_solved_shape_relative_tolerance"],
            "forward_solved_shape_relative_tolerance",
            positive=True,
        )
        # The readback compares the measured deformed radius against the
        # requested radius; mesh discretization and evaluation-frame effects
        # are on the order of 1e-3 relative, so the bound allows 1e-2.
        if tolerance > 1e-2:
            raise ValueError("forward solved-shape relative tolerance is too large")
        forward_shape = {
            "forward_shape_application_mode": mode,
            "forward_deformation_step_tag": _identifier(
                raw["forward_deformation_step_tag"], "forward_deformation_step_tag"
            ),
            "forward_deformation_step_type": step_type,
            "forward_deformation_physics_tag": _identifier(
                raw["forward_deformation_physics_tag"],
                "forward_deformation_physics_tag",
            ),
            "forward_solved_shape_expressions": [
                _text(
                    item,
                    f"forward_solved_shape_expressions[{index}]",
                    maximum=256,
                )
                for index, item in enumerate(expressions)
            ],
            "forward_solved_shape_relative_tolerance": tolerance,
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
        "polarization_values": normalized_polarization_values,
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
        **mesh_reference,
        **native_sensitivity,
        **forward_shape,
    }
    body["controls_fingerprint"] = domain_sha256_v2(SCHEMA_NAME, body)
    if supplied is not None and supplied != body["controls_fingerprint"]:
        raise ValueError("robust condition controls fingerprint is invalid")
    return body


__all__ = [
    "COARSE_SOLVER_MEMORY_SCHEMA_VERSION",
    "FORWARD_SHAPE_SCHEMA_VERSION",
    "LEGACY_SCHEMA_VERSION",
    "MESH_REFERENCE_SCHEMA_VERSION",
    "NATIVE_SENSITIVITY_SCHEMA_VERSION",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "SOLVER_SELECTION_SCHEMA_VERSION",
    "normalize_robust_condition_controls",
]
