"""Trusted structure-family manifest and exact live-tree audit tests."""

from __future__ import annotations

import copy

import pytest

from comsol_mcp.research.adapters import (
    STRUCTURE_ADAPTER_MANIFEST_SCHEMA_NAME,
    STRUCTURE_ADAPTER_MANIFEST_SCHEMA_VERSION,
    STRUCTURE_TREE_AUDIT_SCHEMA_NAME,
    STRUCTURE_TREE_AUDIT_SCHEMA_VERSION,
    normalize_structure_adapter_manifest,
    normalize_structure_tree_audit,
)


def _manifest() -> dict:
    return {
        "schema_name": STRUCTURE_ADAPTER_MANIFEST_SCHEMA_NAME,
        "schema_version": STRUCTURE_ADAPTER_MANIFEST_SCHEMA_VERSION,
        "adapter_id": "periodic_mim_patch_v1",
        "structure_family": "periodic_mim_patch_v1",
        "source_identity": {
            "source_sha256": "a" * 64,
            "comsol_build": "6.4.0.293",
            "tree_sha256": "b" * 64,
        },
        "component_tag": "comp1",
        "geometry_tag": "geom1",
        "patch_feature": {
            "tag_path": ["wp1", "r1"],
            "feature_types": ["WorkPlane", "Rectangle"],
            "size_property": "size",
            "position_property": "pos",
        },
        "mutable_dimensions": [
            {
                "variable_id": "patch_length_x",
                "property_index": 0,
                "unit": "m",
                "baseline": 300e-9,
                "lower": 225e-9,
                "upper": 375e-9,
            },
            {
                "variable_id": "patch_length_y",
                "property_index": 1,
                "unit": "m",
                "baseline": 300e-9,
                "lower": 225e-9,
                "upper": 375e-9,
            },
        ],
        "patch_center": [300e-9, 300e-9],
        "required_features": [
            {"scope": "geometry", "tag_path": ["wp1"], "feature_type": "WorkPlane"},
            {
                "scope": "geometry",
                "tag_path": ["wp1", "r1"],
                "feature_type": "Rectangle",
            },
            {"scope": "physics", "tag_path": ["ewfd"], "feature_type": "EWFD"},
            {
                "scope": "physics",
                "tag_path": ["ewfd", "ps1"],
                "feature_type": "PeriodicStructure",
            },
            {"scope": "mesh", "tag_path": ["mesh1"], "feature_type": "MeshSequence"},
            {"scope": "study", "tag_path": ["std1"], "feature_type": "Study"},
        ],
        "fixed_contracts": {
            "materials": "c" * 64,
            "incidence": "d" * 64,
            "period": "e" * 64,
            "layer_stack": "f" * 64,
            "topology": "1" * 64,
            "mesh": "2" * 64,
            "study": "3" * 64,
            "evidence": "4" * 64,
        },
        "topology_invariants": {
            "domain_count": 2,
            "boundary_count": 13,
            "x_pair_count": 1,
            "y_pair_count": 1,
            "top_port_count": 1,
            "bottom_port_count": 1,
        },
        "evidence_collectors": ["wave_optics_point_audit", "wave_optics_reference_audit"],
        "review": {
            "status": "accepted",
            "reviewer": "caller",
            "reviewed_at": "2026-08-07T00:00:00Z",
            "baseline_receipt_sha256": "5" * 64,
        },
    }


def _audit(manifest: dict) -> dict:
    normalized = normalize_structure_adapter_manifest(manifest)
    return {
        "schema_name": STRUCTURE_TREE_AUDIT_SCHEMA_NAME,
        "schema_version": STRUCTURE_TREE_AUDIT_SCHEMA_VERSION,
        "manifest_fingerprint": normalized["manifest_fingerprint"],
        "source_identity": normalized["source_identity"],
        "features": normalized["required_features"],
        "fixed_contracts": normalized["fixed_contracts"],
        "topology": normalized["topology_invariants"],
    }


def test_manifest_is_stable_defensive_and_reorders_declared_features():
    first = normalize_structure_adapter_manifest(_manifest())
    reordered = _manifest()
    reordered["required_features"].reverse()
    reordered["mutable_dimensions"].reverse()
    second = normalize_structure_adapter_manifest(reordered)
    assert first == second
    assert first["mutable_dimensions"][0]["variable_id"] == "patch_length_x"
    first["patch_center"][0] = 0
    assert normalize_structure_adapter_manifest(_manifest()) == second


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda value: value["mutable_dimensions"][0].update(lower=224e-9), "25 percent"),
        (lambda value: value["mutable_dimensions"][0].update(variable_id="period"), "trusted"),
        (lambda value: value["mutable_dimensions"][1].update(property_index=0), "size contract"),
        (lambda value: value["review"].update(status="pending"), "reviewed"),
        (lambda value: value["fixed_contracts"].pop("mesh"), "immutable"),
    ],
)
def test_manifest_rejects_scope_expansion_or_incomplete_trust(mutate, match):
    value = _manifest()
    mutate(value)
    with pytest.raises(ValueError, match=match):
        normalize_structure_adapter_manifest(value)


def test_tree_audit_accepts_only_exact_manifest_observation_and_fingerprints_it():
    manifest = normalize_structure_adapter_manifest(_manifest())
    audit = normalize_structure_tree_audit(_audit(manifest), manifest)
    assert audit["accepted"] is True
    assert normalize_structure_tree_audit(audit, manifest) == audit


@pytest.mark.parametrize("field", ["source", "feature", "fixed", "topology"])
def test_tree_audit_rejects_every_observed_drift(field):
    manifest = normalize_structure_adapter_manifest(_manifest())
    audit = _audit(manifest)
    if field == "source":
        audit["source_identity"]["tree_sha256"] = "0" * 64
    elif field == "feature":
        audit["features"][0]["feature_type"] = "Block"
    elif field == "fixed":
        audit["fixed_contracts"]["materials"] = "0" * 64
    else:
        audit["topology"]["boundary_count"] += 1
    with pytest.raises(ValueError, match="does not match"):
        normalize_structure_tree_audit(audit, manifest)


def test_manifest_and_audit_reject_tampered_fingerprints():
    manifest = normalize_structure_adapter_manifest(_manifest())
    tampered_manifest = copy.deepcopy(manifest)
    tampered_manifest["patch_center"][0] += 1e-9
    with pytest.raises(ValueError, match="manifest fingerprint"):
        normalize_structure_adapter_manifest(tampered_manifest)
    audit = normalize_structure_tree_audit(_audit(manifest), manifest)
    audit["audit_fingerprint"] = "0" * 64
    with pytest.raises(ValueError, match="audit fingerprint"):
        normalize_structure_tree_audit(audit, manifest)
