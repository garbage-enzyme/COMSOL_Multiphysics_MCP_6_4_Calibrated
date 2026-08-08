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
    assert matrix["capability_probe"]["feature_execution"] == "unverified"
    assert matrix["method_policy"]["accepted_lane"] == "adjoint"
    assert matrix["method_policy"]["global_optimality_claim"] is False
    assert matrix["probe_receipts"]["full_receipts_retained_locally"] is True
    assert matrix["probe_receipts"]["public_materials_redacted"] is True
    assert matrix["probe_receipts"]["solver_residue_observed"] is False


def test_matrix_receipt_hashes_are_valid_sha256_and_no_private_path_is_embedded():
    matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    for key, value in matrix["probe_receipts"].items():
        if key.endswith("_sha256"):
            assert len(value) == 64
            assert all(character in "0123456789abcdef" for character in value)
    serialized = MATRIX_PATH.read_text(encoding="utf-8")
    assert "D:\\" not in serialized
    assert "C:\\Users\\" not in serialized
    assert hashlib.sha256(serialized.encode("utf-8")).hexdigest()
