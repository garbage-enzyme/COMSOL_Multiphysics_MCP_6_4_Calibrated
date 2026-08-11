"""JobManager and synthetic robust shape worker lifecycle tests."""

from __future__ import annotations

import os

from comsol_mcp.jobs import robust_shape_worker
from comsol_mcp.jobs.manager import JobManager
from comsol_mcp.jobs.robust_shape_rows import append_robust_shape_row, read_robust_shape_rows
from comsol_mcp.jobs.robust_shape_worker import run as run_robust_worker
from comsol_mcp.jobs.store import process_identity, read_json
from development_kit.tests.test_robust_shape_optimization import _write_manifest


def _manager(root, monkeypatch):
    manager = JobManager(root, preflight=lambda **_kwargs: {"success": True, "ready": True})
    monkeypatch.setattr(
        manager,
        "_launch_worker",
        lambda _job_id, module: (
            process_identity(os.getpid())
            if module == "comsol_mcp.jobs.robust_shape_worker"
            else (_ for _ in ()).throw(AssertionError(module))
        ),
    )
    monkeypatch.setattr(
        robust_shape_worker,
        "collect_resource_telemetry",
        lambda **_kwargs: {
            "stage": "pre_mesh",
            "available_memory_bytes": 2 * 1024**3,
            "total_memory_bytes": 16 * 1024**3,
            "runtime_free_bytes": 200 * 1024**3,
        },
    )
    return manager


def test_manager_dispatches_synthetic_24_condition_job_without_solver(ascii_tmp_path, monkeypatch):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = _manager(ascii_tmp_path / "jobs", monkeypatch)
    submitted = manager.submit(envelope)
    spec = manager.store.read_spec(submitted["job_id"])
    state = manager.store.read_state(submitted["job_id"])
    assert state["progress"] == {"completed": 0, "total": 24}
    assert run_robust_worker(str(manager.store.root), submitted["job_id"]) == 0
    terminal = manager.status(submitted["job_id"])
    rows = read_robust_shape_rows(
        manager.store.job_dir(submitted["job_id"]) / "robust_shape_rows.jsonl",
        job_fingerprint=spec["spec_fingerprint"],
    )
    assert terminal["status"] == "completed"
    assert terminal["solver_started"] is False
    assert terminal["robust_shape_progress"] == {
        "declared_conditions": 24,
        "completed_conditions": 24,
        "pending_conditions": 0,
        "row_count": 28,
        "last_row_sha256": rows[-1]["row_sha256"],
        "cleanup_recorded": True,
    }
    assert [row["kind"] for row in rows[-4:]] == [
        "gradient",
        "iteration",
        "checkpoint",
        "cleanup",
    ]


def test_worker_replays_complete_condition_without_duplicate_row(ascii_tmp_path, monkeypatch):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = _manager(ascii_tmp_path / "jobs", monkeypatch)
    submitted = manager.submit(envelope)
    job_id = submitted["job_id"]
    spec = manager.store.read_spec(job_id)
    first_condition = spec["condition_table"]["conditions"][0]
    first = append_robust_shape_row(
        manager.store.job_dir(job_id) / "robust_shape_rows.jsonl",
        job_fingerprint=spec["spec_fingerprint"],
        attempt=1,
        kind="condition",
        payload={
            "iteration_id": "it-0",
            "condition_id": first_condition["condition_id"],
            "condition_order": first_condition["order"],
            "status": "completed",
            "observation_fingerprint": "a" * 64,
            "objective_contribution": 0.7,
            "reason_code": "preexisting_complete",
        },
    )
    assert run_robust_worker(str(manager.store.root), job_id) == 0
    rows = read_robust_shape_rows(
        manager.store.job_dir(job_id) / "robust_shape_rows.jsonl",
        job_fingerprint=spec["spec_fingerprint"],
    )
    matches = [
        row
        for row in rows
        if row["kind"] == "condition"
        and row["payload"]["condition_id"] == first_condition["condition_id"]
    ]
    assert len(matches) == 1
    assert matches[0]["row_sha256"] == first["row_sha256"]


def test_exact_duplicate_submission_reuses_existing_job(ascii_tmp_path, monkeypatch):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = _manager(ascii_tmp_path / "jobs", monkeypatch)
    first = manager.submit(envelope)
    second = manager.submit(envelope)
    assert second["duplicate"] is True
    assert second["job_id"] == first["job_id"]


def test_attempt_bound_cancel_records_cleanup_before_cooperative_observation(
    ascii_tmp_path, monkeypatch
):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = _manager(ascii_tmp_path / "jobs", monkeypatch)
    submitted = manager.submit(envelope)
    job_id = submitted["job_id"]
    manager.store.request_cancel(job_id, requester_identity=process_identity(os.getpid()))
    assert run_robust_worker(str(manager.store.root), job_id) == 0
    spec = manager.store.read_spec(job_id)
    rows = read_robust_shape_rows(
        manager.store.job_dir(job_id) / "robust_shape_rows.jsonl",
        job_fingerprint=spec["spec_fingerprint"],
    )
    assert [row["kind"] for row in rows] == ["cleanup"]
    assert all(rows[0]["payload"].values())
    state = manager.store.read_state(job_id)
    assert state["cancel"]["cooperative_observation"]["target_attempt"] == 1


def test_startup_resource_refusal_is_durable_and_solver_free(ascii_tmp_path, monkeypatch):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = _manager(ascii_tmp_path / "jobs", monkeypatch)
    monkeypatch.setattr(
        robust_shape_worker,
        "collect_resource_telemetry",
        lambda **_kwargs: {
            "stage": "pre_mesh",
            "available_memory_bytes": 1024**3 - 1,
            "total_memory_bytes": 16 * 1024**3,
            "runtime_free_bytes": 200 * 1024**3,
        },
    )
    submitted = manager.submit(envelope)
    job_id = submitted["job_id"]
    assert run_robust_worker(str(manager.store.root), job_id) == 1
    state = manager.store.read_state(job_id)
    receipt = read_json(manager.store.job_dir(job_id) / "startup-admission.json")
    assert state["status"] == "failed"
    assert state["solver_started"] is False
    assert receipt["decision"] == "refuse"
    assert receipt["checks"]["available_memory_meets_minimum"] is False
