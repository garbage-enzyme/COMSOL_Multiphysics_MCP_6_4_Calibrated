"""Solver-free validation of the redacted alpha7.1 capability matrix."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parents[2]
MATRIX_PATH = ROOT / "development_kit" / "release" / "native_gradient_support_matrix.json"


def test_native_gradient_matrix_is_redacted_and_binds_the_live_probe_boundary():
    matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    assert matrix["release_boundary"] == "alpha7.1"
    assert matrix["runtime"] == {
        "comsol_version": "6.4",
        "comsol_build": "6.4.0.293",
        "mph_version": "1.3.1",
        "required_physics": "Wave Optics",
        "required_entitlement": "Optimization",
    }
    assert matrix["capability_probe"]["sensitivity"]["gradient_method_allowed"] == [
        "adjoint",
        "forward",
    ]
    assert matrix["capability_probe"]["module_inventory_is_diagnostic_only"] is True
    assert matrix["capability_probe"]["feature_execution"] == (
        "full_vector_native_adjoint_verified"
    )
    assert matrix["capability_probe"]["wiring_probe"]["status"] == "licensed_structural_readback"
    assert matrix["capability_probe"]["wiring_probe"]["solve_executed"] is False
    assert matrix["capability_probe"]["trusted_adapter_probe"]["status"] == (
        "superseded_direct_geometry_binding"
    )
    assert matrix["capability_probe"]["trusted_adapter_probe"]["source_unchanged"] is True
    assert matrix["capability_probe"]["trusted_adapter_probe"]["native_solve_executed"] is False
    deformed = matrix["capability_probe"]["deformed_geometry_formal_gate"]
    assert deformed["status"] == "licensed_structural_proven_with_differentiable_objective"
    assert deformed["mapping"] == "fixed_topology_deformed_geometry"
    assert deformed["free_domains"] == [1, 2, 3]
    assert deformed["patch_boundaries"] == [10, 11, 12, 13, 14, 15]
    assert deformed["variable_value_types"] == ["real", "real"]
    assert deformed["objective_expression_readback"] == "comp1.ewfd.Torder_0_0"
    assert deformed["source_unchanged"] is True
    assert deformed["native_solve_executed"] is False
    assert deformed["cleanup_verified"] is True
    execution = matrix["capability_probe"]["native_execution_diagnostics"]
    assert execution["superseded_objective"]["expression"] == "ewfd.Ttotal"
    assert execution["accepted_objective"]["expression"] == "comp1.ewfd.Torder_0_0"
    assert execution["generated_identity"]["derivative_dataset"] == "dset2"
    assert execution["generated_identity"]["derivative_dataset_solution"] == "sol2"
    assert execution["derivative"]["accepted_expression"] == (
        "real(fsens(patch_length_x))"
    )
    assert execution["derivative"]["relative_error"] < 0.01
    assert execution["source_unchanged"] is True
    assert execution["cleanup_verified"] is True
    formal = matrix["capability_probe"]["native_gradient_formal_gate"]
    assert formal["status"] == "full_vector_native_and_fd_verified_pending_directional"
    assert formal["variable_order"] == ["patch_length_x", "patch_length_y"]
    assert formal["native_gradient"][0] < 0.0 < formal["native_gradient"][1]
    assert formal["generated_solutions"] == ["sol1", "sol2", "sol3"]
    assert formal["derivative_dataset"] == {"dataset": "dset2", "solution": "sol2"}
    assert formal["source_unchanged"] is True
    assert formal["cleanup_verified"] is True
    assert formal["solver_residue_observed"] is False
    finite_difference = formal["finite_difference"]
    assert finite_difference["relative_steps"] == [0.01, 0.003, 0.001]
    assert finite_difference["completed_points"] == 12
    assert finite_difference["selected_relative_errors"][0] < 0.002
    assert 0.08 < finite_difference["selected_relative_errors"][1] < 0.1
    assert finite_difference["cosine_similarity"] > 0.999
    assert finite_difference["all_signs_agree"] is True
    assert finite_difference["source_unchanged"] is True
    assert finite_difference["cleanup_verified"] is True
    assert finite_difference["solver_residue_observed"] is False
    assert matrix["method_policy"]["accepted_lane"] == "adjoint"
    assert matrix["method_policy"]["global_optimality_claim"] is False
    assert matrix["probe_receipts"]["full_receipts_retained_locally"] is True
    assert matrix["probe_receipts"]["public_materials_redacted"] is True
    assert matrix["probe_receipts"]["solver_residue_observed"] is False


def test_matrix_receipt_hashes_are_valid_sha256_and_no_private_path_is_embedded():
    matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    def assert_hashes(value):
        if isinstance(value, dict):
            for key, nested in value.items():
                if key.endswith("_sha256"):
                    assert len(nested) == 64
                    assert all(character in "0123456789abcdef" for character in nested)
                else:
                    assert_hashes(nested)
        elif isinstance(value, list):
            for nested in value:
                assert_hashes(nested)

    assert_hashes(matrix)
    serialized = MATRIX_PATH.read_text(encoding="utf-8")
    assert "D:\\" not in serialized
    assert "C:\\Users\\" not in serialized
    assert hashlib.sha256(serialized.encode("utf-8")).hexdigest()
