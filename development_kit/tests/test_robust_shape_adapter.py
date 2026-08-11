"""Solver-free binding tests for the trusted robust periodic-MIM shape adapter."""

from __future__ import annotations

import copy

import pytest

from comsol_mcp.research.robust_shape_adapter import compile_robust_shape_adapter_binding
from development_kit.tests.test_derivative_support import _support, _variable
from development_kit.tests.test_research_adapters import _audit, _manifest
from development_kit.tests.test_shape_support import _policy


def _contracts() -> tuple[dict, dict, dict, dict]:
    manifest = _manifest()
    baseline_x = 856e-9
    baseline_y = 800e-9
    manifest["mutable_dimensions"][0].update(
        baseline=baseline_x, lower=0.75 * baseline_x, upper=1.25 * baseline_x
    )
    manifest["mutable_dimensions"][1].update(
        baseline=baseline_y, lower=0.75 * baseline_y, upper=1.25 * baseline_y
    )
    support = _support()
    support["variables"] = [_variable(0), _variable(1, "patch_length_y")]
    support["variables"][0]["mapping"].update(
        feature_tag="patch_a71",
        feature_type="PrescribedMeshDisplacement",
        property_name="dx",
        readback_expression="patch_length_x",
    )
    support["variables"][1].update(
        baseline=800.0,
        lower=600.0,
        upper=1000.0,
        scale=800.0,
    )
    support["variables"][1]["mapping"].update(
        feature_tag="patch_a71",
        feature_type="PrescribedMeshDisplacement",
        property_name="dx",
        readback_expression="patch_length_y",
    )
    audit = _audit(manifest)
    return manifest, audit, support, _policy()


def test_binding_freezes_source_tree_topology_policy_and_xy_variables():
    manifest, audit, support, policy = _contracts()
    result = compile_robust_shape_adapter_binding(manifest, audit, support, policy)
    assert result["adapter_id"] == "periodic_mim_patch_v1"
    assert result["source_sha256"] == "a" * 64
    assert [item["variable_id"] for item in result["variables"]] == [
        "patch_length_x",
        "patch_length_y",
    ]
    assert result["variables"][0]["baseline_m"] == pytest.approx(856e-9)
    assert result["variables"][1]["upper_m"] == pytest.approx(1000e-9)
    assert len(result["binding_fingerprint"]) == 64


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("source", "source identity"),
        ("build", "COMSOL build"),
        ("variable", "bounds differ"),
        ("missing_y", "ordered patch_length_x/y"),
        ("tree", "does not match"),
    ],
)
def test_binding_rejects_cross_contract_drift_before_clientapi(field, message):
    manifest, audit, support, policy = _contracts()
    if field == "source":
        support["source_identity"] = "0" * 64
    elif field == "build":
        support["comsol_build"] = "6.4.0.999"
    elif field == "variable":
        support["variables"][1]["upper"] = 999.0
    elif field == "missing_y":
        support["variables"] = support["variables"][:1]
    else:
        audit = copy.deepcopy(audit)
        audit["topology"]["boundary_count"] += 1
    with pytest.raises(ValueError, match=message):
        compile_robust_shape_adapter_binding(manifest, audit, support, policy)
