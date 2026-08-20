"""Geometry-scale shape support, mesh admission, and retention tests."""

from __future__ import annotations

import copy

import pytest

from comsol_mcp.research.shape_support import normalize_shape_support_policy


def _policy() -> dict:
    return {
        "schema_name": "comsol_mcp.shape_support_policy",
        "schema_version": "1.0.0",
        "policy_id": "pedot-shape-policy",
        "adapter_id": "periodic_mim_patch_v1",
        "minimum_gap": {
            "mode": "recommended",
            "explicit_value_m": None,
            "geometry_reference_length_m": 1.0e-6,
            "mesh_resolution_m": 20.0e-9,
            "mesh_multiplier": 3.0,
            "geometry_relative_floor": 0.05,
        },
        "geometry_guards": {
            "minimum_thickness_m": 20.0e-9,
            "minimum_radius_m": None,
            "preserve_topology": True,
            "preserve_selections": True,
            "require_positive_dimensions": True,
            "reject_self_intersection": True,
        },
        "mesh_admission": {
            "max_elements_per_model": 300_000,
            "minimum_element_quality": 0.1,
            "quality_measure": "volcircum",
            "check_after_build": True,
            "check_after_remesh": True,
            "check_before_solve": True,
        },
        "model_retention": {"mode": "finalist_only", "max_retained_models": 1},
    }


def test_recommended_gap_uses_only_geometry_and_mesh_and_element_cap_is_per_model():
    normalized = normalize_shape_support_policy(_policy())
    assert normalized["minimum_gap"]["effective_value_m"] == pytest.approx(60.0e-9)
    assert normalized["minimum_gap"]["claim_disposition"] == (
        "adapter_geometry_mesh_recommendation"
    )
    assert normalized["mesh_admission"]["max_elements_per_model"] == 300_000
    assert normalized["mesh_admission"]["scope"] == "per_model_not_cumulative"
    assert normalize_shape_support_policy(normalized) == normalized


def test_manual_override_and_not_requested_have_explicit_claim_dispositions():
    explicit = _policy()
    explicit["minimum_gap"] = {
        "mode": "explicit",
        "explicit_value_m": 75.0e-9,
        "geometry_reference_length_m": None,
        "mesh_resolution_m": None,
        "mesh_multiplier": None,
        "geometry_relative_floor": None,
    }
    assert normalize_shape_support_policy(explicit)["minimum_gap"] == {
        **explicit["minimum_gap"],
        "effective_value_m": 75.0e-9,
        "claim_disposition": "caller_explicit",
    }
    disabled = _policy()
    disabled["minimum_gap"] = {
        "mode": "not_requested",
        "explicit_value_m": None,
        "geometry_reference_length_m": None,
        "mesh_resolution_m": None,
        "mesh_multiplier": None,
        "geometry_relative_floor": None,
    }
    result = normalize_shape_support_policy(disabled)
    assert result["minimum_gap"]["effective_value_m"] is None
    assert result["minimum_gap"]["claim_disposition"] == "not_requested"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(cores=14), "fields mismatch"),
        (lambda value: value.update(ram_bytes=1 << 30), "fields mismatch"),
        (
            lambda value: value["mesh_admission"].update(check_after_remesh=False),
            "every build",
        ),
        (
            lambda value: value["geometry_guards"].update(preserve_topology=False),
            "every invariant",
        ),
        (
            lambda value: value["model_retention"].update(max_retained_models=2),
            "exactly one",
        ),
    ],
)
def test_policy_rejects_host_assumptions_or_weakened_validity_guards(mutation, message):
    value = _policy()
    mutation(value)
    with pytest.raises(ValueError, match=message):
        normalize_shape_support_policy(value)


def test_policy_fingerprint_rejects_mesh_or_gap_tampering():
    normalized = normalize_shape_support_policy(_policy())
    tampered = copy.deepcopy(normalized)
    tampered["mesh_admission"]["max_elements_per_model"] = 400_000
    with pytest.raises(ValueError, match="fingerprint"):
        normalize_shape_support_policy(tampered)
