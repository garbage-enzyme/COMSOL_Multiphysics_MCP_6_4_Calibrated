"""Solver-free injected detached spectral worker state-machine tests."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import time
import uuid

import pytest

from development_kit.tests.spectral_job_fixtures import (
    spectral_job_spec,
    write_fake_point_audit,
)
from src.jobs.spectral_worker import _run
from src.jobs.manager import JobManager
from src.jobs.store import JobStore, process_identity


class _Model:
    def __init__(self, source: str):
        self.source = source

    def name(self):
        return "fixture"


class _Client:
    port = None

    def __init__(self, source: str):
        self.source = source
        self.cleared = False

    def load(self, source: str):
        assert source == self.source
        return _Model(source)

    def clear(self):
        self.cleared = True


class _Ownership:
    def __init__(self):
        self.released = False

    def preflight(self, **_kwargs):
        return {"ready": True, "blockers": []}

    def acquire(self, **_kwargs):
        return {"success": True}

    def heartbeat(self, **_kwargs):
        return {"success": True}

    def release(self):
        self.released = True
        return {"success": True}


def _telemetry(stage, _point_id, _model, _directory, elapsed):
    return {
        "stage": stage,
        "observed_at_epoch": time.time(),
        "mesh_elements": 12,
        "elapsed_wall_seconds": elapsed,
    }


@pytest.fixture
def ascii_root():
    root = Path("D:/comsol_runtime_test") / f"pytest-spectral-{uuid.uuid4().hex}"
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _created_job(tmp_path, ascii_root):
    runtime = ascii_root / "runtime"
    store = JobStore(runtime / "jobs")
    spec = spectral_job_spec(tmp_path)
    now = time.time()
    state = {
        "schema_version": "2",
        "status": "submitted",
        "attempt": 1,
        "created_at_epoch": now,
        "updated_at_epoch": now,
        "worker_pid": os.getpid(),
        "worker_process_create_time": process_identity(os.getpid())["process_create_time"],
        "worker_command_signature": process_identity(os.getpid())["command_signature"],
        "progress": {"completed": 0, "total": spec["maximum_points"]},
        "last_error": None,
    }
    job_id = store.create(spec, state)
    return store, spec, job_id


def _raw_spec(spec):
    allowed = {
        "job_type",
        "source_model_path",
        "source_model_relative_identity",
        "configuration_sha256",
        "parameter_state",
        "wavelength_parameter",
        "initial_grid",
        "refinement_policy",
        "expansion_policy",
        "maximum_points",
        "collector",
        "analysis_policy",
        "measurement_configuration",
        "resource_policy",
        "cores",
        "version",
        "max_retries",
        "continue_on_error",
    }
    return {key: value for key, value in spec.items() if key in allowed}


def test_injected_worker_reuses_ownership_resource_and_cleanup_paths(tmp_path, ascii_root):
    store, spec, job_id = _created_job(tmp_path, ascii_root)
    ownership = _Ownership()
    client = _Client(spec["source_model_path"])

    def collect(point, _collector, artifact_dir):
        wavelength = point["wavelength"]["value"]
        absorption = 0.1 + 0.8 / (1.0 + ((wavelength - 5e-6) / 0.18e-6) ** 2)
        return write_fake_point_audit(
            artifact_dir, spec, point, absorption=absorption
        )

    code = _run(
        str(store.root),
        job_id,
        ownership_factory=lambda _root, _owner: ownership,
        client_factory=lambda _spec: client,
        collector_executor=collect,
        telemetry_provider=_telemetry,
        native_cancel_enabled=False,
    )
    state = store.read_state(job_id)
    assert code == 0
    assert state["status"] == "completed"
    assert state["spectral_summary"]["scientific_disposition"] == "accepted"
    assert state["cleanup"]["lease_released"] is True
    assert client.cleared is True
    assert ownership.released is True
    assert len(store.read_resource_journal(job_id)) > 0


def test_cleanup_fault_fails_attempt_but_still_releases_lease(tmp_path, ascii_root):
    store, spec, job_id = _created_job(tmp_path, ascii_root)
    ownership = _Ownership()

    def collect(point, _collector, artifact_dir):
        wavelength = point["wavelength"]["value"]
        absorption = 0.1 + 0.8 / (1.0 + ((wavelength - 5e-6) / 0.18e-6) ** 2)
        return write_fake_point_audit(
            artifact_dir, spec, point, absorption=absorption
        )

    code = _run(
        str(store.root),
        job_id,
        ownership_factory=lambda _root, _owner: ownership,
        client_factory=lambda _spec: _Client(spec["source_model_path"]),
        collector_executor=collect,
        telemetry_provider=_telemetry,
        native_cancel_enabled=False,
        fault_hook=lambda phase, _payload: (
            (_ for _ in ()).throw(RuntimeError("injected cleanup"))
            if phase == "during_cleanup"
            else None
        ),
    )
    state = store.read_state(job_id)
    assert code == 1
    assert state["status"] == "failed"
    assert "cleanup_hook" in state["last_error"]["message"]
    assert ownership.released is True
    assert (store.job_dir(job_id) / "analysis" / "summary.json").is_file()


def test_manager_routes_exact_spectral_submissions_and_changed_specs(tmp_path, ascii_root, monkeypatch):
    spec = spectral_job_spec(tmp_path)
    manager = JobManager(
        ascii_root / "manager-jobs",
        preflight=lambda **_kwargs: {"ready": True},
        reconcile_on_start=False,
    )
    launches = []

    def launch(job_id, module):
        launches.append((job_id, module))
        return process_identity(os.getpid())

    monkeypatch.setattr(manager, "_launch_worker", launch)
    first = manager.submit(_raw_spec(spec))
    duplicate = manager.submit(_raw_spec(spec))
    assert launches == [(first["job_id"], "comsol_mcp.jobs.spectral_worker")]
    assert duplicate["duplicate"] is True
    assert duplicate["job_id"] == first["job_id"]
    status = manager.status(first["job_id"])
    assert status["spectral_progress"]["maximum_points"] == spec["maximum_points"]
    assert status["spectral_progress"]["complete_points"] == 0

    changed = _raw_spec(spec)
    changed["configuration_sha256"] = "c" * 64
    second = manager.submit(changed)
    assert second["job_id"] != first["job_id"]
    assert launches[-1][1] == "comsol_mcp.jobs.spectral_worker"


def test_manager_resumes_spectral_worker_without_changing_spec(tmp_path, ascii_root, monkeypatch):
    spec = spectral_job_spec(tmp_path)
    manager = JobManager(
        ascii_root / "resume-jobs",
        preflight=lambda **_kwargs: {"ready": True},
        reconcile_on_start=False,
    )
    launches = []
    monkeypatch.setattr(
        manager,
        "_launch_worker",
        lambda job_id, module: (
            launches.append((job_id, module)) or process_identity(os.getpid())
        ),
    )
    submitted = manager.submit(_raw_spec(spec))
    manager.store.update_state(
        submitted["job_id"], "interrupted", event="injected_interruption"
    )
    resumed = manager.resume(submitted["job_id"])
    assert resumed["attempt"] == 2
    assert launches[-1][1] == "comsol_mcp.jobs.spectral_worker"
    assert manager.store.read_spec(submitted["job_id"])["spec_fingerprint"] == spec["spec_fingerprint"]
