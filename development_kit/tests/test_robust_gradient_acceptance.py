"""Combined robust component and directional gradient acceptance tests."""

from __future__ import annotations

import copy

import pytest

from comsol_mcp.research.gradient_validation import (
    compare_directional_gradient,
    compare_gradient,
)
from comsol_mcp.research.robust_gradient_acceptance import (
    assess_robust_gradient_acceptance,
)
from development_kit.tests.test_gradient_contracts import _gradient, normalize_gradient_support
from development_kit.tests.test_gradient_validation import _finite_difference


def _policy() -> dict:
    return {
        "schema_name": "comsol_mcp.robust_gradient_acceptance_policy",
        "schema_version": "1.0.0",
        "component_relative_error_limit": 0.10,
        "directional_relative_error_limit": 0.05,
        "cosine_floor": 0.995,
        "require_sign": True,
        "required_relative_steps": [0.01, 0.003, 0.001],
    }


def _component() -> dict:
    return compare_gradient(
        _gradient(),
        normalize_gradient_support(),
        _finite_difference(),
        {
            "relative_error_limit": 0.10,
            "absolute_error_floor": 1.0e-12,
            "cosine_floor": 0.995,
            "require_sign": True,
        },
    )


def _directional() -> dict:
    return compare_directional_gradient(
        _gradient(),
        normalize_gradient_support(),
        [1.0],
        step=0.001,
        plus_objective=0.800002,
        minus_objective=0.799998,
        policy={
            "relative_error_limit": 0.05,
            "absolute_error_floor": 1.0e-12,
            "cosine_floor": 0.995,
            "require_sign": True,
        },
    )


def test_frozen_alpha72_gradient_thresholds_pass_complete_three_step_evidence():
    receipt = assess_robust_gradient_acceptance(_policy(), _component(), _directional())
    assert receipt["passed"] is True
    assert receipt["checks"] == {
        "three_step_policy_matches": True,
        "component_relative_errors_within_limit": True,
        "cosine_above_floor": True,
        "component_signs_agree": True,
        "directional_relative_error_within_limit": True,
        "directional_sign_agrees": True,
    }


def test_directional_limit_is_independent_from_component_limit():
    directional = _directional()
    directional["relative_error"] = 0.051
    body = dict(directional)
    body.pop("directional_check_fingerprint")
    from comsol_mcp.durable import domain_sha256_v2

    directional["directional_check_fingerprint"] = domain_sha256_v2(
        "comsol_mcp.directional_gradient_check", body
    )
    receipt = assess_robust_gradient_acceptance(_policy(), _component(), directional)
    assert receipt["passed"] is False
    assert receipt["checks"]["component_relative_errors_within_limit"] is True
    assert receipt["checks"]["directional_relative_error_within_limit"] is False


def test_missing_step_or_mismatched_gradient_identity_fails_closed():
    component = _component()
    component["rows"][0]["steps"].pop()
    body = dict(component)
    body.pop("check_fingerprint")
    from comsol_mcp.durable import domain_sha256_v2

    component["check_fingerprint"] = domain_sha256_v2("comsol_mcp.gradient_check", body)
    receipt = assess_robust_gradient_acceptance(_policy(), component, _directional())
    assert receipt["passed"] is False
    assert receipt["checks"]["three_step_policy_matches"] is False
    directional = _directional()
    directional["gradient_fingerprint"] = "f" * 64
    body = dict(directional)
    body.pop("directional_check_fingerprint")
    directional["directional_check_fingerprint"] = domain_sha256_v2(
        "comsol_mcp.directional_gradient_check", body
    )
    with pytest.raises(ValueError, match="different gradients"):
        assess_robust_gradient_acceptance(_policy(), _component(), directional)


def test_tampered_component_or_policy_fingerprint_is_rejected():
    component = copy.deepcopy(_component())
    component["cosine_similarity"] = 0.0
    with pytest.raises(ValueError, match="fingerprint"):
        assess_robust_gradient_acceptance(_policy(), component, _directional())
    from comsol_mcp.research.robust_gradient_acceptance import normalize_robust_gradient_policy

    normalized = normalize_robust_gradient_policy(_policy())
    normalized["cosine_floor"] = 0.9
    with pytest.raises(ValueError, match="fingerprint"):
        normalize_robust_gradient_policy(normalized)
