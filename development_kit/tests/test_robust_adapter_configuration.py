from __future__ import annotations

import pytest

from comsol_mcp.research.robust_adapter_configuration import (
    SCHEMA_NAME,
    normalize_robust_shape_adapter_configuration,
)
from comsol_mcp.research.robust_condition_controls import (
    normalize_robust_condition_controls,
)
from development_kit.tests.test_lin2025_pedot_backend import (
    _derivative_support,
    _fixture,
    _tree,
)
from development_kit.tests.test_robust_shape_adapter import _contracts
from development_kit.tests.test_shape_support import _policy


def _material_tensor_rows():
    row = {
        "wavelength_m": 1e-6,
        "xx_real": 2.0,
        "xx_imag": -0.1,
        "yy_real": 2.0,
        "yy_imag": -0.1,
        "zz_real": 3.0,
        "zz_imag": -0.2,
    }
    return {
        "schema_name": "comsol_mcp.robust_material_tensor_rows",
        "schema_version": "1.0.0",
        "source_sha256": "c" * 64,
        "states": [
            {"state_id": "OX", "source_sha256": "a" * 64, "rows": [row]},
            {"state_id": "MR", "source_sha256": "b" * 64, "rows": [row]},
        ],
    }


def _condition_controls():
    return {
        "schema_name": "comsol_mcp.robust_condition_controls",
        "schema_version": "1.0.0",
        "component_tag": "comp1",
        "geometry_tag": "geom1",
        "physics_tag": "ewfd",
        "periodic_structure_tag": "ps1",
        "periodic_port_tags": ["pport1", "pport2"],
        "reference_direction_tag": "rdir1",
        "wavelength_parameter": "wl",
        "elevation_parameter": "theta",
        "azimuth_parameter": "phi",
        "study_tag": "std1",
        "study_step_tag": "wl_step",
        "study_step_property": "plist",
        "study_step_array_property": None,
        "solution_tag": "sol1",
        "stationary_solver_tag": "s1",
        "linear_solver_tag": "d1",
        "out_of_core_property": "ooc",
        "out_of_core_value": "on",
        "dataset_tag": "dset1",
        "angle_property": "alpha1_inc",
        "azimuth_property": "alpha2_inc",
        "polarization_property": "Polarization",
        "linear_polarization_property": "LinearPol",
        "polarization_values": {"x_linear": "S", "y_linear": "P"},
        "observable_expression": "ewfd.Ttotal",
        "reflectance_expression": "ewfd.Rtotal",
        "transmittance_expression": "ewfd.Ttotal",
        "absorption_expression": "ewfd.Atotal",
        "evaluated_wavelength_expression": "wl",
        "solved_wavelength_expression": "c_const/ewfd.freq",
        "mesh_tag": "mesh1",
    }


def test_tagged_configuration_preserves_periodic_mim_binding():
    manifest, audit, support, policy = _contracts()
    result = normalize_robust_shape_adapter_configuration(
        {
            "schema_name": SCHEMA_NAME,
            "schema_version": "1.0.0",
            "adapter_id": "periodic_mim_patch_v1",
            "configuration": {
                "structure_adapter_manifest": manifest,
                "structure_tree_audit": audit,
            },
        },
        support,
        policy,
    )
    assert result["binding"]["adapter_id"] == "periodic_mim_patch_v1"
    assert len(result["configuration_fingerprint"]) == 64


def test_tagged_configuration_binds_lin2025_fixture_tree_and_shape_support():
    policy = _policy()
    policy["adapter_id"] = "lin2025_pedot_cylinder_v1"
    result = normalize_robust_shape_adapter_configuration(
        {
            "schema_name": SCHEMA_NAME,
            "schema_version": "1.0.0",
            "adapter_id": "lin2025_pedot_cylinder_v1",
            "configuration": {
                "fixture": _fixture(),
                "tree_readback": _tree(),
                "material_tensor_rows": _material_tensor_rows(),
                "condition_controls": _condition_controls(),
            },
        },
        _derivative_support(),
        policy,
    )
    assert result["binding"]["pedot_domain"] == 5
    assert result["configuration"]["shape_support"]["pedot_boundaries"] == [18, 19, 20, 23, 25, 27]


def test_tagged_configuration_rejects_cross_contract_adapter_drift():
    policy = _policy()
    policy["adapter_id"] = "lin2025_pedot_cylinder_v1"
    with pytest.raises(ValueError, match="identity differs"):
        normalize_robust_shape_adapter_configuration(
            {
                "schema_name": SCHEMA_NAME,
                "schema_version": "1.0.0",
                "adapter_id": "periodic_mim_patch_v1",
                "configuration": {"fixture": _fixture(), "tree_readback": _tree()},
            },
            _derivative_support(),
            policy,
        )


def test_condition_controls_support_explicit_iterative_selection_and_legacy():
    legacy = normalize_robust_condition_controls(_condition_controls())
    assert legacy["schema_version"] == "1.0.0"
    assert "selected_linear_solver_tag" not in legacy
    value = _condition_controls()
    value.update(
        schema_version="1.1.0",
        selected_linear_solver_tag="i1",
        inactive_linear_solver_tags=["d1"],
    )
    selected = normalize_robust_condition_controls(value)
    assert selected["selected_linear_solver_tag"] == "i1"
    assert selected["inactive_linear_solver_tags"] == ["d1"]
    selected.pop("controls_fingerprint")
    selected["inactive_linear_solver_tags"] = ["i1"]
    with pytest.raises(ValueError, match="cannot also be inactive"):
        normalize_robust_condition_controls(selected)


def test_condition_controls_bind_selected_coarse_solver_out_of_core_path():
    value = _condition_controls()
    value.update(
        schema_version="1.2.0",
        selected_linear_solver_tag="i1",
        inactive_linear_solver_tags=["d1"],
        coarse_solver_feature_path=["i1", "mg1", "cs", "dDef"],
        coarse_solver_out_of_core_property="ooc",
        coarse_solver_out_of_core_value="on",
    )
    result = normalize_robust_condition_controls(value)
    assert result["coarse_solver_feature_path"] == ["i1", "mg1", "cs", "dDef"]
    assert result["coarse_solver_out_of_core_value"] == "on"
    value["coarse_solver_feature_path"][0] = "d1"
    with pytest.raises(ValueError, match="must begin at the selected"):
        normalize_robust_condition_controls(value)


def test_condition_controls_bind_explicit_mesh_reference_parameter():
    value = _condition_controls()
    value.update(
        schema_version="1.3.0",
        selected_linear_solver_tag="i1",
        inactive_linear_solver_tags=["d1"],
        coarse_solver_feature_path=["i1", "mg1", "cs", "dDef"],
        coarse_solver_out_of_core_property="ooc",
        coarse_solver_out_of_core_value="on",
        mesh_reference_parameter="mesh_ref_wl",
        mesh_reference_value="1600[nm]",
    )

    result = normalize_robust_condition_controls(value)

    assert result["mesh_reference_parameter"] == "mesh_ref_wl"
    assert result["mesh_reference_value"] == "1600[nm]"
    value.pop("mesh_reference_value")
    with pytest.raises(ValueError, match="fields mismatch"):
        normalize_robust_condition_controls(value)


def test_mesh_reference_supports_direct_solver_without_coarse_path():
    value = _condition_controls()
    value.update(
        schema_version="1.3.0",
        selected_linear_solver_tag="d1",
        inactive_linear_solver_tags=["i1"],
        mesh_reference_parameter="mesh_ref_wl",
        mesh_reference_value="1600[nm]",
    )

    result = normalize_robust_condition_controls(value)

    assert result["selected_linear_solver_tag"] == "d1"
    assert "coarse_solver_feature_path" not in result
    value["coarse_solver_out_of_core_property"] = "ooc"
    with pytest.raises(ValueError, match="must be supplied together"):
        normalize_robust_condition_controls(value)


def test_condition_controls_bind_native_sensitivity_solver_identities():
    value = _condition_controls()
    value.update(
        schema_version="1.4.0",
        selected_linear_solver_tag="d1",
        inactive_linear_solver_tags=["i1"],
        mesh_reference_parameter="mesh_ref_wl",
        mesh_reference_value="1600[nm]",
        sensitivity_parametric_sweep_tag="sweep_pedot72",
        sensitivity_feature_tag="sens_pedot72",
        sensitivity_solver_tag="sn1",
        sensitivity_segregated_solver_tag="se1",
        sensitivity_direct_solver_tags=["dDef", "d1"],
        sensitivity_solution_tags=["sol1", "sol2", "sol3"],
        sensitivity_dataset_tags=["dset1", "dset2"],
        derivative_solution_tag="sol2",
        derivative_dataset_tag="dset2",
        sensitivity_gradient_method="adjoint",
        sensitivity_solver_regeneration="replace_existing_auto_sequence",
        sensitivity_stationary_nonlinearity="auto",
        sensitivity_segregated_step_tags=["ss1", "ss2"],
        sensitivity_merged_step_tag="ss1",
        sensitivity_removed_step_tag="ss2",
        sensitivity_merged_linear_solver_tag="d1",
        sensitivity_constraint_group_policy="merge_material_coordinates_into_wave_optics",
    )

    result = normalize_robust_condition_controls(value)

    assert result["sensitivity_solver_tag"] == "sn1"
    assert result["sensitivity_segregated_solver_tag"] == "se1"
    assert result["sensitivity_direct_solver_tags"] == ["dDef", "d1"]
    assert result["polarization_values"] == {"x_linear": "S", "y_linear": "P"}
    assert result["derivative_solution_tag"] == "sol2"
    assert result["derivative_dataset_tag"] == "dset2"
    assert result["sensitivity_segregated_step_tags"] == ["ss1", "ss2"]
    assert result["sensitivity_constraint_group_policy"] == (
        "merge_material_coordinates_into_wave_optics"
    )
    value["sensitivity_stationary_nonlinearity"] = "off"
    with pytest.raises(ValueError, match="must be auto"):
        normalize_robust_condition_controls(value)


def _forward_shape_controls():
    value = _condition_controls()
    value.update(
        schema_version="1.5.0",
        selected_linear_solver_tag="d1",
        inactive_linear_solver_tags=["i1"],
        mesh_reference_parameter="mesh_ref_wl",
        mesh_reference_value="1600[nm]",
        sensitivity_parametric_sweep_tag="sweep_pedot72",
        sensitivity_feature_tag="sens_pedot72",
        sensitivity_solver_tag="sn1",
        sensitivity_segregated_solver_tag="se1",
        sensitivity_direct_solver_tags=["dDef", "d1"],
        sensitivity_solution_tags=["sol1", "sol2", "sol3"],
        sensitivity_dataset_tags=["dset1", "dset2"],
        derivative_solution_tag="sol2",
        derivative_dataset_tag="dset2",
        sensitivity_gradient_method="adjoint",
        sensitivity_solver_regeneration="replace_existing_auto_sequence",
        sensitivity_stationary_nonlinearity="auto",
        sensitivity_segregated_step_tags=["ss1", "ss2"],
        sensitivity_merged_step_tag="ss1",
        sensitivity_removed_step_tag="ss2",
        sensitivity_merged_linear_solver_tag="d1",
        sensitivity_constraint_group_policy="merge_material_coordinates_into_wave_optics",
        forward_shape_application_mode="deformation_stage",
        forward_deformation_step_tag="dg_step",
        forward_deformation_step_type="Stationary",
        forward_deformation_physics_tag="dg_pedot72",
        forward_solved_shape_expressions=["comp1.material.disp", "comp1.material.disp"],
        forward_solved_shape_relative_tolerance=1e-6,
    )
    return value


def test_condition_controls_1_5_0_bind_forward_shape_application():
    result = normalize_robust_condition_controls(_forward_shape_controls())

    assert result["schema_version"] == "1.5.0"
    assert result["forward_shape_application_mode"] == "deformation_stage"
    assert result["forward_deformation_step_tag"] == "dg_step"
    assert result["forward_deformation_step_type"] == "Stationary"
    assert result["forward_deformation_physics_tag"] == "dg_pedot72"
    assert result["forward_solved_shape_expressions"] == [
        "comp1.material.disp",
        "comp1.material.disp",
    ]
    assert result["forward_solved_shape_relative_tolerance"] == 1e-6
    assert result["sensitivity_solver_tag"] == "sn1"
    assert "forward_shape_application_mode" not in normalize_robust_condition_controls(
        _condition_controls()
    )


def test_condition_controls_1_5_0_reject_unsupported_forward_shape_mode():
    value = _forward_shape_controls()
    value["forward_shape_application_mode"] = "remesh"
    with pytest.raises(ValueError, match="forward shape application mode is unsupported"):
        normalize_robust_condition_controls(value)


def test_condition_controls_1_5_0_reject_unsupported_step_type_or_missing_fields():
    value = _forward_shape_controls()
    value["forward_deformation_step_type"] = "TimeDependent"
    with pytest.raises(ValueError, match="forward deformation step type"):
        normalize_robust_condition_controls(value)
    value = _forward_shape_controls()
    value.pop("forward_deformation_step_tag")
    with pytest.raises(ValueError, match="fields mismatch"):
        normalize_robust_condition_controls(value)


def test_condition_controls_1_5_0_reject_invalid_solved_shape_policy():
    value = _forward_shape_controls()
    value["forward_solved_shape_expressions"] = []
    with pytest.raises(ValueError, match="bounded nonempty list"):
        normalize_robust_condition_controls(value)
    value = _forward_shape_controls()
    value["forward_solved_shape_relative_tolerance"] = 0.01
    with pytest.raises(ValueError, match="too large"):
        normalize_robust_condition_controls(value)


def test_condition_controls_1_4_0_remain_supported_without_forward_shape():
    value = _condition_controls()
    value.update(
        schema_version="1.4.0",
        selected_linear_solver_tag="d1",
        inactive_linear_solver_tags=["i1"],
        mesh_reference_parameter="mesh_ref_wl",
        mesh_reference_value="1600[nm]",
        sensitivity_parametric_sweep_tag="sweep_pedot72",
        sensitivity_feature_tag="sens_pedot72",
        sensitivity_solver_tag="sn1",
        sensitivity_segregated_solver_tag="se1",
        sensitivity_direct_solver_tags=["dDef", "d1"],
        sensitivity_solution_tags=["sol1", "sol2", "sol3"],
        sensitivity_dataset_tags=["dset1", "dset2"],
        derivative_solution_tag="sol2",
        derivative_dataset_tag="dset2",
        sensitivity_gradient_method="adjoint",
        sensitivity_solver_regeneration="replace_existing_auto_sequence",
        sensitivity_stationary_nonlinearity="auto",
        sensitivity_segregated_step_tags=["ss1", "ss2"],
        sensitivity_merged_step_tag="ss1",
        sensitivity_removed_step_tag="ss2",
        sensitivity_merged_linear_solver_tag="d1",
        sensitivity_constraint_group_policy="merge_material_coordinates_into_wave_optics",
    )
    result = normalize_robust_condition_controls(value)
    assert result["schema_version"] == "1.4.0"
    assert "forward_shape_application_mode" not in result
    assert result["sensitivity_solver_tag"] == "sn1"
