"""Combined robust component and directional gradient acceptance tests."""

from __future__ import annotations

import copy

import pytest

from comsol_mcp.research.gradient_validation import (
    compare_directional_gradient,
    compare_gradient,
)
from comsol_mcp.research.robust_gradient_acceptance import (
    assess_licensed_gradient_ladder,
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


def _licensed_fd_row(variable_id: str, error: float) -> dict:
    steps = [
        {
            "relative_step": step,
            "relative_error": error + index * 0.001,
            "sign_agreement": True,
        }
        for index, step in enumerate((0.01, 0.003, 0.001))
    ]
    return {"variable_id": variable_id, "steps": steps, "selected": steps[0]}


def _licensed_receipts():
    source = {"source_revision": "a" * 40, "source_sha256": "b" * 64, "success": True}
    native = {
        **source,
        "derivatives": [
            {"variable_id": "patch_length_x", "accepted_real": 2.0},
            {"variable_id": "patch_length_y", "accepted_real": -1.0},
        ],
    }
    finite_difference = {
        **source,
        "cosine_similarity": 0.9996,
        "derivatives": [
            _licensed_fd_row("patch_length_x", 0.002),
            _licensed_fd_row("patch_length_y", 0.08),
        ],
    }
    directional = {
        **source,
        "variables": ["patch_length_x", "patch_length_y"],
        "relative_error": 0.039,
        "sign_agreement": True,
    }
    return native, finite_difference, directional


def test_licensed_ladder_receipts_produce_canonical_robust_acceptance():
    native, finite_difference, directional = _licensed_receipts()
    receipt = assess_licensed_gradient_ladder(
        _policy(),
        native,
        finite_difference,
        directional,
        native_receipt_sha256="c" * 64,
        finite_difference_receipt_sha256="d" * 64,
        directional_receipt_sha256="e" * 64,
    )
    assert receipt["schema_name"] == "comsol_mcp.robust_gradient_acceptance_receipt"
    assert receipt["passed"] is True
    assert receipt["component_relative_errors"] == [0.002, 0.08]
    assert len(receipt["gradient_fingerprint"]) == 64
    assert len(receipt["receipt_fingerprint"]) == 64


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda native, _fd, _directional: native.update(source_revision="f" * 39),
            "source revision",
        ),
        (
            lambda _native, fd, _directional: fd["derivatives"].reverse(),
            "variable order",
        ),
        (
            lambda _native, _fd, directional: directional.update(source_sha256="f" * 64),
            "different sources",
        ),
    ],
)
def test_licensed_ladder_acceptance_rejects_identity_or_order_drift(mutation, message):
    native, finite_difference, directional = _licensed_receipts()
    mutation(native, finite_difference, directional)
    with pytest.raises(ValueError, match=message):
        assess_licensed_gradient_ladder(
            _policy(),
            native,
            finite_difference,
            directional,
            native_receipt_sha256="c" * 64,
            finite_difference_receipt_sha256="d" * 64,
            directional_receipt_sha256="e" * 64,
        )


def test_licensed_ladder_acceptance_rejects_malformed_receipt_digest():
    native, finite_difference, directional = _licensed_receipts()
    with pytest.raises(ValueError, match="SHA-256"):
        assess_licensed_gradient_ladder(
            _policy(),
            native,
            finite_difference,
            directional,
            native_receipt_sha256="not-a-digest",
            finite_difference_receipt_sha256="d" * 64,
            directional_receipt_sha256="e" * 64,
        )


def _resigned(receipt: dict, fingerprint_field: str, domain: str) -> dict:
    from comsol_mcp.durable import domain_sha256_v2

    body = dict(receipt)
    body.pop(fingerprint_field)
    receipt[fingerprint_field] = domain_sha256_v2(domain, body)
    return receipt


def _component_domain() -> str:
    return "comsol_mcp.gradient_check"


def test_robust_path_rejects_negative_component_relative_error():
    component = _component()
    component["rows"][0]["selected"]["relative_error"] = -0.01
    component = _resigned(component, "check_fingerprint", _component_domain())
    with pytest.raises(ValueError, match="must be nonnegative"):
        assess_robust_gradient_acceptance(_policy(), component, _directional())


def test_robust_path_rejects_cosine_outside_unit_interval():
    component = _component()
    component["cosine_similarity"] = 2.0
    component = _resigned(component, "check_fingerprint", _component_domain())
    with pytest.raises(ValueError, match=r"outside \[-1, 1\]"):
        assess_robust_gradient_acceptance(_policy(), component, _directional())


def test_robust_path_rejects_negative_directional_relative_error():
    directional = _directional()
    directional["relative_error"] = -0.01
    directional = _resigned(
        directional, "directional_check_fingerprint", "comsol_mcp.directional_gradient_check"
    )
    with pytest.raises(ValueError, match="must be nonnegative"):
        assess_robust_gradient_acceptance(_policy(), _component(), directional)


@pytest.mark.parametrize("junk", [42, "0.01", None, [0.01]])
def test_robust_path_rejects_non_mapping_step_entries_as_value_errors(junk):
    # A fingerprint-valid receipt can still carry structurally malformed step
    # rows; they must raise the controlled ValueError, never AttributeError.
    component = _component()
    component["rows"][0]["steps"] = [component["rows"][0]["steps"][0], junk]
    component = _resigned(component, "check_fingerprint", _component_domain())
    with pytest.raises(ValueError, match="step structure"):
        assess_robust_gradient_acceptance(_policy(), component, _directional())


@pytest.mark.parametrize("step_value", [None, "0.01", True, 0.0, -0.01])
def test_robust_path_rejects_malformed_relative_steps_as_value_errors(step_value):
    component = _component()
    component["rows"][0]["steps"] = [
        component["rows"][0]["steps"][0],
        {"relative_step": step_value},
    ]
    component = _resigned(component, "check_fingerprint", _component_domain())
    with pytest.raises(ValueError, match="relative_step"):
        assess_robust_gradient_acceptance(_policy(), component, _directional())
