"""Solver-free binding tests for the trusted robust periodic-MIM shape adapter."""

from __future__ import annotations

import copy
import math

import pytest

from comsol_mcp.research.robust_shape_adapter import (
    compile_robust_shape_adapter_binding,
    prepare_robust_shape_controls,
)
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


class _Backend:
    def __init__(self, *, failure: str | None = None):
        self.state = {"nodes": []}
        self.failure = failure

    def snapshot(self):
        return copy.deepcopy(self.state)

    def restore(self, snapshot):
        self.state = copy.deepcopy(snapshot)
        if self.failure == "restore":
            self.state["nodes"] = ["residue"]

    def prepare_controls(self, _support):
        self.state["nodes"] = ["dg_a71", "free_a71", "fix_a71", "patch_a71"]
        if self.failure in {"prepare", "restore"}:
            raise ValueError("injected control failure")
        return {
            "parameters": {
                "patch_length_x": "856[nm]",
                "patch_length_y": "800[nm]",
            },
            "patch_size_before": [856e-9, 800e-9, 100e-9],
            "patch_size_readback": [856e-9, 800e-9, 100e-9],
            "deformed_geometry": {
                "physics_tag": "dg_a71",
                "physics_type": "DeformedGeometry",
                "free_domains": [1, 2],
                "fixed_outer_boundaries": [1, 2, 3, 4],
                "patch_boundaries": [5, 6],
                "patch_displacement": ["dx", "dy", "0"],
                "patch_domain": 1,
                "patch_footprint": [7],
            },
        }


def test_control_preparation_binds_exact_readback_and_changes_only_derived_state():
    manifest, audit, support, policy = _contracts()
    backend = _Backend()
    receipt = prepare_robust_shape_controls(backend, manifest, audit, support, policy)
    assert receipt["controls"]["deformed_geometry"]["free_domains"] == [1, 2]
    assert receipt["rollback"] == {"attempted": False, "verified": False}
    assert backend.state["nodes"][-1] == "patch_a71"
    assert len(receipt["receipt_fingerprint"]) == 64


def test_control_preparation_restores_complete_snapshot_after_failure():
    manifest, audit, support, policy = _contracts()
    backend = _Backend(failure="prepare")
    before = backend.snapshot()
    with pytest.raises(ValueError, match="injected control failure"):
        prepare_robust_shape_controls(backend, manifest, audit, support, policy)
    assert backend.snapshot() == before


def test_control_preparation_fails_closed_when_rollback_readback_differs():
    manifest, audit, support, policy = _contracts()
    backend = _Backend(failure="restore")
    with pytest.raises(RuntimeError, match="rollback was uncertain"):
        prepare_robust_shape_controls(backend, manifest, audit, support, policy)


class _DriftBackend(_Backend):
    """Backend that re-derives sizes with last-bit float noise."""

    def prepare_controls(self, support):
        result = super().prepare_controls(support)
        before = [float(value) for value in result["patch_size_before"]]
        readback = list(before)
        readback[0] = math.nextafter(readback[0], math.inf)
        result["patch_size_before"] = before
        result["patch_size_readback"] = readback
        return result


class _ChangedBackend(_Backend):
    """Backend whose readback reports a materially different patch size."""

    def prepare_controls(self, support):
        result = super().prepare_controls(support)
        result["patch_size_readback"] = [
            float(value) * 1.001 for value in result["patch_size_before"]
        ]
        return result


class _TextualBackend(_Backend):
    """Backend that echoes a size as a numeric string instead of a number."""

    def prepare_controls(self, support):
        result = super().prepare_controls(support)
        result["patch_size_readback"] = ["856e-9", 800e-9, 100e-9]
        return result


def test_control_readback_tolerates_last_bit_geometry_drift():
    manifest, audit, support, policy = _contracts()
    receipt = prepare_robust_shape_controls(_DriftBackend(), manifest, audit, support, policy)
    controls = receipt["controls"]
    assert controls["patch_size_readback"][0] == pytest.approx(
        controls["patch_size_before"][0], rel=1e-12
    )
    assert len(receipt["receipt_fingerprint"]) == 64


@pytest.mark.parametrize("backend_type", [_ChangedBackend, _TextualBackend])
def test_control_readback_rejects_material_drift_or_non_numeric_sizes(backend_type):
    manifest, audit, support, policy = _contracts()
    with pytest.raises(ValueError, match="baseline geometry changed"):
        prepare_robust_shape_controls(backend_type(), manifest, audit, support, policy)
