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
    assert result["configuration"]["shape_support"]["pedot_boundaries"] == [
        18, 19, 20, 23, 25, 27
    ]


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
