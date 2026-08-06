"""Trusted structure-family adapter manifests and exact tree audits."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .contracts import _bounded_json, _identifier, _object, _text, _timestamp

STRUCTURE_ADAPTER_MANIFEST_SCHEMA_NAME = "comsol_mcp.research_structure_adapter_manifest"
STRUCTURE_ADAPTER_MANIFEST_SCHEMA_VERSION = "1.0.0"
STRUCTURE_TREE_AUDIT_SCHEMA_NAME = "comsol_mcp.research_structure_tree_audit"
STRUCTURE_TREE_AUDIT_SCHEMA_VERSION = "1.0.0"
PERIODIC_MIM_PATCH_ADAPTER_ID = "periodic_mim_patch_v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VARIABLES = ("patch_length_x", "patch_length_y")
_FIXED_KEYS = {
    "evidence",
    "incidence",
    "layer_stack",
    "materials",
    "mesh",
    "period",
    "study",
    "topology",
}
_SCOPES = {"geometry", "material", "mesh", "physics", "result", "study"}


def _sha(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _tag_path(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise ValueError(f"{name} must be a bounded nonempty tag path")
    return [_identifier(item, f"{name}[{index}]") for index, item in enumerate(value)]


def _features(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 128:
        raise ValueError(f"{name} must be a bounded nonempty list")
    result = []
    for index, item in enumerate(value):
        item_name = f"{name}[{index}]"
        raw = _object(item, {"scope", "tag_path", "feature_type"}, item_name)
        if raw["scope"] not in _SCOPES:
            raise ValueError(f"{item_name}.scope is unsupported")
        result.append(
            {
                "scope": raw["scope"],
                "tag_path": _tag_path(raw["tag_path"], f"{item_name}.tag_path"),
                "feature_type": _text(
                    raw["feature_type"], f"{item_name}.feature_type", maximum=128
                ),
            }
        )
    keys = [(item["scope"], tuple(item["tag_path"])) for item in result]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{name} paths must be unique")
    return sorted(result, key=lambda item: (item["scope"], item["tag_path"]))


def _dimensions(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("mutable_dimensions must contain exactly two entries")
    by_id = {}
    for index, item in enumerate(value):
        name = f"mutable_dimensions[{index}]"
        raw = _object(
            item,
            {"variable_id", "property_index", "unit", "baseline", "lower", "upper"},
            name,
        )
        variable_id = _identifier(raw["variable_id"], f"{name}.variable_id")
        property_index = raw["property_index"]
        if isinstance(property_index, bool) or not isinstance(property_index, int):
            raise ValueError(f"{name}.property_index must be integer")
        baseline = _finite(raw["baseline"], f"{name}.baseline")
        lower = _finite(raw["lower"], f"{name}.lower")
        upper = _finite(raw["upper"], f"{name}.upper")
        if baseline <= 0 or lower != 0.75 * baseline or upper != 1.25 * baseline:
            raise ValueError(f"{name} must freeze exact positive +/-25 percent bounds")
        if variable_id in by_id:
            raise ValueError("mutable variable IDs must be unique")
        by_id[variable_id] = {
            "variable_id": variable_id,
            "property_index": property_index,
            "unit": _text(raw["unit"], f"{name}.unit", maximum=32),
            "baseline": baseline,
            "lower": lower,
            "upper": upper,
        }
    if set(by_id) != set(_VARIABLES) or {
        by_id["patch_length_x"]["property_index"],
        by_id["patch_length_y"]["property_index"],
    } != {0, 1}:
        raise ValueError("mutable dimensions are outside the trusted x/y size contract")
    return [by_id[item] for item in _VARIABLES]


def _fixed(value: object, name: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != _FIXED_KEYS:
        raise ValueError(f"{name} must cover every immutable adapter surface")
    return {key: _sha(value[key], f"{name}.{key}") for key in sorted(value)}


def _topology(value: object, name: str) -> dict[str, int]:
    fields = {
        "boundary_count",
        "bottom_port_count",
        "domain_count",
        "top_port_count",
        "x_pair_count",
        "y_pair_count",
    }
    raw = _object(value, fields, name)
    for key, item in raw.items():
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise ValueError(f"{name}.{key} must be a positive integer")
    return {key: raw[key] for key in sorted(raw)}


def normalize_structure_adapter_manifest(value: object) -> dict[str, Any]:
    """Normalize one caller-reviewed exact periodic MIM template contract."""
    bounded = _bounded_json(value, "structure adapter manifest", 512 * 1024)
    supplied = bounded.pop("manifest_fingerprint", None) if isinstance(bounded, dict) else None
    fields = {
        "adapter_id",
        "component_tag",
        "evidence_collectors",
        "fixed_contracts",
        "geometry_tag",
        "mutable_dimensions",
        "patch_center",
        "patch_feature",
        "required_features",
        "review",
        "schema_name",
        "schema_version",
        "source_identity",
        "structure_family",
        "topology_invariants",
    }
    raw = _object(bounded, fields, "structure adapter manifest")
    if (
        raw["schema_name"] != STRUCTURE_ADAPTER_MANIFEST_SCHEMA_NAME
        or raw["schema_version"] != STRUCTURE_ADAPTER_MANIFEST_SCHEMA_VERSION
        or raw["adapter_id"] != PERIODIC_MIM_PATCH_ADAPTER_ID
        or raw["structure_family"] != PERIODIC_MIM_PATCH_ADAPTER_ID
    ):
        raise ValueError("structure adapter schema or family identity is unsupported")
    source = _object(
        raw["source_identity"], {"comsol_build", "source_sha256", "tree_sha256"}, "source_identity"
    )
    patch = _object(
        raw["patch_feature"],
        {"feature_types", "position_property", "size_property", "tag_path"},
        "patch_feature",
    )
    path = _tag_path(patch["tag_path"], "patch_feature.tag_path")
    types = patch["feature_types"]
    if not isinstance(types, list) or len(types) != len(path):
        raise ValueError("patch feature types must align with its tag path")
    center = raw["patch_center"]
    if not isinstance(center, list) or len(center) != 2:
        raise ValueError("patch_center must contain x and y")
    collectors = raw["evidence_collectors"]
    if not isinstance(collectors, list) or not 1 <= len(collectors) <= 16:
        raise ValueError("evidence_collectors must be a bounded nonempty list")
    normalized_collectors = sorted(
        {
            _identifier(item, f"evidence_collectors[{index}]")
            for index, item in enumerate(collectors)
        }
    )
    if len(normalized_collectors) != len(collectors):
        raise ValueError("evidence_collectors must be unique")
    review = _object(
        raw["review"],
        {"baseline_receipt_sha256", "reviewed_at", "reviewer", "status"},
        "review",
    )
    if review["status"] != "accepted":
        raise ValueError("structure adapter manifest must be caller-reviewed and accepted")
    body = {
        "schema_name": STRUCTURE_ADAPTER_MANIFEST_SCHEMA_NAME,
        "schema_version": STRUCTURE_ADAPTER_MANIFEST_SCHEMA_VERSION,
        "adapter_id": PERIODIC_MIM_PATCH_ADAPTER_ID,
        "structure_family": PERIODIC_MIM_PATCH_ADAPTER_ID,
        "source_identity": {
            "comsol_build": _text(
                source["comsol_build"], "source_identity.comsol_build", maximum=64
            ),
            "source_sha256": _sha(source["source_sha256"], "source_identity.source_sha256"),
            "tree_sha256": _sha(source["tree_sha256"], "source_identity.tree_sha256"),
        },
        "component_tag": _identifier(raw["component_tag"], "component_tag"),
        "geometry_tag": _identifier(raw["geometry_tag"], "geometry_tag"),
        "patch_feature": {
            "tag_path": path,
            "feature_types": [
                _text(item, f"patch_feature.feature_types[{index}]", maximum=128)
                for index, item in enumerate(types)
            ],
            "size_property": _identifier(patch["size_property"], "patch_feature.size_property"),
            "position_property": _identifier(
                patch["position_property"], "patch_feature.position_property"
            ),
        },
        "mutable_dimensions": _dimensions(raw["mutable_dimensions"]),
        "patch_center": [
            _finite(center[0], "patch_center[0]"),
            _finite(center[1], "patch_center[1]"),
        ],
        "required_features": _features(raw["required_features"], "required_features"),
        "fixed_contracts": _fixed(raw["fixed_contracts"], "fixed_contracts"),
        "topology_invariants": _topology(raw["topology_invariants"], "topology_invariants"),
        "evidence_collectors": normalized_collectors,
        "review": {
            "status": "accepted",
            "reviewer": _text(review["reviewer"], "review.reviewer", maximum=256),
            "reviewed_at": _timestamp(review["reviewed_at"], "review.reviewed_at"),
            "baseline_receipt_sha256": _sha(
                review["baseline_receipt_sha256"], "review.baseline_receipt_sha256"
            ),
        },
    }
    fingerprint = domain_sha256_v2(STRUCTURE_ADAPTER_MANIFEST_SCHEMA_NAME, body)
    if supplied is not None and supplied != fingerprint:
        raise ValueError("structure adapter manifest fingerprint is invalid")
    return {**body, "manifest_fingerprint": fingerprint}


def normalize_structure_tree_audit(value: object, manifest: object) -> dict[str, Any]:
    """Bind a read-only live tree observation to one exact accepted manifest."""
    expected = normalize_structure_adapter_manifest(manifest)
    bounded = _bounded_json(value, "structure tree audit", 512 * 1024)
    supplied = bounded.pop("audit_fingerprint", None) if isinstance(bounded, dict) else None
    supplied_accepted = bounded.pop("accepted", None) if isinstance(bounded, dict) else None
    if supplied_accepted is not None and supplied_accepted is not True:
        raise ValueError("structure tree audit accepted field must be true")
    raw = _object(
        bounded,
        {
            "features",
            "fixed_contracts",
            "manifest_fingerprint",
            "schema_name",
            "schema_version",
            "source_identity",
            "topology",
        },
        "structure tree audit",
    )
    if (
        raw["schema_name"] != STRUCTURE_TREE_AUDIT_SCHEMA_NAME
        or raw["schema_version"] != STRUCTURE_TREE_AUDIT_SCHEMA_VERSION
        or raw["manifest_fingerprint"] != expected["manifest_fingerprint"]
    ):
        raise ValueError("structure tree audit identity is unsupported or foreign")
    source = _object(
        raw["source_identity"], {"comsol_build", "source_sha256", "tree_sha256"}, "source_identity"
    )
    observed_source = {
        "comsol_build": _text(source["comsol_build"], "source_identity.comsol_build", maximum=64),
        "source_sha256": _sha(source["source_sha256"], "source_identity.source_sha256"),
        "tree_sha256": _sha(source["tree_sha256"], "source_identity.tree_sha256"),
    }
    features = _features(raw["features"], "features")
    fixed = _fixed(raw["fixed_contracts"], "fixed_contracts")
    topology = _topology(raw["topology"], "topology")
    if (
        observed_source != expected["source_identity"]
        or features != expected["required_features"]
        or fixed != expected["fixed_contracts"]
        or topology != expected["topology_invariants"]
    ):
        raise ValueError("live model tree does not match the accepted structure adapter manifest")
    body = {
        "schema_name": STRUCTURE_TREE_AUDIT_SCHEMA_NAME,
        "schema_version": STRUCTURE_TREE_AUDIT_SCHEMA_VERSION,
        "manifest_fingerprint": expected["manifest_fingerprint"],
        "source_identity": observed_source,
        "features": features,
        "fixed_contracts": fixed,
        "topology": topology,
        "accepted": True,
    }
    fingerprint = domain_sha256_v2(STRUCTURE_TREE_AUDIT_SCHEMA_NAME, body)
    if supplied is not None and supplied != fingerprint:
        raise ValueError("structure tree audit fingerprint is invalid")
    return {**body, "audit_fingerprint": fingerprint}


__all__ = [
    "PERIODIC_MIM_PATCH_ADAPTER_ID",
    "STRUCTURE_ADAPTER_MANIFEST_SCHEMA_NAME",
    "STRUCTURE_ADAPTER_MANIFEST_SCHEMA_VERSION",
    "STRUCTURE_TREE_AUDIT_SCHEMA_NAME",
    "STRUCTURE_TREE_AUDIT_SCHEMA_VERSION",
    "normalize_structure_adapter_manifest",
    "normalize_structure_tree_audit",
]
