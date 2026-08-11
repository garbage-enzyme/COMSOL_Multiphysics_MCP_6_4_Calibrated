"""Independent finalist promotion evidence tests."""

from __future__ import annotations

import copy

import pytest

from comsol_mcp.research.robust_conditions import normalize_optimization_condition_table
from comsol_mcp.research.robust_finalist_evidence import (
    assess_robust_finalist_validation,
    normalize_robust_finalist_validation_receipt,
)
from comsol_mcp.research.shape_support import normalize_shape_support_policy
from development_kit.tests.test_external_validation import _receipt as _external_receipt
from development_kit.tests.test_robust_conditions import _table
from development_kit.tests.test_robust_finalist_validation import _policy
from development_kit.tests.test_shape_support import _policy as _shape_policy


def _inputs() -> tuple[dict, dict, dict, dict]:
    conditions = _table()
    shape = _shape_policy()
    normalized_conditions = normalize_optimization_condition_table(conditions)
    normalized_shape = normalize_shape_support_policy(shape)
    policy = _policy(
        condition_table_fingerprint=normalized_conditions["condition_table_fingerprint"],
        shape_policy_fingerprint=normalized_shape["policy_fingerprint"],
    )
    candidate = "a" * 64
    rows = []
    for condition in normalized_conditions["conditions"]:
        if not condition["active"] or condition["objective_role"] != "objective":
            continue
        for axis, offsets in (
            ("wavelength_relative", (-0.01, 0.01)),
            ("angle_deg", (-2.0, 2.0)),
        ):
            for offset in offsets:
                rows.append(
                    {
                        "base_condition_id": condition["condition_id"],
                        "axis": axis,
                        "offset": offset,
                        "status": "measured",
                        "objective_value": 0.45,
                        "evidence_sha256": f"{len(rows) + 1:064x}",
                    }
                )
    evidence = {
        "candidate_fingerprint": candidate,
        "optimizer_execution_fingerprint": "b" * 64,
        "manufacturability": {
            "shape_policy_fingerprint": normalized_shape["policy_fingerprint"],
            "minimum_gap_m": 70e-9,
            "minimum_thickness_m": 25e-9,
            "minimum_radius_m": None,
            "topology_preserved": True,
            "selections_preserved": True,
            "positive_dimensions": True,
            "self_intersection_absent": True,
            "evidence_sha256": "c" * 64,
        },
        "fresh_remesh": {
            "candidate_fingerprint": candidate,
            "explicit_rebuild": True,
            "optimizer_state_reused": False,
            "model_sha256": "d" * 64,
            "mesh_sha256": "e" * 64,
            "element_count": 120_000,
            "minimum_element_quality": 0.2,
            "quality_measure": "volcircum",
            "objective_evidence_sha256": "f" * 64,
            "evidence_sha256": "1" * 64,
        },
        "mesh_convergence": {
            "levels": [
                {
                    "level_id": "baseline",
                    "candidate_fingerprint": candidate,
                    "model_sha256": "2" * 64,
                    "mesh_sha256": "3" * 64,
                    "element_count": 100_000,
                    "minimum_element_quality": 0.2,
                    "quality_measure": "volcircum",
                    "objective_configuration_fingerprint": "4" * 64,
                    "objective_value": 0.5,
                    "objective_evidence_sha256": "5" * 64,
                },
                {
                    "level_id": "finer",
                    "candidate_fingerprint": candidate,
                    "model_sha256": "6" * 64,
                    "mesh_sha256": "7" * 64,
                    "element_count": 150_000,
                    "minimum_element_quality": 0.18,
                    "quality_measure": "volcircum",
                    "objective_configuration_fingerprint": "4" * 64,
                    "objective_value": 0.52,
                    "objective_evidence_sha256": "8" * 64,
                },
            ],
            "evidence_sha256": "9" * 64,
        },
        "branch_guard": {
            "mode": "required",
            "observable_id": "transmission-contrast",
            "baseline_branch_id": "mode-1",
            "finer_branch_id": "mode-1",
            "baseline_mode_order": 1,
            "finer_mode_order": 1,
            "ambiguous": False,
            "disappeared": False,
            "evidence_sha256": "0" * 64,
        },
        "off_design_rows": rows,
        "external_validation_receipt": _external_receipt(),
    }
    return policy, conditions, shape, evidence


def test_complete_finalist_evidence_is_validated_and_fingerprint_stable():
    policy, conditions, shape, evidence = _inputs()
    receipt = assess_robust_finalist_validation(policy, conditions, shape, evidence)
    assert receipt["accepted"] is True
    assert receipt["disposition"] == "validated"
    assert all(receipt["checks"].values())
    assert receipt["mesh_convergence"]["relative_objective_change"] == pytest.approx(0.04)
    assert receipt["off_design"] == {
        "expected_row_count": 96,
        "observed_row_count": 96,
        "all_measured": True,
        "rows_fingerprint": receipt["off_design"]["rows_fingerprint"],
    }
    assert normalize_robust_finalist_validation_receipt(receipt) == receipt


@pytest.mark.parametrize(
    ("mutation", "failed_check"),
    [
        (
            lambda evidence: evidence["manufacturability"].update(minimum_gap_m=50e-9),
            "manufacturability",
        ),
        (
            lambda evidence: evidence["fresh_remesh"].update(optimizer_state_reused=True),
            "fresh_remesh",
        ),
        (
            lambda evidence: evidence["mesh_convergence"]["levels"][1].update(objective_value=0.53),
            "mesh_convergence",
        ),
        (
            lambda evidence: evidence["branch_guard"].update(finer_mode_order=2),
            "branch_guard",
        ),
        (lambda evidence: evidence["off_design_rows"].pop(), "off_design"),
        (
            lambda evidence: evidence["external_validation_receipt"].update(
                disposition="disagreed"
            ),
            "external_fidelity",
        ),
    ],
)
def test_failed_scientific_checks_are_retained_as_rejected_receipts(mutation, failed_check):
    policy, conditions, shape, evidence = _inputs()
    mutation(evidence)
    if failed_check == "external_fidelity":
        evidence["external_validation_receipt"].pop("receipt_fingerprint", None)
    receipt = assess_robust_finalist_validation(policy, conditions, shape, evidence)
    assert receipt["accepted"] is False
    assert receipt["checks"][failed_check] is False
    assert f"{failed_check}_failed" in receipt["reason_codes"]


def test_missing_required_external_receipt_is_an_honest_rejection():
    policy, conditions, shape, evidence = _inputs()
    evidence["external_validation_receipt"] = None
    receipt = assess_robust_finalist_validation(policy, conditions, shape, evidence)
    assert receipt["checks"]["external_fidelity"] is False
    assert receipt["external_validation_receipt_fingerprint"] is None


def test_identity_drift_duplicate_rows_and_receipt_tampering_fail_closed():
    policy, conditions, shape, evidence = _inputs()
    evidence["fresh_remesh"]["candidate_fingerprint"] = "0" * 64
    with pytest.raises(ValueError, match="candidate identity"):
        assess_robust_finalist_validation(policy, conditions, shape, evidence)
    policy, conditions, shape, evidence = _inputs()
    evidence["off_design_rows"].append(copy.deepcopy(evidence["off_design_rows"][0]))
    with pytest.raises(ValueError, match="unique"):
        assess_robust_finalist_validation(policy, conditions, shape, evidence)
    policy, conditions, shape, evidence = _inputs()
    receipt = assess_robust_finalist_validation(policy, conditions, shape, evidence)
    receipt["checks"]["branch_guard"] = False
    with pytest.raises(ValueError, match="reason codes|fingerprint"):
        normalize_robust_finalist_validation_receipt(receipt)
