"""Manual GCMMA/MMA robust optimizer policy tests."""

from __future__ import annotations

import copy

import pytest

from comsol_mcp.research.robust_optimizer_policy import normalize_robust_optimizer_policy


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
