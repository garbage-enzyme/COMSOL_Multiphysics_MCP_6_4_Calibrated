"""Solver-free manifest and fake-worker contracts for adjoint jobs."""

import hashlib
import json
import os
import sys
from types import SimpleNamespace

import pytest

from comsol_mcp.jobs import native_adjoint_runtime
from comsol_mcp.jobs.adjoint_optimization import (
    expand_adjoint_optimization_manifest,
    normalize_adjoint_optimization_submission,
)
from comsol_mcp.jobs.adjoint_optimization_worker import run as run_adjoint_worker
from comsol_mcp.jobs.adjoint_rows import read_adjoint_rows
from comsol_mcp.jobs.manager import JobManager
from comsol_mcp.jobs.store import process_identity
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


def test_submission_requires_explicit_ascii_manifest_and_resources(ascii_tmp_path):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    normalized = normalize_adjoint_optimization_submission(envelope)
    assert normalized["cores"] == 3
    assert normalized["resource_policy"]["host_defaults_applied"] is False
    missing = dict(envelope)
    missing.pop("cores")
    with pytest.raises(ValueError, match="fields"):
        normalize_adjoint_optimization_submission(missing)


def test_manifest_expansion_hashes_source_and_support_identity(ascii_tmp_path):
    envelope, source, _ = _write_manifest(ascii_tmp_path)
    spec = expand_adjoint_optimization_manifest(envelope)
    assert spec["source_model_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert spec["support"]["derivative_method"] == "adjoint"
    assert spec["synthetic_mode"] is True
    changed = dict(envelope)
    changed["submission_manifest_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="SHA-256"):
        expand_adjoint_optimization_manifest(changed)


def test_manifest_rejects_source_mutation_after_submission(ascii_tmp_path):
    envelope, source, _ = _write_manifest(ascii_tmp_path)
    source.write_bytes(b"changed")
    with pytest.raises(ValueError, match="source SHA-256"):
        expand_adjoint_optimization_manifest(envelope)


def test_manifest_rejects_unvalidated_method_or_mismatched_core_budget(ascii_tmp_path):
    envelope, _, manifest = _write_manifest(ascii_tmp_path)
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["optimizer"]["method"] = "mma"
    payload = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    manifest.write_bytes(payload)
    envelope["submission_manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    with pytest.raises(ValueError, match="only validated gcmma"):
        expand_adjoint_optimization_manifest(envelope)

    raw["optimizer"]["method"] = "gcmma"
    raw["optimizer"]["budget"]["cores"] = 2
    payload = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    manifest.write_bytes(payload)
    envelope["submission_manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    with pytest.raises(ValueError, match="budget cores"):
        expand_adjoint_optimization_manifest(envelope)


def test_durable_manager_accepts_synthetic_adjoint_discriminator(ascii_tmp_path, monkeypatch):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = JobManager(
        ascii_tmp_path / "jobs",
        preflight=lambda **_kwargs: {"success": True, "ready": True},
    )
    monkeypatch.setattr(
        manager,
        "_launch_worker",
        lambda _job_id, module: (
            process_identity(os.getpid())
            if module == "comsol_mcp.jobs.adjoint_optimization_worker"
            else (_ for _ in ()).throw(AssertionError(module))
        ),
    )
    result = manager.submit(envelope)
    spec = manager.store.read_spec(result["job_id"])
    assert spec["job_type"] == "adjoint_optimization"
    assert spec["synthetic_mode"] is True
    assert spec["optimizer"]["budget"]["max_iterations"] == 10
    assert manager.store.read_state(result["job_id"])["progress"] == {
        "completed": 0,
        "total": 10,
    }
    assert run_adjoint_worker(str(manager.store.root), result["job_id"]) == 0
    terminal = manager.store.read_state(result["job_id"])
    assert terminal["status"] == "completed"
    assert terminal["solver_started"] is False


def test_real_adjoint_worker_dispatches_validated_runtime_and_persists_rows(
    ascii_tmp_path, monkeypatch
):
    envelope, _, manifest = _write_manifest(ascii_tmp_path)
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["synthetic_mode"] = False
    payload = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    manifest.write_bytes(payload)
    envelope["submission_manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    manager = JobManager(
        ascii_tmp_path / "jobs",
        preflight=lambda **_kwargs: {"success": True, "ready": True},
    )
    monkeypatch.setattr(
        manager, "_launch_worker", lambda _job_id, _module: process_identity(os.getpid())
    )
    result = manager.submit(envelope)

    class Ownership:
        def __init__(self, *_args, **_kwargs):
            pass

        def preflight(self, **_kwargs):
            return {"ready": True}

        def acquire(self, **_kwargs):
            return {"success": True}

        def release(self):
            return {"success": True}

    monkeypatch.setitem(
        sys.modules,
        "comsol_mcp.tools.ownership",
        SimpleNamespace(SolverOwnership=Ownership),
    )
    monkeypatch.setattr(
        native_adjoint_runtime,
        "execute_native_adjoint_optimization",
        lambda _spec, _directory: {
            "success": True,
            "final_variables_si": {"patch_length_x": 8.0e-7},
            "fresh_forward_objective": 0.6,
            "cleanup": {"source_unchanged": True, "client_clear": True},
        },
    )

    assert run_adjoint_worker(str(manager.store.root), result["job_id"]) == 0
    terminal = manager.store.read_state(result["job_id"])
    rows = manager.store.job_dir(result["job_id"]) / "optimization_rows.jsonl"
    persisted = read_adjoint_rows(
        rows,
        job_fingerprint=manager.store.read_spec(result["job_id"])["spec_fingerprint"],
    )
    assert terminal["status"] == "completed"
    assert terminal["solver_started"] is True
    assert [row["kind"] for row in persisted] == ["gradient", "iteration"]
