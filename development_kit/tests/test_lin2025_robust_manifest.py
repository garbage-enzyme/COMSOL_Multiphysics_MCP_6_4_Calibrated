"""Solver-free tests for the repository-owned Lin2025 robust manifest compiler."""

from __future__ import annotations

import hashlib
import json

import pytest

from comsol_mcp.jobs.store import read_json
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
    raw["finalist_validation_policy"]["shape_policy_fingerprint"] = (
        normalize_shape_support_policy(raw["shape_policy"])["policy_fingerprint"]
    )
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
    controls = {
        "schema_name": "comsol_mcp.robust_condition_controls",
        "schema_version": "1.0.0",
        "component_tag": "comp1",
        "physics_tag": "ewfd",
        "periodic_structure_tag": "ps1",
        "periodic_port_tags": ["pport1", "pport2"],
        "reference_direction_tag": "rdir1",
        "wavelength_parameter": "wl",
        "elevation_parameter": "theta",
        "azimuth_parameter": "phi",
        "study_tag": "std1",
        "study_step_tag": "wl_step",
        "solution_tag": "sol1",
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
