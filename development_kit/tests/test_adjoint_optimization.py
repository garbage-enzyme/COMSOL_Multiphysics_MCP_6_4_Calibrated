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
from comsol_mcp.jobs.store import JobStore, process_identity, read_json
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


def test_submission_normalization_is_idempotent_over_its_own_output(ascii_tmp_path):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    once = normalize_adjoint_optimization_submission(envelope)
    twice = normalize_adjoint_optimization_submission(once)
    assert twice == once

    mismatched = dict(once)
    mismatched["schema_version"] = "9.9.9"
    with pytest.raises(ValueError, match="schema version is unsupported"):
        normalize_adjoint_optimization_submission(mismatched)

    foreign_name = dict(once)
    foreign_name["schema_name"] = "other.submission"
    with pytest.raises(ValueError, match="schema name is unsupported"):
        normalize_adjoint_optimization_submission(foreign_name)

    expanded_once = expand_adjoint_optimization_manifest(once)
    expanded_twice = expand_adjoint_optimization_manifest(twice)
    assert expanded_twice == expanded_once


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


def test_manifest_rejects_oversized_or_non_numeric_initial_values(ascii_tmp_path):
    envelope, _, manifest = _write_manifest(ascii_tmp_path)

    def rewrite(mutate):
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        mutate(raw)
        payload = json.dumps(
            raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        manifest.write_bytes(payload)
        envelope["submission_manifest_sha256"] = hashlib.sha256(payload).hexdigest()

    def set_values(raw, values):
        raw["initial_values"] = values

    rewrite(lambda raw: set_values(raw, [True]))
    with pytest.raises(ValueError, match="must be a number"):
        expand_adjoint_optimization_manifest(envelope)
    rewrite(lambda raw: set_values(raw, ["856.0"]))
    with pytest.raises(ValueError, match="must be a number"):
        expand_adjoint_optimization_manifest(envelope)
    rewrite(lambda raw: set_values(raw, [None]))
    with pytest.raises(ValueError, match="must be a number"):
        expand_adjoint_optimization_manifest(envelope)


def test_manifest_rejects_oversized_submission_before_reading(ascii_tmp_path):
    envelope, _, manifest = _write_manifest(ascii_tmp_path)
    manifest.write_bytes(b"x" * (513 * 1024))
    with pytest.raises(ValueError, match="byte limit"):
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
    assert not (manager.store.job_dir(result["job_id"]) / "wall-watchdog.json").exists()
    assert spec["optimizer"]["budget"]["max_iterations"] == 10
    assert manager.store.read_state(result["job_id"])["progress"] == {
        "completed": 0,
        "total": 10,
    }
    assert run_adjoint_worker(str(manager.store.root), result["job_id"]) == 0
    terminal = manager.store.read_state(result["job_id"])
    assert terminal["status"] == "completed"
    assert terminal["solver_started"] is False


def test_synthetic_worker_writes_rows_for_its_own_attempt_after_prior_attempt_rows(
    ascii_tmp_path, monkeypatch
):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = JobManager(
        ascii_tmp_path / "jobs",
        preflight=lambda **_kwargs: {"success": True, "ready": True},
    )
    monkeypatch.setattr(
        manager,
        "_launch_worker",
        lambda _job_id, _module: process_identity(os.getpid()),
    )
    result = manager.submit(envelope)
    job_id = result["job_id"]
    store = manager.store

    def broken_identity(self, *_args, **_kwargs):
        raise OSError("injected identity failure")

    monkeypatch.setattr(JobStore, "bind_worker_identity", broken_identity)
    assert run_adjoint_worker(str(store.root), job_id) == 1
    store.update_state(job_id, "starting", event="retry_starting")
    rows_path = store.job_dir(job_id) / "optimization_rows.jsonl"
    spec_fingerprint = store.read_spec(job_id)["spec_fingerprint"]
    from comsol_mcp.jobs.adjoint_rows import append_adjoint_row, read_adjoint_rows

    foreign_attempt = 9
    append_adjoint_row(
        rows_path,
        job_fingerprint=spec_fingerprint,
        attempt=foreign_attempt,
        kind="gradient",
        payload={
            "iteration_id": "it-0",
            "gradient_fingerprint": "a" * 64,
            "check_fingerprint": "b" * 64,
            "evidence_state": "gradient_validated",
        },
    )
    monkeypatch.setattr(JobStore, "bind_worker_identity", lambda self, *a, **k: None)
    assert run_adjoint_worker(str(store.root), job_id) == 0
    all_rows = read_adjoint_rows(rows_path, job_fingerprint=spec_fingerprint)
    assert any(row["attempt"] == foreign_attempt for row in all_rows)
    current_rows = [row for row in all_rows if row["attempt"] != foreign_attempt]
    assert {"gradient", "iteration"} <= {row["kind"] for row in current_rows}


def test_synthetic_worker_failure_reaches_terminal_state_instead_of_propagating(
    ascii_tmp_path, monkeypatch
):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = JobManager(
        ascii_tmp_path / "jobs",
        preflight=lambda **_kwargs: {"success": True, "ready": True},
    )
    monkeypatch.setattr(
        manager,
        "_launch_worker",
        lambda _job_id, _module: process_identity(os.getpid()),
    )
    result = manager.submit(envelope)
    job_id = result["job_id"]
    store = manager.store

    def broken_identity(self, *_args, **_kwargs):
        raise OSError("injected identity failure")

    monkeypatch.setattr(JobStore, "bind_worker_identity", broken_identity)

    assert run_adjoint_worker(str(store.root), job_id) == 1

    terminal = store.read_state(job_id)
    assert terminal["status"] == "failed"
    assert terminal["last_error"]["type"] == "OSError"
    assert "injected identity failure" in terminal["last_error"]["message"]


def test_synthetic_worker_exception_during_cancelling_records_cooperative_cancel(
    ascii_tmp_path, monkeypatch
):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = JobManager(
        ascii_tmp_path / "jobs",
        preflight=lambda **_kwargs: {"success": True, "ready": True},
    )
    monkeypatch.setattr(
        manager,
        "_launch_worker",
        lambda _job_id, _module: process_identity(os.getpid()),
    )
    result = manager.submit(envelope)
    job_id = result["job_id"]
    store = manager.store
    store.request_cancel(job_id, requester_identity=process_identity(os.getpid()))

    def broken_identity(self, *_args, **_kwargs):
        raise OSError("injected cancel-window failure")

    monkeypatch.setattr(JobStore, "bind_worker_identity", broken_identity)

    observed = {}

    def record_observed(self, job_id_arg, *, attempt, message, worker_error):
        observed["attempt"] = attempt
        observed["message"] = message
        observed["worker_error"] = worker_error

    monkeypatch.setattr(JobStore, "record_cooperative_cancel_observed", record_observed)

    assert run_adjoint_worker(str(store.root), job_id) == 0
    assert observed["message"] == "Stopped during synthetic validation"
    assert observed["worker_error"]["type"] == "OSError"
    assert store.read_state(job_id)["status"] != "failed"


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


def _real_adjoint_envelope(ascii_tmp_path):
    envelope, _, manifest = _write_manifest(ascii_tmp_path)
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["synthetic_mode"] = False
    payload = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    manifest.write_bytes(payload)
    envelope["submission_manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    return envelope


def test_real_adjoint_submission_arms_the_wall_watchdog(ascii_tmp_path):
    envelope = _real_adjoint_envelope(ascii_tmp_path)
    manager = JobManager(
        ascii_tmp_path / "jobs",
        preflight=lambda **_kwargs: {"success": True, "ready": True},
    )
    result = manager.submit(envelope)

    record = read_json(manager.store.job_dir(result["job_id"]) / "wall-watchdog.json")
    assert record["status"] == "armed"
    assert record["budget_source"] == "optimizer.budget.max_wall_time_seconds"
    assert record["budget_seconds"] == 3600


def test_native_adjoint_worker_fails_closed_without_armed_watchdog(ascii_tmp_path, monkeypatch):
    envelope = _real_adjoint_envelope(ascii_tmp_path)
    manager = JobManager(
        ascii_tmp_path / "jobs",
        preflight=lambda **_kwargs: {"success": True, "ready": True},
    )
    monkeypatch.setattr(
        manager, "_launch_worker", lambda _job_id, _module: process_identity(os.getpid())
    )
    result = manager.submit(envelope)
    job_id = result["job_id"]
    (manager.store.job_dir(job_id) / "wall-watchdog.json").unlink()

    assert run_adjoint_worker(str(manager.store.root), job_id) == 1

    terminal = manager.store.read_state(job_id)
    assert terminal["status"] == "failed"
    assert "was not armed" in terminal["last_error"]["message"]


def _minimal_adjoint_runtime_spec(tmp_path):
    source = tmp_path / "source.mph"
    source.write_bytes(b"adjoint runtime fixture")
    return {
        "source_model_path": str(source),
        "source_model_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "support": {"support_fingerprint": "a" * 64},
        "optimizer": {
            "method": "gcmma",
            "optimizer_fingerprint": "b" * 64,
            "budget": {"cores": 1, "max_solves": 5, "max_wall_time_seconds": 60},
        },
        "cores": 1,
        "version": "6.4",
    }


def test_cleanup_failure_records_errors_and_preserves_original_error(ascii_tmp_path):
    class FailingClearClient:
        def load(self, _path):
            raise RuntimeError("boom")

        def clear(self):
            raise OSError("clear failed")

    spec = _minimal_adjoint_runtime_spec(ascii_tmp_path)
    workdir = ascii_tmp_path / "runtime-work"

    with pytest.raises(RuntimeError, match="boom"):
        native_adjoint_runtime.execute_native_adjoint_optimization(
            spec,
            workdir,
            client_factory=lambda **_kwargs: FailingClearClient(),
        )

    receipt = json.loads((workdir / "native-optimizer-receipt.json").read_text(encoding="utf-8"))
    assert receipt["success"] is False
    assert receipt["cleanup"]["client_clear"] is False
    assert receipt["cleanup"]["source_unchanged"] is True
    assert receipt["cleanup"]["cleanup_errors"] == ["client_clear:OSError:clear failed"]


def test_clean_failure_receipt_omits_empty_cleanup_errors(ascii_tmp_path):
    class CleanClient:
        def load(self, _path):
            raise RuntimeError("boom")

        def clear(self):
            return None

    spec = _minimal_adjoint_runtime_spec(ascii_tmp_path)
    workdir = ascii_tmp_path / "runtime-clean"

    with pytest.raises(RuntimeError, match="boom"):
        native_adjoint_runtime.execute_native_adjoint_optimization(
            spec,
            workdir,
            client_factory=lambda **_kwargs: CleanClient(),
        )

    receipt = json.loads((workdir / "native-optimizer-receipt.json").read_text(encoding="utf-8"))
    assert receipt["success"] is False
    assert receipt["cleanup"] == {"client_clear": True, "source_unchanged": True}
    assert "cleanup_errors" not in receipt["cleanup"]
