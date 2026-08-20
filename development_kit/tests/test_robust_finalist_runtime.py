"""Solver-free orchestration tests for the licensed finalist runtime."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from comsol_mcp.durable import atomic_write_json, domain_sha256_v2
from comsol_mcp.jobs import robust_finalist_runtime
from comsol_mcp.jobs.robust_shape_optimization import expand_robust_shape_manifest
from comsol_mcp.jobs.store import read_json
from comsol_mcp.research.robust_finalist_validation import (
    normalize_robust_finalist_validation_policy,
)
from development_kit.scripts.lin2025_robust_manifest import (
    compile_lin2025_robust_submission,
)
from development_kit.tests.test_lin2025_robust_manifest import _write_inputs


def _spec(root: Path) -> dict:
    source = _write_inputs(root)
    compile_lin2025_robust_submission(
        source_model=source,
        fixture_path=root / "fixture.json",
        tree_path=root / "tree.json",
        support_path=root / "support.json",
        pedot_fixture_path=root / "pedot.json",
        campaign_path=root / "campaign.json",
        manifest_path=root / "manifest.json",
        envelope_path=root / "envelope.json",
    )
    spec = expand_robust_shape_manifest(read_json(root / "envelope.json"))
    policy = copy.deepcopy(spec["finalist_validation_policy"])
    policy.pop("policy_fingerprint")
    policy["external_fidelity"] = {
        "mode": "required",
        "primary_backend": "independent_comsol",
        "fallback_mode": "explicit_manual_rcwa",
        "automatic_fallback": False,
        "maximum_absolute_condition_delta": 1e-6,
    }
    spec["finalist_validation_policy"] = normalize_robust_finalist_validation_policy(policy)
    return spec


def _observation(condition: dict) -> dict:
    pair_order = condition["order"] // 2
    value = 0.7 + pair_order * 1e-4 if condition["material_state_id"] == "OX" else 0.2
    evidence = domain_sha256_v2(
        "comsol_mcp.fake_finalist_observation",
        {"condition_id": condition["condition_id"], "value": value},
    )
    return {
        "condition_id": condition["condition_id"],
        "observable_id": condition["observable_id"],
        "value": value,
        "evidence_sha256": evidence,
        "disposition": "measured",
    }


def test_finalist_runtime_collects_all_144_solves_and_validates_receipt(
    ascii_tmp_path, monkeypatch
):
    spec = _spec(ascii_tmp_path)
    calls = []

    def fake_execute(candidate, directory, **_kwargs):
        directory.mkdir(parents=True, exist_ok=True)
        mesh_reference = candidate["adapter_configuration"]["configuration"]["condition_controls"][
            "mesh_reference_value"
        ]
        elements = 150_000 if mesh_reference == "1200[nm]" else 100_000
        shape_support = candidate["adapter_configuration"]["configuration"]["shape_support"]
        body = {
            "schema_name": "comsol_mcp.robust_shape_application",
            "schema_version": "1.1.0",
            "mode": "deformation_stage",
            "initial_values": candidate["initial_values"],
            "dataset_tag": "dset1",
            "solution_tag": "sol1",
            "mesh_elements": elements,
            "minimum_mesh_quality": 0.2,
            "solver_started": True,
            "stage_fingerprint": "a" * 64,
            "solved_shape": [
                {
                    "variable_id": variable["variable_id"],
                    "observed_radius_m": value * 1e-9,
                    "matches": True,
                }
                for variable, value in zip(
                    candidate["support"]["variables"], candidate["initial_values"], strict=True
                )
            ],
            "shape_controls": {
                "shape_support_fingerprint": shape_support["support_fingerprint"],
                "controls": {
                    "deformed_geometry": {
                        "free_domains": shape_support["free_domains"],
                        "fixed_boundaries": shape_support["fixed_boundaries"],
                        "pedot_boundaries": shape_support["pedot_boundaries"],
                        "height_preserved": True,
                        "center_preserved": True,
                    }
                },
            },
        }
        application = {
            **body,
            "receipt_fingerprint": domain_sha256_v2("comsol_mcp.robust_shape_application", body),
        }
        atomic_write_json(directory / "shape-application.json", application)
        (directory / "robust-working.mph").write_bytes(candidate["spec_fingerprint"].encode())
        observations = [
            _observation(condition)
            for condition in candidate["condition_table"]["conditions"]
            if condition["active"] and condition["objective_role"] == "objective"
        ]
        calls.append((mesh_reference, len(observations)))
        return {"shape_application": application, "observations": observations}

    monkeypatch.setattr(robust_finalist_runtime, "execute_lin2025_conditions", fake_execute)
    accepted = [
        _observation(condition)
        for condition in spec["condition_table"]["conditions"]
        if condition["active"] and condition["objective_role"] == "objective"
    ]
    receipt = robust_finalist_runtime.run_licensed_finalist(
        spec,
        ascii_tmp_path / "finalist",
        attempt=1,
        candidate_values=[265.0, 264.5],
        candidate_fingerprint="b" * 64,
        accepted_observations=accepted,
        accepted_objective_value=0.5,
        optimizer_execution_fingerprint="c" * 64,
        client_factory=lambda **_kwargs: object(),
        java_environment_reader=lambda _name: str(ascii_tmp_path),
        cancel_requested=lambda: False,
    )

    assert calls == [("1600[nm]", 24), ("1200[nm]", 24), ("1600[nm]", 96)]
    assert spec["finalist_validation_solve_count"] == 144
    assert receipt["accepted"] is True
    assert receipt["checks"] == {
        "manufacturability": True,
        "fresh_remesh": True,
        "mesh_convergence": True,
        "branch_guard": True,
        "off_design": True,
        "external_fidelity": True,
    }
    assert receipt["mesh_convergence"]["levels"][1]["element_count"] == 150_000
    assert receipt["off_design"]["observed_row_count"] == 96
    assert read_json(ascii_tmp_path / "finalist" / "finalist-validation.json") == receipt
    external = read_json(ascii_tmp_path / "finalist" / "external-validation.json")
    assert external["receipt_fingerprint"] == receipt["external_validation_receipt_fingerprint"]
    assert (
        read_json(ascii_tmp_path / "finalist" / "finalist-summary.json")[
            "external_validation_receipt_fingerprint"
        ]
        == external["receipt_fingerprint"]
    )


@pytest.mark.parametrize("mutation", ["reordered", "duplicated"])
def test_finalist_runtime_rejects_condition_observation_drift(ascii_tmp_path, mutation):
    spec = _spec(ascii_tmp_path)
    observations = [
        _observation(condition)
        for condition in spec["condition_table"]["conditions"]
        if condition["active"] and condition["objective_role"] == "objective"
    ]
    if mutation == "reordered":
        observations[0], observations[1] = observations[1], observations[0]
    else:
        observations[1] = copy.deepcopy(observations[0])

    with pytest.raises(ValueError, match="missing, duplicated, or reordered"):
        robust_finalist_runtime._ordered_observations_by_condition_id(
            observations,
            spec["condition_table"],
            label="accepted optimizer",
        )


def test_shape_evidence_rejects_non_object_solved_shape_items(ascii_tmp_path):
    body = {
        "schema_name": "comsol_mcp.robust_shape_application",
        "schema_version": "1.1.0",
        "solved_shape": [None, None],
    }
    receipt = {
        **body,
        "receipt_fingerprint": domain_sha256_v2("comsol_mcp.robust_shape_application", body),
    }
    atomic_write_json(ascii_tmp_path / "shape-application.json", receipt)
    with pytest.raises(ValueError, match="solved-shape readback is incomplete"):
        robust_finalist_runtime._shape_evidence({"shape_application": receipt}, ascii_tmp_path)
