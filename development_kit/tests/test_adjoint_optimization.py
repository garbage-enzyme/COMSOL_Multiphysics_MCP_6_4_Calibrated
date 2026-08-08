"""Solver-free manifest and fake-worker contracts for adjoint jobs."""

import hashlib
import json

import pytest

from comsol_mcp.jobs.adjoint_optimization import (
    expand_adjoint_optimization_manifest,
    normalize_adjoint_optimization_submission,
)
from development_kit.tests.test_derivative_support import _support
from development_kit.tests.test_gradient_contracts import _optimizer


def _resource_policy() -> dict:
    return {"max_mesh_elements": 1000}


def _write_manifest(tmp_path):
    source = tmp_path / "source.mph"
    source.write_bytes(b"synthetic source")
    support = _support()
    support["source_identity"] = hashlib.sha256(source.read_bytes()).hexdigest()
    body = {
        "schema_name": "comsol_mcp.adjoint_optimization_manifest",
        "schema_version": "1.0.0",
        "source_model_path": str(source),
        "source_model_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "support": support,
        "optimizer": _optimizer(),
        "initial_values": [856.0],
        "synthetic_mode": True,
    }
    manifest = tmp_path / "manifest.json"
    payload = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    manifest.write_bytes(payload)
    envelope = {
        "job_type": "adjoint_optimization",
        "submission_manifest_path": str(manifest),
        "submission_manifest_sha256": hashlib.sha256(payload).hexdigest(),
        "cores": 3,
        "version": "6.4",
        "resource_policy": _resource_policy(),
    }
    return envelope, source, manifest


def test_submission_requires_explicit_ascii_manifest_and_resources(tmp_path):
    envelope, _, _ = _write_manifest(tmp_path)
    normalized = normalize_adjoint_optimization_submission(envelope)
    assert normalized["cores"] == 3
    assert normalized["resource_policy"]["host_defaults_applied"] is False
    missing = dict(envelope)
    missing.pop("cores")
    with pytest.raises(ValueError, match="fields"):
        normalize_adjoint_optimization_submission(missing)


def test_manifest_expansion_hashes_source_and_support_identity(tmp_path):
    envelope, source, _ = _write_manifest(tmp_path)
    spec = expand_adjoint_optimization_manifest(envelope)
    assert spec["source_model_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert spec["support"]["derivative_method"] == "adjoint"
    assert spec["synthetic_mode"] is True
    changed = dict(envelope)
    changed["submission_manifest_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="SHA-256"):
        expand_adjoint_optimization_manifest(changed)


def test_manifest_rejects_source_mutation_after_submission(tmp_path):
    envelope, source, _ = _write_manifest(tmp_path)
    source.write_bytes(b"changed")
    with pytest.raises(ValueError, match="source SHA-256"):
        expand_adjoint_optimization_manifest(envelope)
