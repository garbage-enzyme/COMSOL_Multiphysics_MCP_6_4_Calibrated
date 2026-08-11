"""Finalist remesh, convergence, branch, off-design, and fidelity policy tests."""

from __future__ import annotations

import copy

import pytest

from comsol_mcp.research.robust_finalist_validation import (
    normalize_robust_finalist_validation_policy,
)


def _policy(
    *,
    condition_table_fingerprint: str = "1" * 64,
    shape_policy_fingerprint: str = "2" * 64,
) -> dict:
    return {
        "schema_name": "comsol_mcp.robust_finalist_validation_policy",
        "schema_version": "1.0.0",
        "policy_id": "pedot-finalist-v1",
        "condition_table_fingerprint": condition_table_fingerprint,
        "shape_policy_fingerprint": shape_policy_fingerprint,
        "fresh_remesh": {
            "required": True,
            "explicit_rebuild": True,
            "independent_from_optimizer_state": True,
        },
        "mesh_convergence": {
            "baseline_level_id": "baseline",
            "finer_level_id": "finer",
            "max_relative_objective_change": 0.05,
            "max_elements_per_model": 300_000,
            "minimum_element_quality": 0.1,
            "quality_measure": "volcircum",
        },
        "branch_guard": {
            "mode": "required",
            "observable_id": "transmission-contrast",
            "require_same_branch_identity": True,
            "require_same_mode_order": True,
            "ambiguity_disposition": "reject",
            "disappearance_disposition": "reject",
        },
        "off_design": {
            "mode": "required",
            "validation_only": True,
            "include_in_optimizer": False,
            "wavelength_relative_offsets": [0.01, -0.01],
            "angle_offsets_deg": [2.0, -2.0],
        },
        "external_fidelity": {
            "mode": "required",
            "primary_backend": "independent_comsol",
            "fallback_mode": "explicit_manual_rcwa",
            "automatic_fallback": False,
        },
    }


def test_fixture_policy_is_canonical_idempotent_and_caller_owned():
    normalized = normalize_robust_finalist_validation_policy(_policy())
    assert normalized["off_design"]["wavelength_relative_offsets"] == [-0.01, 0.01]
    assert normalized["off_design"]["angle_offsets_deg"] == [-2.0, 2.0]
    assert normalized["mesh_convergence"]["max_relative_objective_change"] == 0.05
    assert normalized["external_fidelity"]["automatic_fallback"] is False
    assert normalize_robust_finalist_validation_policy(normalized) == normalized


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(cores=14), "fields mismatch"),
        (lambda value: value.update(ram_bytes=1 << 30), "fields mismatch"),
        (
            lambda value: value["fresh_remesh"].update(explicit_rebuild=False),
            "fresh remesh",
        ),
        (
            lambda value: value["branch_guard"].update(require_same_mode_order=False),
            "fail closed",
        ),
        (
            lambda value: value["off_design"].update(include_in_optimizer=True),
            "validation-only",
        ),
        (
            lambda value: value["external_fidelity"].update(automatic_fallback=True),
            "automatic fallback",
        ),
    ],
)
def test_policy_rejects_host_assumptions_or_weakened_finalist_guards(mutation, message):
    value = _policy()
    mutation(value)
    with pytest.raises(ValueError, match=message):
        normalize_robust_finalist_validation_policy(value)


def test_optional_modes_preserve_explicit_no_claim_dispositions():
    value = _policy()
    value["branch_guard"] = {
        "mode": "not_applicable",
        "observable_id": None,
        "require_same_branch_identity": False,
        "require_same_mode_order": False,
        "ambiguity_disposition": "not_applicable",
        "disappearance_disposition": "not_applicable",
    }
    value["off_design"] = {
        "mode": "not_requested",
        "validation_only": False,
        "include_in_optimizer": False,
        "wavelength_relative_offsets": [],
        "angle_offsets_deg": [],
    }
    value["external_fidelity"] = {
        "mode": "not_requested",
        "primary_backend": None,
        "fallback_mode": "not_requested",
        "automatic_fallback": False,
    }
    normalized = normalize_robust_finalist_validation_policy(value)
    assert normalized["branch_guard"]["mode"] == "not_applicable"
    assert normalized["off_design"]["mode"] == "not_requested"
    assert normalized["external_fidelity"]["mode"] == "not_requested"


def test_policy_rejects_duplicate_zero_or_out_of_range_offsets_and_tampering():
    for offsets in ([0.01, 0.01], [0.0], [0.6]):
        value = _policy()
        value["off_design"]["wavelength_relative_offsets"] = offsets
        with pytest.raises(ValueError, match="offsets"):
            normalize_robust_finalist_validation_policy(value)
    normalized = normalize_robust_finalist_validation_policy(_policy())
    tampered = copy.deepcopy(normalized)
    tampered["mesh_convergence"]["max_relative_objective_change"] = 0.1
    with pytest.raises(ValueError, match="fingerprint"):
        normalize_robust_finalist_validation_policy(tampered)
