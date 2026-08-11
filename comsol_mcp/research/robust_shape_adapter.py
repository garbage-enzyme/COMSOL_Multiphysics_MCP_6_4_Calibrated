"""Exact solver-free binding for the trusted robust periodic-MIM shape adapter."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .adapters import (
    PERIODIC_MIM_PATCH_ADAPTER_ID,
    normalize_structure_adapter_manifest,
    normalize_structure_tree_audit,
)
from .derivative_support import normalize_derivative_support
from .shape_support import normalize_shape_support_policy

ROBUST_SHAPE_ADAPTER_BINDING_SCHEMA_NAME = "comsol_mcp.robust_shape_adapter_binding"
ROBUST_SHAPE_ADAPTER_BINDING_SCHEMA_VERSION = "1.0.0"
_VARIABLE_IDS = ("patch_length_x", "patch_length_y")
_UNIT_TO_METRE = {"m": 1.0, "um": 1e-6, "nm": 1e-9}


def _metres(value: float, unit: str, name: str) -> float:
    try:
        scale = _UNIT_TO_METRE[unit]
    except KeyError as exc:
        raise ValueError(f"{name} uses an unsupported length unit") from exc
    result = float(value) * scale
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _variable_binding(
    support: Mapping[str, Any], manifest: Mapping[str, Any]
) -> list[dict[str, Any]]:
    support_variables = support["variables"]
    manifest_variables = manifest["mutable_dimensions"]
    if [item["variable_id"] for item in support_variables] != list(_VARIABLE_IDS):
        raise ValueError("trusted robust shape support must contain ordered patch_length_x/y")
    if [item["variable_id"] for item in manifest_variables] != list(_VARIABLE_IDS):
        raise ValueError("trusted structure manifest must contain ordered patch_length_x/y")
    result = []
    for index, (variable, dimension) in enumerate(
        zip(support_variables, manifest_variables, strict=True)
    ):
        if variable["order"] != index or dimension["property_index"] != index:
            raise ValueError("trusted robust shape variable property order changed")
        observed = {
            field: _metres(variable[field], variable["unit"], f"variables[{index}].{field}")
            for field in ("baseline", "lower", "upper")
        }
        expected = {field: float(dimension[field]) for field in observed}
        if any(
            not math.isclose(observed[field], expected[field], rel_tol=1e-12, abs_tol=1e-15)
            for field in observed
        ):
            raise ValueError("trusted robust shape variable bounds differ from structure manifest")
        result.append(
            {
                "variable_id": variable["variable_id"],
                "order": index,
                "support_unit": variable["unit"],
                "canonical_unit": "m",
                "baseline_m": observed["baseline"],
                "lower_m": observed["lower"],
                "upper_m": observed["upper"],
                "mapping_feature_tag": variable["mapping"]["feature_tag"],
                "mapping_feature_type": variable["mapping"]["feature_type"],
                "mapping_property_name": variable["mapping"]["property_name"],
                "mapping_property_index": variable["mapping"]["property_index"],
            }
        )
    return result


def compile_robust_shape_adapter_binding(
    structure_manifest: object,
    tree_audit: object,
    derivative_support: object,
    shape_policy: object,
) -> dict[str, Any]:
    """Bind inherited structure, derivative, and shape contracts before ClientAPI work."""
    manifest = normalize_structure_adapter_manifest(structure_manifest)
    audit = normalize_structure_tree_audit(tree_audit, manifest)
    support = normalize_derivative_support(derivative_support)
    policy = normalize_shape_support_policy(shape_policy)
    adapter_ids = {
        manifest["adapter_id"],
        manifest["structure_family"],
        support["adapter_id"],
        policy["adapter_id"],
    }
    if adapter_ids != {PERIODIC_MIM_PATCH_ADAPTER_ID}:
        raise ValueError("trusted robust shape adapter identity differs across contracts")
    if support["source_identity"] != manifest["source_identity"]["source_sha256"]:
        raise ValueError("trusted robust shape source identity differs across contracts")
    if support["comsol_build"] != manifest["source_identity"]["comsol_build"]:
        raise ValueError("trusted robust shape COMSOL build differs across contracts")
    variables = _variable_binding(support, manifest)
    body = {
        "schema_name": ROBUST_SHAPE_ADAPTER_BINDING_SCHEMA_NAME,
        "schema_version": ROBUST_SHAPE_ADAPTER_BINDING_SCHEMA_VERSION,
        "adapter_id": PERIODIC_MIM_PATCH_ADAPTER_ID,
        "adapter_version": support["adapter_version"],
        "source_sha256": support["source_identity"],
        "comsol_build": support["comsol_build"],
        "structure_manifest_fingerprint": manifest["manifest_fingerprint"],
        "tree_audit_fingerprint": audit["audit_fingerprint"],
        "support_fingerprint": support["support_fingerprint"],
        "shape_policy_fingerprint": policy["policy_fingerprint"],
        "topology_invariants": manifest["topology_invariants"],
        "variables": variables,
    }
    body["binding_fingerprint"] = domain_sha256_v2(ROBUST_SHAPE_ADAPTER_BINDING_SCHEMA_NAME, body)
    return body


__all__ = [
    "ROBUST_SHAPE_ADAPTER_BINDING_SCHEMA_NAME",
    "ROBUST_SHAPE_ADAPTER_BINDING_SCHEMA_VERSION",
    "compile_robust_shape_adapter_binding",
]
