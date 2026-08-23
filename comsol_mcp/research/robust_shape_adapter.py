"""Exact solver-free binding for the trusted robust periodic-MIM shape adapter."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Protocol

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
ROBUST_SHAPE_CONTROL_RECEIPT_SCHEMA_NAME = "comsol_mcp.robust_shape_control_receipt"
ROBUST_SHAPE_CONTROL_RECEIPT_SCHEMA_VERSION = "1.0.0"
_VARIABLE_IDS = ("patch_length_x", "patch_length_y")
_UNIT_TO_METRE = {"m": 1.0, "um": 1e-6, "nm": 1e-9}


class RobustShapeControlBackend(Protocol):
    """Minimal failure-atomic control preparation surface for one derived model."""

    def snapshot(self) -> Mapping[str, Any]: ...

    def restore(self, snapshot: Mapping[str, Any]) -> None: ...

    def prepare_controls(self, support: Mapping[str, Any]) -> Mapping[str, Any]: ...


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
        mapping = variable["mapping"]
        if (
            mapping["feature_tag"] != "patch_a71"
            or mapping["feature_type"] != "PrescribedMeshDisplacement"
            or mapping["property_name"] != "dx"
            or mapping["property_index"] != index
        ):
            raise ValueError("trusted robust shape control mapping changed")
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


def _control_readback(value: object, binding: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("trusted robust shape control readback must be an object")
    raw = dict(value)
    if set(raw) != {"parameters", "patch_size_before", "patch_size_readback", "deformed_geometry"}:
        raise ValueError("trusted robust shape control readback fields changed")
    parameters = raw["parameters"]
    expected_ids = [item["variable_id"] for item in binding["variables"]]
    if not isinstance(parameters, Mapping) or list(parameters) != expected_ids:
        raise ValueError("trusted robust shape control parameters changed")
    before = raw["patch_size_before"]
    readback = raw["patch_size_readback"]

    def _geometry_vector(value: object) -> list[float] | None:
        if not isinstance(value, list) or len(value) != 3:
            return None
        if any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            for item in value
        ):
            return None
        return [float(item) for item in value]

    before_sizes = _geometry_vector(before)
    readback_sizes = _geometry_vector(readback)
    if (
        before_sizes is None
        or readback_sizes is None
        or any(
            not math.isclose(observed, expected, rel_tol=1e-12, abs_tol=1e-15)
            for observed, expected in zip(readback_sizes, before_sizes, strict=True)
        )
    ):
        raise ValueError(
            "trusted robust shape baseline geometry changed during control preparation"
        )
    deformation = raw["deformed_geometry"]
    if not isinstance(deformation, Mapping):
        raise ValueError("trusted robust shape deformation readback must be an object")
    deformation = dict(deformation)
    expected_fields = {
        "physics_tag",
        "physics_type",
        "free_domains",
        "fixed_outer_boundaries",
        "patch_boundaries",
        "patch_displacement",
        "patch_domain",
        "patch_footprint",
    }
    if set(deformation) != expected_fields:
        raise ValueError("trusted robust shape deformation readback fields changed")
    if deformation["physics_tag"] != "dg_a71" or deformation["physics_type"] != "DeformedGeometry":
        raise ValueError("trusted robust shape deformation identity changed")
    free_domains = deformation["free_domains"]
    expected_domains = list(range(1, binding["topology_invariants"]["domain_count"] + 1))
    if free_domains != expected_domains:
        raise ValueError("trusted robust shape free-domain topology changed")
    fixed = deformation["fixed_outer_boundaries"]
    patch = deformation["patch_boundaries"]
    if (
        not isinstance(fixed, list)
        or not isinstance(patch, list)
        or not fixed
        or not patch
        or fixed != sorted(set(fixed))
        or patch != sorted(set(patch))
        or set(fixed) & set(patch)
    ):
        raise ValueError("trusted robust shape deformation selections changed")
    if deformation["patch_domain"] not in expected_domains:
        raise ValueError("trusted robust shape patch domain changed")
    footprint = deformation["patch_footprint"]
    if not isinstance(footprint, list) or len(footprint) != 1:
        raise ValueError("trusted robust shape patch footprint changed")
    displacement = deformation["patch_displacement"]
    if not isinstance(displacement, list) or len(displacement) != 3:
        raise ValueError("trusted robust shape displacement mapping changed")
    return {
        "parameters": dict(parameters),
        "patch_size_before": list(before),
        "patch_size_readback": list(readback),
        "deformed_geometry": {
            **deformation,
            "free_domains": list(free_domains),
            "fixed_outer_boundaries": list(fixed),
            "patch_boundaries": list(patch),
            "patch_displacement": list(displacement),
            "patch_footprint": list(footprint),
        },
    }


def prepare_robust_shape_controls(
    backend: RobustShapeControlBackend,
    structure_manifest: object,
    tree_audit: object,
    derivative_support: object,
    shape_policy: object,
) -> dict[str, Any]:
    """Create and verify trusted x/y controls with complete snapshot rollback."""
    support = normalize_derivative_support(derivative_support)
    binding = compile_robust_shape_adapter_binding(
        structure_manifest,
        tree_audit,
        support,
        shape_policy,
    )
    snapshot = dict(backend.snapshot())
    try:
        controls = _control_readback(backend.prepare_controls(support), binding)
        if dict(backend.snapshot()) == snapshot:
            raise ValueError("trusted robust shape control preparation produced no model change")
    except Exception as exc:
        rollback_error = None
        try:
            backend.restore(snapshot)
            if dict(backend.snapshot()) != snapshot:
                raise RuntimeError("robust shape control rollback readback differs from snapshot")
        except Exception as rollback_exc:
            rollback_error = f"{type(rollback_exc).__name__}: {rollback_exc}"
        if rollback_error is not None:
            raise RuntimeError(
                "robust shape control preparation failed and rollback was uncertain: "
                f"{rollback_error}"
            ) from exc
        raise
    body = {
        "schema_name": ROBUST_SHAPE_CONTROL_RECEIPT_SCHEMA_NAME,
        "schema_version": ROBUST_SHAPE_CONTROL_RECEIPT_SCHEMA_VERSION,
        "binding_fingerprint": binding["binding_fingerprint"],
        "support_fingerprint": support["support_fingerprint"],
        "controls": controls,
        "rollback": {"attempted": False, "verified": False},
    }
    body["receipt_fingerprint"] = domain_sha256_v2(ROBUST_SHAPE_CONTROL_RECEIPT_SCHEMA_NAME, body)
    return body


__all__ = [
    "ROBUST_SHAPE_ADAPTER_BINDING_SCHEMA_NAME",
    "ROBUST_SHAPE_ADAPTER_BINDING_SCHEMA_VERSION",
    "ROBUST_SHAPE_CONTROL_RECEIPT_SCHEMA_NAME",
    "ROBUST_SHAPE_CONTROL_RECEIPT_SCHEMA_VERSION",
    "RobustShapeControlBackend",
    "compile_robust_shape_adapter_binding",
    "prepare_robust_shape_controls",
]
