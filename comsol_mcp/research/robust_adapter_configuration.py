"""Tagged robust-shape adapter configurations for MIM and Lin2025 fixtures."""

from __future__ import annotations

from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .adapters import (
    PERIODIC_MIM_PATCH_ADAPTER_ID,
    normalize_structure_adapter_manifest,
    normalize_structure_tree_audit,
)
from .derivative_support import _bounded_json, _object, normalize_derivative_support
from .lin2025_pedot_cylinder import (
    ADAPTER_ID as LIN2025_ADAPTER_ID,
)
from .lin2025_pedot_cylinder import (
    compile_lin2025_pedot_cylinder_binding,
    compile_lin2025_pedot_shape_support,
    normalize_lin2025_pedot_cylinder_fixture,
)
from .robust_material_tensor_rows import normalize_robust_material_tensor_rows
from .robust_shape_adapter import compile_robust_shape_adapter_binding
from .shape_support import normalize_shape_support_policy

SCHEMA_NAME = "comsol_mcp.robust_shape_adapter_configuration"
SCHEMA_VERSION = "1.0.0"


def normalize_robust_shape_adapter_configuration(
    value: object,
    derivative_support: object,
    shape_policy: object,
) -> dict[str, Any]:
    """Normalize and bind one explicit trusted adapter family."""
    bounded = _bounded_json(value, "robust shape adapter configuration", 2 * 1024 * 1024)
    supplied = None
    if isinstance(bounded, dict) and "configuration_fingerprint" in bounded:
        supplied = bounded.pop("configuration_fingerprint")
    raw = _object(
        bounded,
        {"schema_name", "schema_version", "adapter_id", "configuration"},
        "robust shape adapter configuration",
    )
    if raw["schema_name"] != SCHEMA_NAME or raw["schema_version"] != SCHEMA_VERSION:
        raise ValueError("robust shape adapter configuration schema is unsupported")
    support = normalize_derivative_support(derivative_support)
    policy = normalize_shape_support_policy(shape_policy)
    adapter_id = raw["adapter_id"]
    if adapter_id != support["adapter_id"] or adapter_id != policy["adapter_id"]:
        raise ValueError("robust shape adapter identity differs across contracts")
    configuration = raw["configuration"]
    if adapter_id == PERIODIC_MIM_PATCH_ADAPTER_ID:
        config = _object(
            configuration,
            {"structure_adapter_manifest", "structure_tree_audit"},
            "periodic MIM adapter configuration",
        )
        manifest = normalize_structure_adapter_manifest(config["structure_adapter_manifest"])
        audit = normalize_structure_tree_audit(config["structure_tree_audit"], manifest)
        binding = compile_robust_shape_adapter_binding(manifest, audit, support, policy)
        normalized_configuration = {
            "structure_adapter_manifest": manifest,
            "structure_tree_audit": audit,
        }
    elif adapter_id == LIN2025_ADAPTER_ID:
        config = _object(
            configuration,
            {"fixture", "tree_readback", "material_tensor_rows"},
            "Lin2025 adapter configuration",
        )
        fixture = normalize_lin2025_pedot_cylinder_fixture(config["fixture"])
        shape = compile_lin2025_pedot_shape_support(fixture, config["tree_readback"])
        binding = compile_lin2025_pedot_cylinder_binding(fixture, support, policy)
        if binding["source_sha256"] != shape["source_sha256"]:
            raise ValueError("Lin2025 binding and shape source identities differ")
        tensor_rows = normalize_robust_material_tensor_rows(config["material_tensor_rows"])
        normalized_configuration = {
            "fixture": fixture,
            "tree_readback": dict(config["tree_readback"]),
            "shape_support": shape,
            "material_tensor_rows": tensor_rows,
        }
    else:
        raise ValueError("robust shape adapter family is unsupported")
    body = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "adapter_id": adapter_id,
        "configuration": normalized_configuration,
        "binding": binding,
        "support_fingerprint": support["support_fingerprint"],
        "shape_policy_fingerprint": policy["policy_fingerprint"],
    }
    body["configuration_fingerprint"] = domain_sha256_v2(SCHEMA_NAME, body)
    if supplied is not None and supplied != body["configuration_fingerprint"]:
        raise ValueError("robust shape adapter configuration fingerprint is invalid")
    return body


__all__ = [
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "normalize_robust_shape_adapter_configuration",
]
