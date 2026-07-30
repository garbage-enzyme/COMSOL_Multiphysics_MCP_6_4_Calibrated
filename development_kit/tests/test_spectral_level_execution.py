"""Solver-free loaded-model spectral execution adapter tests."""

from __future__ import annotations

import time
from pathlib import Path
import shutil
import uuid

import pytest

from development_kit.tests.spectral_job_fixtures import spectral_job_spec, write_fake_point_audit
from src.jobs.spectral_level_execution import execute_loaded_spectral_level
from src.jobs.store import JobStore


@pytest.fixture
def ascii_jobs_root():
    root = Path("D:/comsol_runtime_test") / f"spectral-level-{uuid.uuid4().hex}"
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


class _Ownership:
    def __init__(self):
        self.heartbeats = []

    def heartbeat(self, **kwargs):
        self.heartbeats.append(kwargs)
        return {"success": True}


def _telemetry(stage, point_id, model, directory, elapsed):
    return {
        "stage": stage,
        "observed_at_epoch": time.time(),
        "mesh_elements": 12,
        "elapsed_wall_seconds": elapsed,
    }


def _state(total):
    now = time.time()
    return {
        "schema_version": "2", "status": "running", "attempt": 1,
        "created_at_epoch": now, "updated_at_epoch": now,
        "worker_pid": None, "worker_process_create_time": None,
        "worker_command_signature": None,
        "progress": {"completed": 0, "total": total}, "last_error": None,
    }


def test_loaded_level_composes_resource_checks_collector_and_spectral_runner(tmp_path, ascii_jobs_root):
    source_root = tmp_path / "source"
    source_root.mkdir()
    spec = spectral_job_spec(source_root, maximum_points=10)
    store = JobStore(ascii_jobs_root)
    job_id = store.create(spec, _state(10))
    directory = store.job_dir(job_id) / "level"
    rows = []
    ownership = _Ownership()

    def collector(point, _collector, artifact_dir):
        wavelength = point["wavelength"]["value"]
        coordinate = (wavelength - 5e-6) / 0.4e-6
        return write_fake_point_audit(
            artifact_dir,
            spec,
            point,
            absorption=0.1 + 0.8 / (1.0 + coordinate * coordinate),
        )

    output = execute_loaded_spectral_level(
        store=store, job_id=job_id, spec=spec, directory=directory, attempt=1,
        model=object(), client=object(), model_name="fixture", ownership=ownership,
        preflight={"ready": True}, worker_started=time.monotonic(),
        should_stop=lambda: False, on_durable_row=lambda row: rows.append(row),
        collector_executor=collector, telemetry_provider=_telemetry,
    )

    assert output["result"]["completed"] is True
    assert output["result"]["progress"]["scientific_disposition"] == "accepted"
    assert len(rows) == output["result"]["progress"]["row_count"]
    assert len(ownership.heartbeats) == len(rows)
    assert output["latest_resource_decision"]["action"] == "start_point"


def test_loaded_level_stops_before_collector_when_control_requests_cancel(tmp_path, ascii_jobs_root):
    source_root = tmp_path / "source"
    source_root.mkdir()
    spec = spectral_job_spec(source_root, maximum_points=10)
    store = JobStore(ascii_jobs_root)
    job_id = store.create(spec, _state(10))
    called = []
    output = execute_loaded_spectral_level(
        store=store, job_id=job_id, spec=spec,
        directory=store.job_dir(job_id) / "level", attempt=1,
        model=object(), client=object(), model_name="fixture", ownership=_Ownership(),
        preflight={"ready": True}, worker_started=time.monotonic(),
        should_stop=lambda: True, on_durable_row=lambda row: called.append(row),
        collector_executor=lambda *args: (_ for _ in ()).throw(AssertionError("collector started")),
        telemetry_provider=_telemetry,
    )

    assert output["result"]["completed"] is False
    assert output["result"]["stop_reason"] == "before_solve_cancel"
    assert called == []


def test_loaded_level_reports_mesh_telemetry_failure_that_blocks_admission(
    tmp_path, ascii_jobs_root, monkeypatch
):
    from src.tools import mesh as mesh_module

    source_root = tmp_path / "source"
    source_root.mkdir()
    spec = spectral_job_spec(source_root, maximum_points=10)
    store = JobStore(ascii_jobs_root)
    job_id = store.create(spec, _state(10))
    monkeypatch.setattr(
        mesh_module,
        "get_mesh_info",
        lambda _model: (_ for _ in ()).throw(RuntimeError("backend unavailable")),
    )
    output = execute_loaded_spectral_level(
        store=store, job_id=job_id, spec=spec,
        directory=store.job_dir(job_id) / "level", attempt=1,
        model=object(), client=object(), model_name="fixture", ownership=_Ownership(),
        preflight={"ready": True}, worker_started=time.monotonic(),
        should_stop=lambda: False, on_durable_row=lambda _row: None,
        collector_executor=lambda *_args: pytest.fail("collector must not start"),
    )

    assert output["result"]["completed"] is False
    assert output["telemetry_diagnostics"][0]["code"] == "mesh_telemetry_failed"
    assert output["telemetry_diagnostics"][0]["error_type"] == "RuntimeError"
    assert output["latest_resource_decision"]["telemetry_diagnostics"]
