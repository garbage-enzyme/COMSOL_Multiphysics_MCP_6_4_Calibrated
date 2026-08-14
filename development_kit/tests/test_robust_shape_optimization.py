"""Solver-free robust shape manifest and submission tests."""

from __future__ import annotations

import hashlib
import json

import pytest

from comsol_mcp.jobs.robust_shape_optimization import expand_robust_shape_manifest
from comsol_mcp.research.robust_conditions import normalize_optimization_condition_table
from comsol_mcp.research.shape_support import normalize_shape_support_policy
from development_kit.tests.test_derivative_support import _support, _variable
from development_kit.tests.test_gradient_contracts import _optimizer
from development_kit.tests.test_lin2025_pedot_backend import (
    _derivative_support as _lin_support,
)
from development_kit.tests.test_lin2025_pedot_backend import (
    _fixture as _lin_fixture,
)
from development_kit.tests.test_lin2025_pedot_backend import (
    _tree as _lin_tree,
)
from development_kit.tests.test_research_adapters import (
    _audit as _structure_audit,
)
from development_kit.tests.test_research_adapters import (
    _manifest as _structure_manifest,
)
from development_kit.tests.test_robust_conditions import _table
from development_kit.tests.test_robust_finalist_validation import _policy as _finalist_policy
from development_kit.tests.test_robust_gradient_acceptance import _policy as _gradient_policy
from development_kit.tests.test_robust_objectives import _configuration
from development_kit.tests.test_robust_optimizer_policy import _policy as _optimizer_policy
from development_kit.tests.test_robust_startup_admission import _policy as _startup_policy
from development_kit.tests.test_shape_support import _policy as _shape_policy


def _write_manifest(tmp_path, *, selected: str = "gcmma", synthetic: bool = True):
    source = tmp_path / "source.mph"
    source.write_bytes(b"synthetic robust source")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    support = _support()
    support["source_identity"] = source_hash
    support["variables"] = [_variable(0), _variable(1, "patch_length_y")]
    support["variables"][0]["mapping"].update(
        feature_tag="patch_a71",
        feature_type="PrescribedMeshDisplacement",
        property_name="dx",
        readback_expression="patch_length_x",
    )
    support["variables"][1].update(
        baseline=800.0,
        lower=600.0,
        upper=1000.0,
        scale=800.0,
    )
    support["variables"][1]["mapping"].update(
        feature_tag="patch_a71",
        feature_type="PrescribedMeshDisplacement",
        property_name="dx",
        readback_expression="patch_length_y",
    )
    structure_manifest = _structure_manifest()
    structure_manifest["source_identity"]["source_sha256"] = source_hash
    baseline_x = 856e-9
    baseline_y = 800e-9
    structure_manifest["mutable_dimensions"][0].update(
        baseline=baseline_x, lower=0.75 * baseline_x, upper=1.25 * baseline_x
    )
    structure_manifest["mutable_dimensions"][1].update(
        baseline=baseline_y, lower=0.75 * baseline_y, upper=1.25 * baseline_y
    )
    optimizer = _optimizer()
    optimizer["budget"]["cores"] = 14
    optimizer["method"] = selected
    optimizer_policy = _optimizer_policy(selected)
    if selected == "mma":
        optimizer_policy["method_evidence"][1] = {
            "method": "mma",
            "support_state": "validated" if not synthetic else "structural_only",
            "evidence_sha256": "b" * 64,
        }
    condition_table = _table()
    shape_policy = _shape_policy()
    finalist_policy = _finalist_policy(
        condition_table_fingerprint=normalize_optimization_condition_table(condition_table)[
            "condition_table_fingerprint"
        ],
        shape_policy_fingerprint=normalize_shape_support_policy(shape_policy)["policy_fingerprint"],
    )
    if synthetic:
        finalist_policy["external_fidelity"] = {
            "mode": "not_requested",
            "primary_backend": None,
            "fallback_mode": "not_requested",
            "automatic_fallback": False,
        }
    manifest_body = {
        "schema_name": "comsol_mcp.robust_shape_optimization_manifest",
        "schema_version": "1.0.0",
        "source_model_path": str(source),
        "source_model_sha256": source_hash,
        "structure_adapter_manifest": structure_manifest,
        "structure_tree_audit": _structure_audit(structure_manifest),
        "support": support,
        "condition_table": condition_table,
        "objective": _configuration(),
        "shape_policy": shape_policy,
        "finalist_validation_policy": finalist_policy,
        "gradient_policy": _gradient_policy(),
        "optimizer_policy": optimizer_policy,
        "native_optimizer": optimizer,
        "startup_admission": _startup_policy(),
        "initial_values": [856.0, 800.0],
        "synthetic_mode": synthetic,
    }
    manifest = tmp_path / "manifest.json"
    payload = json.dumps(manifest_body, sort_keys=True, separators=(",", ":")).encode()
    manifest.write_bytes(payload)
    envelope = {
        "job_type": "robust_shape_optimization",
        "submission_manifest_path": str(manifest),
        "submission_manifest_sha256": hashlib.sha256(payload).hexdigest(),
        "cores": 14,
        "version": "6.4",
        "resource_policy": {"max_mesh_elements": 300_000},
        "comsol_temporary_directory": str(tmp_path),
    }
    return envelope, source, manifest


def test_manifest_binds_every_robust_contract_and_uses_no_host_defaults(ascii_tmp_path):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    spec = expand_robust_shape_manifest(envelope)
    assert spec["job_type"] == "robust_shape_optimization"
    assert spec["condition_table"]["completeness"]["dimension_cardinalities"] == {
        "wavelength": 3,
        "incidence_elevation": 2,
        "incidence_azimuth": 1,
        "polarization_basis": 2,
        "material_state": 2,
    }
    assert spec["resource_policy"]["host_defaults_applied"] is False
    assert spec["shape_policy"]["mesh_admission"]["max_elements_per_model"] == 300_000
    assert spec["finalist_validation_policy"]["off_design"] == {
        "mode": "required",
        "validation_only": True,
        "include_in_optimizer": False,
        "wavelength_relative_offsets": [-0.01, 0.01],
        "angle_offsets_deg": [-2.0, 2.0],
    }
    assert spec["optimizer_policy"]["selected_method"] == "gcmma"
    assert spec["startup_admission"]["check_frequency"] == "startup_only"
    assert spec["adapter_binding"]["source_sha256"] == spec["source_model_sha256"]
    assert [item["variable_id"] for item in spec["adapter_binding"]["variables"]] == [
        "patch_length_x",
        "patch_length_y",
    ]


def test_manifest_v11_accepts_tagged_lin2025_adapter(ascii_tmp_path):
    envelope, source, manifest = _write_manifest(ascii_tmp_path)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    raw = json.loads(manifest.read_text())
    support = _lin_support()
    support["source_identity"] = source_hash
    fixture = _lin_fixture()
    fixture["source_identity"]["source_sha256"] = source_hash
    tree = _lin_tree()
    tree["source_sha256"] = source_hash
    raw["schema_version"] = "1.1.0"
    raw.pop("structure_adapter_manifest")
    raw.pop("structure_tree_audit")
    raw["support"] = support
    wavelength_map = dict(
        zip(
            sorted({row["wavelength_m"] for row in raw["condition_table"]["conditions"]}),
            (8e-7, 1e-6, 1.2e-6),
            strict=True,
        )
    )
    for row in raw["condition_table"]["conditions"]:
        row["wavelength_m"] = wavelength_map[row["wavelength_m"]]
    raw["finalist_validation_policy"]["condition_table_fingerprint"] = (
        normalize_optimization_condition_table(raw["condition_table"])[
            "condition_table_fingerprint"
        ]
    )
    raw["shape_policy"]["adapter_id"] = "lin2025_pedot_cylinder_v1"
    raw["finalist_validation_policy"]["shape_policy_fingerprint"] = (
        normalize_shape_support_policy(raw["shape_policy"])["policy_fingerprint"]
    )
    raw["adapter_configuration"] = {
        "schema_name": "comsol_mcp.robust_shape_adapter_configuration",
        "schema_version": "1.0.0",
        "adapter_id": "lin2025_pedot_cylinder_v1",
        "configuration": {
            "fixture": fixture,
            "tree_readback": tree,
            "material_tensor_rows": {
                "schema_name": "comsol_mcp.robust_material_tensor_rows",
                "schema_version": "1.0.0",
                "source_sha256": "c" * 64,
                "states": [
                    {
                        "state_id": state,
                        "source_sha256": raw["condition_table"]["material_states"][index][
                            "optical_property_source_sha256"
                        ],
                        "rows": [
                            {
                                "wavelength_m": 8e-7,
                                "xx_real": 2.0,
                                "xx_imag": -0.1,
                                "yy_real": 2.0,
                                "yy_imag": -0.1,
                                "zz_real": 3.0,
                                "zz_imag": -0.2,
                            },
                            {
                                "wavelength_m": 1.0e-6,
                                "xx_real": 2.1,
                                "xx_imag": -0.1,
                                "yy_real": 2.1,
                                "yy_imag": -0.1,
                                "zz_real": 3.1,
                                "zz_imag": -0.2,
                            },
                            {
                                "wavelength_m": 1.2e-6,
                                "xx_real": 2.2,
                                "xx_imag": -0.1,
                                "yy_real": 2.2,
                                "yy_imag": -0.1,
                                "zz_real": 3.2,
                                "zz_imag": -0.2,
                            },
                        ],
                    }
                    for index, state in enumerate(("OX", "MR"))
                ],
            },
            "condition_controls": {
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
                "study_step_array_property": "plistarr",
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
            },
        },
    }
    raw["initial_values"] = [260.0, 260.0]
    payload = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    manifest.write_bytes(payload)
    envelope["submission_manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    spec = expand_robust_shape_manifest(envelope)
    assert spec["schema_version"] == "1.1.0"
    assert spec["adapter_binding"]["pedot_domain"] == 5
    assert spec["material_tensor_binding"]["active_wavelengths_m"]["OX"] == pytest.approx(
        [8e-7, 1e-6, 1.2e-6]
    )
    assert spec["material_tensor_binding"]["active_wavelengths_m"]["MR"] == pytest.approx(
        [8e-7, 1e-6, 1.2e-6]
    )


def test_manifest_rejects_source_or_manifest_mutation(ascii_tmp_path):
    envelope, source, manifest = _write_manifest(ascii_tmp_path)
    source.write_bytes(b"changed")
    with pytest.raises(ValueError, match="source SHA-256"):
        expand_robust_shape_manifest(envelope)
    envelope, _, manifest = _write_manifest(ascii_tmp_path)
    manifest.write_bytes(manifest.read_bytes() + b" ")
    with pytest.raises(ValueError, match="manifest SHA-256"):
        expand_robust_shape_manifest(envelope)


def test_manifest_rejects_mesh_core_or_method_identity_drift(ascii_tmp_path):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    envelope["resource_policy"] = {"max_mesh_elements": 400_000}
    with pytest.raises(ValueError, match="mesh cap"):
        expand_robust_shape_manifest(envelope)
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    envelope["cores"] = 13
    with pytest.raises(ValueError, match="budget cores"):
        expand_robust_shape_manifest(envelope)
    envelope, _, manifest = _write_manifest(ascii_tmp_path)
    raw = json.loads(manifest.read_text())
    raw["optimizer_policy"]["selected_method"] = "mma"
    payload = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    manifest.write_bytes(payload)
    envelope["submission_manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    with pytest.raises(ValueError, match="method differs"):
        expand_robust_shape_manifest(envelope)


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("condition", "condition table identity"),
        ("shape", "shape policy identity"),
        ("cap", "mesh cap differs"),
        ("quality", "mesh quality identity"),
    ],
)
def test_manifest_rejects_finalist_policy_identity_drift(ascii_tmp_path, field, message):
    envelope, _, manifest = _write_manifest(ascii_tmp_path)
    raw = json.loads(manifest.read_text())
    policy = raw["finalist_validation_policy"]
    if field == "condition":
        policy["condition_table_fingerprint"] = "0" * 64
    elif field == "shape":
        policy["shape_policy_fingerprint"] = "0" * 64
    elif field == "cap":
        policy["mesh_convergence"]["max_elements_per_model"] = 299_999
    else:
        policy["mesh_convergence"]["minimum_element_quality"] = 0.2
    payload = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    manifest.write_bytes(payload)
    envelope["submission_manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    with pytest.raises(ValueError, match=message):
        expand_robust_shape_manifest(envelope)


def test_real_mma_requires_validated_method_evidence(ascii_tmp_path):
    envelope, _, manifest = _write_manifest(ascii_tmp_path, selected="mma", synthetic=True)
    raw = json.loads(manifest.read_text())
    raw["synthetic_mode"] = False
    payload = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    manifest.write_bytes(payload)
    envelope["submission_manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    with pytest.raises(ValueError, match="lacks accepted execution evidence"):
        expand_robust_shape_manifest(envelope)


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("tree", "does not match"),
        ("source", "source identity"),
        ("variable", "bounds differ"),
    ],
)
def test_manifest_rejects_untrusted_adapter_binding(ascii_tmp_path, field, message):
    envelope, _, manifest = _write_manifest(ascii_tmp_path)
    raw = json.loads(manifest.read_text())
    if field == "tree":
        raw["structure_tree_audit"]["topology"]["boundary_count"] += 1
    elif field == "source":
        raw["support"]["source_identity"] = "0" * 64
    else:
        raw["support"]["variables"][1]["upper"] = 999.0
    payload = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    manifest.write_bytes(payload)
    envelope["submission_manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    with pytest.raises(ValueError, match=message):
        expand_robust_shape_manifest(envelope)
