"""Solver-free robust shape manifest and submission tests."""

from __future__ import annotations

import hashlib
import json

import pytest

from comsol_mcp.jobs.robust_shape_optimization import expand_robust_shape_manifest
from development_kit.tests.test_derivative_support import _support
from development_kit.tests.test_gradient_contracts import _optimizer
from development_kit.tests.test_robust_conditions import _table
from development_kit.tests.test_robust_gradient_acceptance import _policy as _gradient_policy
from development_kit.tests.test_robust_objectives import _configuration
from development_kit.tests.test_robust_optimizer_policy import _policy as _optimizer_policy
from development_kit.tests.test_robust_startup_admission import _policy as _startup_policy
from development_kit.tests.test_shape_support import _policy as _shape_policy


def _write_manifest(tmp_path, *, selected: str = "gcmma", synthetic: bool = True):
    source = tmp_path / "source.mph"
    source.write_bytes(b"synthetic robust source")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    support = _support()
    support["source_identity"] = source_hash
    optimizer = _optimizer()
    optimizer["budget"]["cores"] = 14
    optimizer["method"] = selected
    optimizer_policy = _optimizer_policy(selected)
    if selected == "mma":
        optimizer_policy["method_evidence"][1] = {
            "method": "mma",
            "support_state": "validated" if not synthetic else "structural_only",
            "evidence_sha256": "b" * 64,
        }
    manifest_body = {
        "schema_name": "comsol_mcp.robust_shape_optimization_manifest",
        "schema_version": "1.0.0",
        "source_model_path": str(source),
        "source_model_sha256": source_hash,
        "support": support,
        "condition_table": _table(),
        "objective": _configuration(),
        "shape_policy": _shape_policy(),
        "gradient_policy": _gradient_policy(),
        "optimizer_policy": optimizer_policy,
        "native_optimizer": optimizer,
        "startup_admission": _startup_policy(),
        "initial_values": [856.0],
        "synthetic_mode": synthetic,
    }
    manifest = tmp_path / "manifest.json"
    payload = json.dumps(manifest_body, sort_keys=True, separators=(",", ":")).encode()
    manifest.write_bytes(payload)
    envelope = {
        "job_type": "robust_shape_optimization",
        "submission_manifest_path": str(manifest),
        "submission_manifest_sha256": hashlib.sha256(payload).hexdigest(),
        "cores": 14,
        "version": "6.4",
        "resource_policy": {"max_mesh_elements": 300_000},
    }
    return envelope, source, manifest


def test_manifest_binds_every_robust_contract_and_uses_no_host_defaults(ascii_tmp_path):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    spec = expand_robust_shape_manifest(envelope)
    assert spec["job_type"] == "robust_shape_optimization"
    assert spec["condition_table"]["completeness"]["dimension_cardinalities"] == {
        "wavelength": 3,
        "incidence_elevation": 2,
        "incidence_azimuth": 1,
        "polarization_basis": 2,
        "material_state": 2,
    }
    assert spec["resource_policy"]["host_defaults_applied"] is False
    assert spec["shape_policy"]["mesh_admission"]["max_elements_per_model"] == 300_000
    assert spec["optimizer_policy"]["selected_method"] == "gcmma"
    assert spec["startup_admission"]["check_frequency"] == "startup_only"


def test_manifest_rejects_source_or_manifest_mutation(ascii_tmp_path):
    envelope, source, manifest = _write_manifest(ascii_tmp_path)
    source.write_bytes(b"changed")
    with pytest.raises(ValueError, match="source SHA-256"):
        expand_robust_shape_manifest(envelope)
    envelope, _, manifest = _write_manifest(ascii_tmp_path)
    manifest.write_bytes(manifest.read_bytes() + b" ")
    with pytest.raises(ValueError, match="manifest SHA-256"):
        expand_robust_shape_manifest(envelope)


def test_manifest_rejects_mesh_core_or_method_identity_drift(ascii_tmp_path):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    envelope["resource_policy"] = {"max_mesh_elements": 400_000}
    with pytest.raises(ValueError, match="mesh cap"):
        expand_robust_shape_manifest(envelope)
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    envelope["cores"] = 13
    with pytest.raises(ValueError, match="budget cores"):
        expand_robust_shape_manifest(envelope)
    envelope, _, manifest = _write_manifest(ascii_tmp_path)
    raw = json.loads(manifest.read_text())
    raw["optimizer_policy"]["selected_method"] = "mma"
    payload = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    manifest.write_bytes(payload)
    envelope["submission_manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    with pytest.raises(ValueError, match="method differs"):
        expand_robust_shape_manifest(envelope)


def test_real_mma_requires_validated_method_evidence(ascii_tmp_path):
    envelope, _, manifest = _write_manifest(ascii_tmp_path, selected="mma", synthetic=True)
    raw = json.loads(manifest.read_text())
    raw["synthetic_mode"] = False
    payload = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    manifest.write_bytes(payload)
    envelope["submission_manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    with pytest.raises(ValueError, match="lacks accepted execution evidence"):
        expand_robust_shape_manifest(envelope)
