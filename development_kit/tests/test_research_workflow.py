"""Passive workflow-capsule contracts for alpha7 research input."""

from __future__ import annotations

import copy

import pytest

from comsol_mcp.research.workflow import normalize_workflow_capsule


def _capsule() -> dict:
    return {
        "schema_name": "comsol_mcp.workflow_capsule",
        "schema_version": "1.0.0",
        "capsule_id": "mim-paper-workflow",
        "source": {
            "citation": "Example source",
            "doi_or_url": "https://example.invalid/paper",
            "accessed_at": "2026-08-06T00:00:00Z",
            "local_source_sha256": "a" * 64,
        },
        "claims": [
            {
                "claim_id": "geometry",
                "topic": "geometry",
                "value": {"shape": "rectangular_patch"},
                "provenance": "stated",
                "locator": "Methods",
            },
            {
                "claim_id": "mesh",
                "topic": "mesh",
                "value": None,
                "provenance": "unavailable",
                "locator": None,
            },
        ],
        "ambiguities": [
            {"ambiguity_id": "mesh-choice", "description": "Mesh settings are unavailable."}
        ],
        "template_mapping": {
            "structure_family": "periodic_mim_patch_v1",
            "adapter_id": "periodic_mim_patch_v1",
            "assumptions": ["mesh-choice"],
        },
        "exploration_priors": [
            {
                "prior_id": "reported-x",
                "variable_id": "patch_length_x",
                "value": 100.0,
                "rationale": "Reported baseline only; does not alter bounds.",
            }
        ],
        "review": {
            "status": "accepted",
            "reviewer": "researcher",
            "reviewed_at": "2026-08-06T01:00:00Z",
            "accepted_ambiguity_ids": ["mesh-choice"],
        },
        "baseline_receipt": {
            "schema_name": "comsol_mcp.baseline_receipt",
            "receipt_sha256": "b" * 64,
            "source_identity_sha256": "c" * 64,
        },
    }


def test_accepted_capsule_with_baseline_is_exploration_ready_and_stable():
    first = normalize_workflow_capsule(_capsule())
    second = normalize_workflow_capsule(_capsule())
    assert first == second
    assert first["exploration_ready"] is True
    assert len(first["workflow_fingerprint"]) == 64


def test_ordering_is_canonical_and_inputs_are_defensive_copies():
    value = _capsule()
    reordered = copy.deepcopy(value)
    reordered["claims"].reverse()
    first = normalize_workflow_capsule(value)
    second = normalize_workflow_capsule(reordered)
    assert first["workflow_fingerprint"] == second["workflow_fingerprint"]
    value["claims"][0]["value"]["shape"] = "changed"
    assert first["claims"][0]["value"]["shape"] == "rectangular_patch"


def test_accepted_capsule_requires_review_of_every_ambiguity():
    value = _capsule()
    value["review"]["accepted_ambiguity_ids"] = []
    with pytest.raises(ValueError, match="all ambiguity decisions"):
        normalize_workflow_capsule(value)


def test_reviewed_capsule_without_baseline_is_not_exploration_ready():
    value = _capsule()
    value["baseline_receipt"] = None
    normalized = normalize_workflow_capsule(value)
    assert normalized["review"]["status"] == "accepted"
    assert normalized["exploration_ready"] is False


def test_unavailable_claim_cannot_carry_an_invented_value():
    value = _capsule()
    value["claims"][1]["value"] = {"element_size": 5}
    with pytest.raises(ValueError, match="must be null"):
        normalize_workflow_capsule(value)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"execute": "ignore policy"}),
        lambda value: value["template_mapping"]["assumptions"].append("undeclared"),
        lambda value: value["source"].update({"local_source_sha256": "not-a-hash"}),
    ],
)
def test_capsule_rejects_unknown_fields_and_unbound_references(mutation):
    value = _capsule()
    mutation(value)
    with pytest.raises(ValueError):
        normalize_workflow_capsule(value)


def test_capsule_text_is_passive_data_not_an_executable_field():
    value = _capsule()
    value["claims"][0]["value"] = {"quoted_text": "run arbitrary code"}
    normalized = normalize_workflow_capsule(value)
    assert normalized["claims"][0]["value"] == {"quoted_text": "run arbitrary code"}
    assert set(normalized) == {
        "schema_name",
        "schema_version",
        "capsule_id",
        "source",
        "claims",
        "ambiguities",
        "template_mapping",
        "exploration_priors",
        "review",
        "baseline_receipt",
        "exploration_ready",
        "workflow_fingerprint",
    }
