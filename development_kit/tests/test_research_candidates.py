"""Canonical pre-side-effect candidate contracts."""

from __future__ import annotations

import copy

import pytest

from comsol_mcp.research.records import normalize_candidate_record
from development_kit.tests.test_research_contracts import _space


def _candidate() -> dict:
    return {
        "schema_name": "comsol_mcp.research_candidate",
        "schema_version": "1.0.0",
        "candidate_id": "candidate-001",
        "campaign_fingerprint": "a" * 64,
        "requested_values": {"patch_length_x": 100.0, "patch_length_y": 80.0},
        "parent_candidate_ids": [],
        "proposal_reason": "Deterministic initial point.",
        "hypothesis": None,
        "preflight_results": [],
        "predicted_resource_class": "fem_standard",
        "requested_fidelity": "coarse_fem",
        "producer_identity": "b" * 64,
        "optimizer_identity": "c" * 64,
        "random_seed": 17001,
        "lifecycle_state": "proposed",
        "terminal_reason": None,
    }


def test_candidate_is_canonical_and_deduplicates_numeric_spellings():
    first = normalize_candidate_record(_candidate(), _space())
    alternate = _candidate()
    alternate["candidate_id"] = "candidate-002"
    alternate["requested_values"] = {"patch_length_x": 100, "patch_length_y": 80}
    second = normalize_candidate_record(alternate, _space())
    assert first["normalized_values"] == {"patch_length_x": 100.0, "patch_length_y": 80.0}
    assert first["candidate_fingerprint"] == second["candidate_fingerprint"]
    assert first["record_fingerprint"] != second["record_fingerprint"]


@pytest.mark.parametrize(
    "values",
    [
        {"patch_length_x": 74.9, "patch_length_y": 80.0},
        {"patch_length_x": True, "patch_length_y": 80.0},
        {"patch_length_x": float("nan"), "patch_length_y": 80.0},
        {"patch_length_x": 100.0},
        {"patch_length_x": 100.0, "patch_length_y": 80.0, "extra": 1.0},
    ],
)
def test_candidate_rejects_out_of_space_or_nonfinite_values(values):
    value = _candidate()
    value["requested_values"] = values
    with pytest.raises(ValueError):
        normalize_candidate_record(value, _space())


def test_candidate_rejects_oversized_integer_as_value_error():
    value = _candidate()
    value["requested_values"] = {"patch_length_x": 10**400, "patch_length_y": 80}
    with pytest.raises(ValueError, match="must be finite"):
        normalize_candidate_record(value, _space())


def test_candidate_fingerprint_changes_with_campaign_or_normalized_point():
    first = normalize_candidate_record(_candidate(), _space())
    changed_campaign = _candidate()
    changed_campaign["campaign_fingerprint"] = "d" * 64
    changed_point = _candidate()
    changed_point["requested_values"]["patch_length_x"] = 101.0
    assert (
        first["candidate_fingerprint"]
        != normalize_candidate_record(changed_campaign, _space())["candidate_fingerprint"]
    )
    assert (
        first["candidate_fingerprint"]
        != normalize_candidate_record(changed_point, _space())["candidate_fingerprint"]
    )


def test_candidate_terminal_state_and_reason_must_agree():
    value = _candidate()
    value["lifecycle_state"] = "failed"
    with pytest.raises(ValueError, match="terminal states require"):
        normalize_candidate_record(value, _space())
    value["terminal_reason"] = "evaluation_failed"
    assert normalize_candidate_record(value, _space())["lifecycle_state"] == "failed"
    value = _candidate()
    value["terminal_reason"] = "premature"
    with pytest.raises(ValueError, match="nonterminal states require null"):
        normalize_candidate_record(value, _space())


def test_preflight_results_are_unique_sorted_and_boolean():
    value = _candidate()
    value["preflight_results"] = [
        {"constraint_id": "z", "passed": True, "reason_code": "passed"},
        {"constraint_id": "a", "passed": False, "reason_code": "gap_too_small"},
    ]
    normalized = normalize_candidate_record(value, _space())
    assert [item["constraint_id"] for item in normalized["preflight_results"]] == ["a", "z"]
    duplicate = copy.deepcopy(value)
    duplicate["preflight_results"][1]["constraint_id"] = "z"
    with pytest.raises(ValueError, match="must be unique"):
        normalize_candidate_record(duplicate, _space())
    value["preflight_results"][0]["passed"] = 1
    with pytest.raises(ValueError, match="must be boolean"):
        normalize_candidate_record(value, _space())


def test_categorical_candidate_must_use_exact_allowed_value():
    space = _space()
    space["variables"][0] = {
        "variable_id": "material_state",
        "kind": "categorical",
        "unit": "1",
        "baseline": "gold",
        "lower": None,
        "upper": None,
        "allowed_values": ["gold", "silver"],
        "dependency_class": "material",
        "adapter_path": "material.state",
    }
    space["adapter_mappings"][0] = {
        "variable_id": "material_state",
        "adapter_path": "material.state",
        "unit": "1",
    }
    value = _candidate()
    value["requested_values"] = {"material_state": "gold", "patch_length_y": 80.0}
    assert normalize_candidate_record(value, space)["normalized_values"]["material_state"] == "gold"
    value["requested_values"]["material_state"] = "copper"
    with pytest.raises(ValueError, match="outside allowed_values"):
        normalize_candidate_record(value, space)


def test_candidate_negative_zero_canonicalizes_to_positive_zero():
    import math

    space = _space()
    variable = {
        "variable_id": "signed_offset",
        "kind": "continuous",
        "unit": "nm",
        "baseline": 0.0,
        "lower": -10.0,
        "upper": 10.0,
        "allowed_values": None,
        "dependency_class": "geometry",
        "adapter_path": "geom.signed_offset",
    }
    space["variables"] = [variable]
    space["adapter_mappings"] = [
        {
            "variable_id": variable["variable_id"],
            "adapter_path": variable["adapter_path"],
            "unit": variable["unit"],
        }
    ]
    negative = _candidate()
    negative["requested_values"] = {"signed_offset": -1e-15}
    zero = _candidate()
    zero["requested_values"] = {"signed_offset": 0.0}
    first = normalize_candidate_record(negative, space)
    second = normalize_candidate_record(zero, space)
    value = first["normalized_values"]["signed_offset"]
    assert value == 0.0
    assert math.copysign(1.0, value) == 1.0
    # Identical canonical values must share one candidate identity.
    assert first["candidate_fingerprint"] == second["candidate_fingerprint"]


def test_candidate_rejects_non_string_lifecycle_state_as_value_error():
    value = _candidate()
    value["lifecycle_state"] = ["proposed"]
    with pytest.raises(ValueError):
        normalize_candidate_record(value, _space())
