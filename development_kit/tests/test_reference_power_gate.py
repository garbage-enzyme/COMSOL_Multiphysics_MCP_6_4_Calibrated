"""Solver-free evaluation gates for the reference-power fresh-process runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.evidence.contracts import (
    PHYSICAL_EVIDENCE_SCHEMA_NAME,
    PHYSICAL_EVIDENCE_SCHEMA_VERSION,
    build_physical_evidence,
)
from src.evidence.reference_power_gate import (
    build_reference_power_policies,
    evaluate_reference_power_negative_controls,
    evaluate_reference_power_results,
    inventory_reference_power_artifacts,
)


ROOT = Path(__file__).parents[2]
CONTRACT = json.loads(
    (
        ROOT
        / "development_kit"
        / "release"
        / "integration_fixtures"
        / "reference_power_evidence.json"
    ).read_text(
        encoding="utf-8"
    )
)


def _physical_evidence():
    incident_raw, reflected_raw, transmitted_raw = -2.0, 0.4, -1.2
    return build_physical_evidence(
        {
            "schema_name": PHYSICAL_EVIDENCE_SCHEMA_NAME,
            "schema_version": PHYSICAL_EVIDENCE_SCHEMA_VERSION,
            "artifact_type": "reference_power_unit_point",
            "producer": {"tool": "reference_power_unit", "tool_schema_version": "1"},
            "identity": {
                "config_id": "reference-power-unit",
                "config_sha256": "a" * 64,
                "source_sha256": "b" * 64,
            },
            "model": {
                "component_tag": "comp1",
                "physics_tag": "ewfd",
                "study_tag": "std1",
                "study_step_tag": "wl_step",
            },
            "evidence": {
                "flux.incident_raw_power_w": {"state": "measured", "value": incident_raw, "unit": "W"},
                "flux.reflected_raw_power_w": {"state": "measured", "value": reflected_raw, "unit": "W"},
                "flux.transmitted_raw_power_w": {"state": "measured", "value": transmitted_raw, "unit": "W"},
                "flux.incident_positive_power_sign": {"state": "derived_from_declared_convention", "value": -1},
                "flux.reflected_positive_power_sign": {"state": "derived_from_declared_convention", "value": 1},
                "flux.transmitted_positive_power_sign": {"state": "derived_from_declared_convention", "value": -1},
                "flux.incident_power_w": {"state": "derived_from_declared_convention", "value": 2.0, "unit": "W"},
                "flux.reflected_power_w": {"state": "derived_from_declared_convention", "value": 0.4, "unit": "W"},
                "flux.transmitted_power_w": {"state": "derived_from_declared_convention", "value": 1.2, "unit": "W"},
                "flux.R": {"state": "derived_from_declared_convention", "value": 0.2, "unit": "1"},
                "flux.T": {"state": "derived_from_declared_convention", "value": 0.6, "unit": "1"},
                "flux.A": {"state": "derived_from_declared_convention", "value": 0.2, "unit": "1"},
                "flux.closure_abs": {"state": "derived_from_declared_convention", "value": 0.0, "unit": "1"},
                "flux.convention_complete": {"state": "derived_from_declared_convention", "value": True},
                "flux.physical_flux_closure_eligible": {"state": "derived_from_declared_convention", "value": True},
            },
            "limitations": [],
        }
    )


def _results():
    evidence = _physical_evidence()
    policies = build_reference_power_policies(CONTRACT)
    point = {
        "audit_status": "policy_evaluated",
        "assessment": {"project_verdict": "pass"},
        "physical_evidence": evidence,
        "measurement": {
            "declared_plane_flux": {"R": 0.2, "T": 0.6, "A": 0.2, "closure_abs": 0.0},
            "wavelength": {"absolute_difference_m": 0.0, "relative_difference": 0.0},
            "integrity": {"source_unchanged": True},
        },
    }
    reference = {
        "audit_status": "measurement_complete",
        "assessment": {"overall": "pass"},
        "cleanup": {"removed": True},
        "reference": {
            "R": 0.0,
            "T": 1.0,
            "R_plus_T_residual_abs": 0.0,
            "target_to_transverse_ratio": 50.0,
        },
    }
    return reference, point, policies


def test_negative_controls_reuse_raw_evidence_and_must_fail_policy():
    _reference, point, policies = _results()
    result = evaluate_reference_power_negative_controls(
        point["physical_evidence"], policies["declared_flux"]
    )

    assert result["reversed_sign"]["overall"] == "fail"
    assert result["internal_consistency_substitution"]["overall"] == "fail"
    assert result["reversed_sign"]["passed_rejection_gate"] is True
    assert point["physical_evidence"]["evidence"]["flux.reflected_positive_power_sign"]["value"] == 1


def test_combined_reference_power_evaluation_requires_every_physical_and_negative_gate():
    reference, point, _policies = _results()
    passed = evaluate_reference_power_results(CONTRACT, reference, point)
    failed_reference = dict(reference)
    failed_reference["reference"] = {**reference["reference"], "R": 0.01}

    assert passed["passed"] is True
    failed = evaluate_reference_power_results(CONTRACT, failed_reference, point)
    assert failed["passed"] is False
    assert failed["checks"]["reference_air"]["reflection"] is False


def test_artifact_inventory_is_relative_hashed_and_bounded(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "a.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "nested" / "b.csv").write_text("x\n1\n", encoding="utf-8")

    result = inventory_reference_power_artifacts(tmp_path, CONTRACT["limits"])

    assert result["file_count"] == 2
    assert {item["relative_path"] for item in result["files"]} == {"a.json", "nested/b.csv"}
    assert all(len(item["sha256"]) == 64 for item in result["files"])

    limited = {**CONTRACT["limits"], "max_artifact_files": 1}
    with pytest.raises(ValueError, match="file count"):
        inventory_reference_power_artifacts(tmp_path, limited)
