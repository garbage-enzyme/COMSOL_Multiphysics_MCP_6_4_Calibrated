from __future__ import annotations

import pytest

from comsol_mcp.research.robust_adapter_configuration import (
    SCHEMA_NAME,
    normalize_robust_shape_adapter_configuration,
)
from development_kit.tests.test_lin2025_pedot_backend import (
    _derivative_support,
    _fixture,
    _tree,
)
from development_kit.tests.test_robust_shape_adapter import _contracts
from development_kit.tests.test_shape_support import _policy


def test_tagged_configuration_preserves_periodic_mim_binding():
    manifest, audit, support, policy = _contracts()
    result = normalize_robust_shape_adapter_configuration(
        {
            "schema_name": SCHEMA_NAME,
            "schema_version": "1.0.0",
            "adapter_id": "periodic_mim_patch_v1",
            "configuration": {
                "structure_adapter_manifest": manifest,
                "structure_tree_audit": audit,
            },
        },
        support,
        policy,
    )
    assert result["binding"]["adapter_id"] == "periodic_mim_patch_v1"
    assert len(result["configuration_fingerprint"]) == 64


def test_tagged_configuration_binds_lin2025_fixture_tree_and_shape_support():
    policy = _policy()
    policy["adapter_id"] = "lin2025_pedot_cylinder_v1"
    result = normalize_robust_shape_adapter_configuration(
        {
            "schema_name": SCHEMA_NAME,
            "schema_version": "1.0.0",
            "adapter_id": "lin2025_pedot_cylinder_v1",
            "configuration": {"fixture": _fixture(), "tree_readback": _tree()},
        },
        _derivative_support(),
        policy,
    )
    assert result["binding"]["pedot_domain"] == 5
    assert result["configuration"]["shape_support"]["pedot_boundaries"] == [
        18, 19, 20, 23, 25, 27
    ]


def test_tagged_configuration_rejects_cross_contract_adapter_drift():
    policy = _policy()
    policy["adapter_id"] = "lin2025_pedot_cylinder_v1"
    with pytest.raises(ValueError, match="identity differs"):
        normalize_robust_shape_adapter_configuration(
            {
                "schema_name": SCHEMA_NAME,
                "schema_version": "1.0.0",
                "adapter_id": "periodic_mim_patch_v1",
                "configuration": {"fixture": _fixture(), "tree_readback": _tree()},
            },
            _derivative_support(),
            policy,
        )
