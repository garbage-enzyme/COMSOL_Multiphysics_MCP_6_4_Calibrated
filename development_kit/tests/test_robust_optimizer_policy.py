"""Manual GCMMA/MMA robust optimizer policy tests."""

from __future__ import annotations

import copy

import pytest

from comsol_mcp.research.robust_optimizer_policy import (
    assess_robust_optimizer_execution,
    normalize_robust_optimizer_policy,
)
from development_kit.tests.test_gradient_contracts import _optimizer


def _policy(selected: str = "gcmma") -> dict:
    return {
        "schema_name": "comsol_mcp.robust_optimizer_policy",
        "schema_version": "1.0.0",
        "policy_id": "robust-native-manual",
        "allowed_methods": ["gcmma", "mma"],
        "selected_method": selected,
        "automatic_fallback": False,
        "method_evidence": [
            {"method": "gcmma", "support_state": "validated", "evidence_sha256": "a" * 64},
            {"method": "mma", "support_state": "structural_only", "evidence_sha256": "b" * 64},
        ],
    }


def test_gcmma_is_executable_but_mma_requires_separate_live_validation():
    gcmma = normalize_robust_optimizer_policy(_policy())
    assert gcmma["execution_allowed"] is True
    assert gcmma["selection_disposition"] == "validated_manual_selection"
    mma = normalize_robust_optimizer_policy(_policy("mma"))
    assert mma["execution_allowed"] is False
    assert mma["selection_disposition"] == "validation_required"
    assert gcmma["policy_fingerprint"] != mma["policy_fingerprint"]
    assert normalize_robust_optimizer_policy(gcmma) == gcmma


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(automatic_fallback=True), "fallback is forbidden"),
        (lambda value: value.update(selected_method="ipopt"), "explicitly allowed"),
        (lambda value: value["allowed_methods"].append("ipopt"), "GCMMA/MMA"),
        (lambda value: value["method_evidence"].pop(), "exactly cover"),
        (
            lambda value: value["method_evidence"][0].update(evidence_sha256=None),
            "require evidence",
        ),
    ],
)
def test_policy_rejects_automatic_fallback_unreviewed_methods_or_missing_evidence(
    mutation, message
):
    value = _policy()
    mutation(value)
    with pytest.raises(ValueError, match=message):
        normalize_robust_optimizer_policy(value)


def test_policy_fingerprint_rejects_method_or_evidence_tampering():
    normalized = normalize_robust_optimizer_policy(_policy())
    tampered = copy.deepcopy(normalized)
    tampered["selected_method"] = "mma"
    with pytest.raises(ValueError, match="derived selection|fingerprint"):
        normalize_robust_optimizer_policy(tampered)


def _execution_receipt(*, delta: float = 0.4) -> dict:
    optimizer = _optimizer()
    baseline = 0.2
    fresh = baseline + delta
    return {
        "schema_name": "comsol_mcp.native_optimizer_licensed_gate",
        "schema_version": "1.0.0",
        "success": True,
        "source_revision": "a" * 40,
        "source_sha256": "b" * 64,
        "optimizer_method": "gcmma",
        "budget": optimizer["budget"],
        "baseline_objective": baseline,
        "final_objective": fresh,
        "fresh_forward_objective": fresh,
        "fresh_forward_delta": delta,
        "mesh_admission_policy": {
            "max_elements_per_model": 300_000,
            "minimum_element_quality": 0.1,
            "scope": "baseline_and_explicit_finalist_remesh",
            "internal_optimizer_remesh_callback": False,
        },
        "baseline_mesh": {
            "element_count": 20_000,
            "minimum_quality": 0.2,
            "mean_quality": 0.7,
            "quality_measure": "volcircum",
        },
        "remesh": {
            "explicit_rebuild": True,
            "before": {
                "element_count": 20_000,
                "minimum_quality": 0.2,
                "mean_quality": 0.7,
                "quality_measure": "volcircum",
            },
            "after": {
                "element_count": 21_000,
                "minimum_quality": 0.21,
                "mean_quality": 0.69,
                "quality_measure": "volcircum",
            },
        },
        "deformation_feasibility_policy": {
            "jacobian_expression": "reldetjac",
            "minimum_relative_jacobian": 0.0,
            "comparison": "strictly_greater_than",
            "scope": "fresh_forward_finalist_deformed_geometry",
        },
        "deformation_feasibility": {
            "sample_count": 100,
            "minimum_relative_jacobian": 0.02,
            "maximum_relative_jacobian": 1.0,
            "threshold": 0.0,
            "passed": True,
        },
        "cleanup": {"client_clear": True, "source_unchanged": True},
    }


def test_optimizer_execution_accepts_only_positive_fresh_forward_mesh_admitted_result():
    receipt = assess_robust_optimizer_execution(
        _optimizer(),
        _execution_receipt(),
        native_receipt_sha256="c" * 64,
        max_elements_per_model=300_000,
        minimum_element_quality=0.1,
        deformation_jacobian_expression="reldetjac",
        minimum_relative_jacobian=0.0,
    )
    assert receipt["schema_name"] == "comsol_mcp.robust_optimizer_execution_receipt"
    assert receipt["disposition"] == "accepted"
    assert receipt["fresh_forward_improvement"] is True
    assert all(receipt["mesh_checks"].values())
    assert all(receipt["deformation_checks"].values())
    assert receipt["automatic_fallback_used"] is False


def test_optimizer_execution_records_nonimproving_or_failed_method_as_rejected():
    nonimproving = assess_robust_optimizer_execution(
        _optimizer(),
        _execution_receipt(delta=-0.1),
        native_receipt_sha256="c" * 64,
        max_elements_per_model=300_000,
        minimum_element_quality=0.1,
        deformation_jacobian_expression="reldetjac",
        minimum_relative_jacobian=0.0,
    )
    assert nonimproving["disposition"] == "rejected"
    failed = _execution_receipt()
    failed["success"] = False
    failed["error"] = {"code": "deformation_feasibility_failed", "type": "FlException"}
    for field in (
        "baseline_objective",
        "final_objective",
        "fresh_forward_objective",
        "fresh_forward_delta",
        "mesh_admission_policy",
        "baseline_mesh",
        "remesh",
        "deformation_feasibility_policy",
        "deformation_feasibility",
    ):
        failed.pop(field)
    rejected = assess_robust_optimizer_execution(
        _optimizer(),
        failed,
        native_receipt_sha256="d" * 64,
        max_elements_per_model=300_000,
        minimum_element_quality=0.1,
        deformation_jacobian_expression="reldetjac",
        minimum_relative_jacobian=0.0,
    )
    assert rejected["execution_success"] is False
    assert rejected["failure_code"] == "deformation_feasibility_failed"
    assert rejected["disposition"] == "rejected"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda receipt: receipt.update(optimizer_method="mma"), "method differs"),
        (lambda receipt: receipt["budget"].update(max_solves=21), "budget differs"),
        (
            lambda receipt: receipt.update(fresh_forward_delta=0.3),
            "delta differs",
        ),
        (
            lambda receipt: receipt["remesh"]["after"].update(element_count=300_001),
            "",
        ),
    ],
)
def test_optimizer_execution_rejects_identity_evidence_or_mesh_drift(mutation, message):
    native = _execution_receipt()
    mutation(native)
    if message:
        with pytest.raises(ValueError, match=message):
            assess_robust_optimizer_execution(
                _optimizer(),
                native,
                native_receipt_sha256="c" * 64,
                max_elements_per_model=300_000,
                minimum_element_quality=0.1,
                deformation_jacobian_expression="reldetjac",
                minimum_relative_jacobian=0.0,
            )
    else:
        receipt = assess_robust_optimizer_execution(
            _optimizer(),
            native,
            native_receipt_sha256="c" * 64,
            max_elements_per_model=300_000,
            minimum_element_quality=0.1,
            deformation_jacobian_expression="reldetjac",
            minimum_relative_jacobian=0.0,
        )
        assert receipt["mesh_checks"]["finalist_remesh_admitted"] is False
        assert receipt["disposition"] == "rejected"
