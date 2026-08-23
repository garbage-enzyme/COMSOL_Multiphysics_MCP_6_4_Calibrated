"""Solver-free tests for the repository-owned Lin2025 robust manifest compiler."""

from __future__ import annotations

import hashlib
import json

import pytest

from comsol_mcp.jobs.robust_shape_optimization import expand_robust_shape_manifest
from comsol_mcp.jobs.store import read_json
from comsol_mcp.research.robust_condition_controls import normalize_robust_condition_controls
from comsol_mcp.research.robust_conditions import normalize_optimization_condition_table
from comsol_mcp.research.shape_support import normalize_shape_support_policy
from development_kit.scripts.lin2025_robust_manifest import (
    CAMPAIGN_SCHEMA_NAME,
    CAMPAIGN_SCHEMA_VERSION,
    compile_lin2025_robust_submission,
)
from development_kit.tests.test_lin2025_pedot_backend import (
    _derivative_support as _lin_support,
)
from development_kit.tests.test_lin2025_pedot_backend import _fixture as _lin_fixture
from development_kit.tests.test_lin2025_pedot_backend import _tree as _lin_tree
from development_kit.tests.test_robust_adapter_configuration import _forward_shape_controls
from development_kit.tests.test_robust_shape_optimization import _write_manifest


def _write_inputs(root):
    envelope, source, base_manifest = _write_manifest(root)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    raw = json.loads(base_manifest.read_text())
    support = _lin_support()
    support["source_identity"] = source_hash
    fixture = _lin_fixture()
    fixture["source_identity"]["source_sha256"] = source_hash
    tree = _lin_tree()
    tree["source_sha256"] = source_hash
    wavelengths = dict(
        zip(
            sorted({row["wavelength_m"] for row in raw["condition_table"]["conditions"]}),
            (8e-7, 1e-6, 1.2e-6),
            strict=True,
        )
    )
    for row in raw["condition_table"]["conditions"]:
        row["wavelength_m"] = wavelengths[row["wavelength_m"]]
    condition_table = normalize_optimization_condition_table(raw["condition_table"])
    raw["shape_policy"]["adapter_id"] = "lin2025_pedot_cylinder_v1"
    raw["finalist_validation_policy"]["condition_table_fingerprint"] = condition_table[
        "condition_table_fingerprint"
    ]
    raw["finalist_validation_policy"]["shape_policy_fingerprint"] = normalize_shape_support_policy(
        raw["shape_policy"]
    )["policy_fingerprint"]
    material_rows = {
        "schema_name": "comsol_mcp.robust_material_tensor_rows",
        "schema_version": "1.0.0",
        "source_sha256": "c" * 64,
        "states": [
            {
                "state_id": state,
                "source_sha256": condition_table["material_states"][index][
                    "optical_property_source_sha256"
                ],
                "rows": [
                    {
                        "wavelength_m": wavelength,
                        "xx_real": 2.0 + sample_index * 0.1,
                        "xx_imag": -0.1,
                        "yy_real": 2.0 + sample_index * 0.1,
                        "yy_imag": -0.1,
                        "zz_real": 3.0 + sample_index * 0.1,
                        "zz_imag": -0.2,
                    }
                    for sample_index, wavelength in enumerate((8e-7, 1e-6, 1.2e-6))
                ],
            }
            for index, state in enumerate(("OX", "MR"))
        ],
    }
    controls = _forward_shape_controls()
    controls["observable_expression"] = support["objective"]["expression"]
    controls["forward_solved_shape_expressions"] = [
        "comp1.material.u",
        "comp1.material.v",
    ]
    campaign = {
        "schema_name": CAMPAIGN_SCHEMA_NAME,
        "schema_version": CAMPAIGN_SCHEMA_VERSION,
        "condition_controls": controls,
        "objective": raw["objective"],
        "shape_policy": raw["shape_policy"],
        "finalist_validation_policy": raw["finalist_validation_policy"],
        "gradient_policy": raw["gradient_policy"],
        "optimizer_policy": raw["optimizer_policy"],
        "native_optimizer": raw["native_optimizer"],
        "startup_admission": raw["startup_admission"],
        "initial_values": [260.0, 260.0],
        "cores": envelope["cores"],
        "version": envelope["version"],
        "resource_policy": envelope["resource_policy"],
        "condition_execution_limit": 24,
        "comsol_temporary_directory": str(root),
    }
    values = {
        "fixture.json": fixture,
        "tree.json": tree,
        "support.json": support,
        "pedot.json": {
            "schema_name": "comsol_mcp.robust_pedot_fixture_manifest",
            "condition_table": condition_table,
            "material_tensor_rows": material_rows,
        },
        "campaign.json": campaign,
    }
    for name, value in values.items():
        (root / name).write_text(json.dumps(value), encoding="utf-8")
    return source


def test_compiler_emits_self_validated_24_condition_submission(ascii_tmp_path):
    source = _write_inputs(ascii_tmp_path)
    manifest = ascii_tmp_path / "licensed-manifest.json"
    envelope = ascii_tmp_path / "licensed-envelope.json"
    receipt = compile_lin2025_robust_submission(
        source_model=source,
        fixture_path=ascii_tmp_path / "fixture.json",
        tree_path=ascii_tmp_path / "tree.json",
        support_path=ascii_tmp_path / "support.json",
        pedot_fixture_path=ascii_tmp_path / "pedot.json",
        campaign_path=ascii_tmp_path / "campaign.json",
        manifest_path=manifest,
        envelope_path=envelope,
    )
    assert receipt["condition_count"] == 24
    assert receipt["adapter_id"] == "lin2025_pedot_cylinder_v1"
    assert receipt["manifest_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert read_json(envelope)["submission_manifest_sha256"] == receipt["manifest_sha256"]
    assert read_json(manifest)["synthetic_mode"] is False


def test_compiler_passes_through_1_5_0_forward_shape_controls(ascii_tmp_path):
    source = _write_inputs(ascii_tmp_path)
    campaign_path = ascii_tmp_path / "campaign.json"
    campaign = read_json(campaign_path)
    campaign["condition_controls"].update(
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
    campaign_path.write_text(json.dumps(campaign), encoding="utf-8")

    receipt = compile_lin2025_robust_submission(
        source_model=source,
        fixture_path=ascii_tmp_path / "fixture.json",
        tree_path=ascii_tmp_path / "tree.json",
        support_path=ascii_tmp_path / "support.json",
        pedot_fixture_path=ascii_tmp_path / "pedot.json",
        campaign_path=campaign_path,
        manifest_path=ascii_tmp_path / "licensed-manifest.json",
        envelope_path=ascii_tmp_path / "licensed-envelope.json",
    )

    manifest = read_json(ascii_tmp_path / "licensed-manifest.json")
    controls = manifest["adapter_configuration"]["configuration"]["condition_controls"]
    assert receipt["condition_count"] == 24
    assert controls["schema_version"] == "1.5.0"
    assert controls["forward_shape_application_mode"] == "deformation_stage"
    assert controls["forward_deformation_step_tag"] == "dg_step"
    assert controls["forward_solved_shape_relative_tolerance"] == 1e-6
    normalized = normalize_robust_condition_controls(controls)
    assert normalized["forward_shape_application_mode"] == "deformation_stage"
    assert normalized["forward_deformation_physics_tag"] == "dg_pedot72"
    assert (
        len(
            expand_robust_shape_manifest(read_json(ascii_tmp_path / "licensed-envelope.json"))[
                "condition_table"
            ]["conditions"]
        )
        == 24
    )


def test_compiler_omits_null_condition_limit_for_full_run(ascii_tmp_path):
    source = _write_inputs(ascii_tmp_path)
    campaign_path = ascii_tmp_path / "campaign.json"
    campaign = read_json(campaign_path)
    campaign["condition_execution_limit"] = None
    campaign_path.write_text(json.dumps(campaign), encoding="utf-8")
    envelope_path = ascii_tmp_path / "licensed-envelope.json"

    receipt = compile_lin2025_robust_submission(
        source_model=source,
        fixture_path=ascii_tmp_path / "fixture.json",
        tree_path=ascii_tmp_path / "tree.json",
        support_path=ascii_tmp_path / "support.json",
        pedot_fixture_path=ascii_tmp_path / "pedot.json",
        campaign_path=campaign_path,
        manifest_path=ascii_tmp_path / "licensed-manifest.json",
        envelope_path=envelope_path,
    )

    envelope = read_json(envelope_path)
    assert receipt["condition_count"] == 24
    assert "condition_execution_limit" not in envelope
    assert len(expand_robust_shape_manifest(envelope)["condition_table"]["conditions"]) == 24


def test_compiler_requires_caller_directory_for_explicit_out_of_core(ascii_tmp_path):
    source = _write_inputs(ascii_tmp_path)
    campaign_path = ascii_tmp_path / "campaign.json"
    campaign = read_json(campaign_path)
    campaign.pop("comsol_temporary_directory")
    campaign_path.write_text(json.dumps(campaign), encoding="utf-8")
    with pytest.raises(ValueError, match="campaign input fields"):
        compile_lin2025_robust_submission(
            source_model=source,
            fixture_path=ascii_tmp_path / "fixture.json",
            tree_path=ascii_tmp_path / "tree.json",
            support_path=ascii_tmp_path / "support.json",
            pedot_fixture_path=ascii_tmp_path / "pedot.json",
            campaign_path=campaign_path,
            manifest_path=ascii_tmp_path / "licensed-manifest.json",
            envelope_path=ascii_tmp_path / "licensed-envelope.json",
        )


def test_expansion_rejects_omitted_directory_for_explicit_out_of_core(ascii_tmp_path):
    source = _write_inputs(ascii_tmp_path)
    manifest = ascii_tmp_path / "licensed-manifest.json"
    envelope_path = ascii_tmp_path / "licensed-envelope.json"
    compile_lin2025_robust_submission(
        source_model=source,
        fixture_path=ascii_tmp_path / "fixture.json",
        tree_path=ascii_tmp_path / "tree.json",
        support_path=ascii_tmp_path / "support.json",
        pedot_fixture_path=ascii_tmp_path / "pedot.json",
        campaign_path=ascii_tmp_path / "campaign.json",
        manifest_path=manifest,
        envelope_path=envelope_path,
    )
    envelope = read_json(envelope_path)
    envelope.pop("comsol_temporary_directory")
    with pytest.raises(ValueError, match="explicit out-of-core solve"):
        expand_robust_shape_manifest(envelope)


def test_compiler_removes_outputs_when_cross_binding_fails(ascii_tmp_path):
    source = _write_inputs(ascii_tmp_path)
    campaign_path = ascii_tmp_path / "campaign.json"
    campaign = read_json(campaign_path)
    campaign["resource_policy"]["max_mesh_elements"] -= 1
    campaign_path.write_text(json.dumps(campaign), encoding="utf-8")
    manifest = ascii_tmp_path / "bad-manifest.json"
    envelope = ascii_tmp_path / "bad-envelope.json"
    with pytest.raises(ValueError, match="mesh"):
        compile_lin2025_robust_submission(
            source_model=source,
            fixture_path=ascii_tmp_path / "fixture.json",
            tree_path=ascii_tmp_path / "tree.json",
            support_path=ascii_tmp_path / "support.json",
            pedot_fixture_path=ascii_tmp_path / "pedot.json",
            campaign_path=campaign_path,
            manifest_path=manifest,
            envelope_path=envelope,
        )
    assert not manifest.exists()
    assert not envelope.exists()


def test_failed_rerun_preserves_a_preexisting_envelope(ascii_tmp_path):
    source = _write_inputs(ascii_tmp_path)
    campaign_path = ascii_tmp_path / "campaign.json"
    campaign = read_json(campaign_path)
    campaign["resource_policy"]["max_mesh_elements"] -= 1
    campaign_path.write_text(json.dumps(campaign), encoding="utf-8")
    manifest = ascii_tmp_path / "licensed-manifest.json"
    envelope_path = ascii_tmp_path / "licensed-envelope.json"
    # A previous successful invocation left a valid envelope behind; this
    # invocation never writes it before validation fails, so cleanup must
    # not destroy it.
    envelope_path.write_text(json.dumps({"sentinel": True}), encoding="utf-8")
    with pytest.raises(ValueError, match="mesh"):
        compile_lin2025_robust_submission(
            source_model=source,
            fixture_path=ascii_tmp_path / "fixture.json",
            tree_path=ascii_tmp_path / "tree.json",
            support_path=ascii_tmp_path / "support.json",
            pedot_fixture_path=ascii_tmp_path / "pedot.json",
            campaign_path=campaign_path,
            manifest_path=manifest,
            envelope_path=envelope_path,
        )
    assert not manifest.exists()
    assert json.loads(envelope_path.read_text(encoding="utf-8")) == {"sentinel": True}


def test_envelope_write_failure_does_not_leave_an_orphaned_manifest(ascii_tmp_path, monkeypatch):
    source = _write_inputs(ascii_tmp_path)
    import development_kit.scripts.lin2025_robust_manifest as compiler_module

    manifest = ascii_tmp_path / "licensed-manifest.json"
    envelope_path = ascii_tmp_path / "licensed-envelope.json"
    real_write = compiler_module.atomic_write_json

    def failing_write(path, value):
        if path == envelope_path:
            raise OSError("controlled envelope write failure")
        real_write(path, value)

    monkeypatch.setattr(compiler_module, "atomic_write_json", failing_write)
    with pytest.raises(OSError, match="controlled envelope write failure"):
        compile_lin2025_robust_submission(
            source_model=source,
            fixture_path=ascii_tmp_path / "fixture.json",
            tree_path=ascii_tmp_path / "tree.json",
            support_path=ascii_tmp_path / "support.json",
            pedot_fixture_path=ascii_tmp_path / "pedot.json",
            campaign_path=ascii_tmp_path / "campaign.json",
            manifest_path=manifest,
            envelope_path=envelope_path,
        )
    assert not manifest.exists()
    assert not envelope_path.exists()
