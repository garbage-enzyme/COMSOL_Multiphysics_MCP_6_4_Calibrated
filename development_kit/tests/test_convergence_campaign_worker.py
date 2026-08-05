"""Injected durable convergence worker ownership, recovery, and cleanup tests."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import src.jobs.convergence_campaign_worker as worker_module
from src.jobs.convergence_campaign import normalize_convergence_campaign_spec
from src.jobs.convergence_campaign_rows import read_convergence_campaign_levels
from src.jobs.convergence_campaign_runner import convergence_level_directory
from src.jobs.convergence_campaign_worker import _run
from src.jobs.manager import JobManager
from src.jobs.spectral_rows import read_spectral_rows
from src.jobs.store import JobStore, atomic_write_json, process_identity

from development_kit.tests.spectral_job_fixtures import write_fake_point_audit
from development_kit.tests.test_convergence_campaign_job import _raw_campaign


class _Model:
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _Client:
    port = None

    def __init__(self, *, attempt_mutation=False):
        self.loaded = []
        self.clear_count = 0
        self.attempt_mutation = attempt_mutation
        self.mutation_blocked = 0

    def load(self, path):
        if self.attempt_mutation:
            try:
                Path(path).write_bytes(b"replacement")
            except PermissionError:
                self.mutation_blocked += 1
        self.loaded.append(path)
        return _Model(f"model-{len(self.loaded)}")

    def clear(self):
        self.clear_count += 1


class _Ownership:
    def __init__(self, *, release_success=True):
        self.acquired = False
        self.released = False
        self.release_success = release_success

    def preflight(self, **kwargs):
        return {"ready": True, "blockers": []}

    def acquire(self, **kwargs):
        self.acquired = True
        return {"success": True}

    def heartbeat(self, **kwargs):
        return {"success": True}

    def release(self):
        self.released = True
        return {"success": self.release_success}


def _telemetry(stage, point_id, model, directory, elapsed):
    return {
        "stage": stage,
        "observed_at_epoch": time.time(),
        "mesh_elements": 12,
        "elapsed_wall_seconds": elapsed,
    }


def _created_job(tmp_path, ascii_tmp_path):
    raw = _raw_campaign(tmp_path / "sources")
    raw["convergence_policy"]["minimum_level_count"] = 2
    raw["convergence_policy"]["declared_cap_reached"] = False
    raw["stop_policy"]["minimum_completed_levels"] = 2
    spec = normalize_convergence_campaign_spec(raw)
    store = JobStore(ascii_tmp_path / "runtime" / "jobs")
    now = time.time()
    identity = process_identity(os.getpid())
    state = {
        "schema_version": "2",
        "status": "submitted",
        "attempt": 1,
        "created_at_epoch": now,
        "updated_at_epoch": now,
        "worker_pid": identity["pid"],
        "worker_process_create_time": identity["process_create_time"],
        "worker_command_signature": identity["command_signature"],
        "progress": {"completed": 0, "total": spec["maximum_total_points"]},
        "last_error": None,
    }
    job_id = store.create(spec, state)
    return store, spec, job_id


def _collector_for(spec, *, fail_configuration=None):
    by_configuration = {
        level["spectral_job"]["configuration_sha256"]: level for level in spec["levels"]
    }

    def collect(point, _collector, artifact_dir):
        configuration = point["configuration_sha256"]
        if configuration == fail_configuration:
            raise RuntimeError("injected level solve failure")
        level = by_configuration[configuration]
        ordinal = level["ordinal"]
        wavelength = point["wavelength"]["value"]
        center = 5e-6 + ordinal * 1e-9
        absorption = 0.1 + 0.8 / (1.0 + ((wavelength - center) / 0.4e-6) ** 2)
        return write_fake_point_audit(
            artifact_dir, level["spectral_job"], point, absorption=absorption
        )

    return collect


def test_worker_uses_one_owner_and_client_for_all_exact_levels(tmp_path, ascii_tmp_path):
    store, spec, job_id = _created_job(tmp_path, ascii_tmp_path)
    ownership = _Ownership()
    client = _Client(attempt_mutation=True)
    factory_calls = {"ownership": 0, "client": 0}

    def ownership_factory(*_args):
        factory_calls["ownership"] += 1
        return ownership

    def client_factory(_spec):
        factory_calls["client"] += 1
        return client

    code = _run(
        str(store.root),
        job_id,
        ownership_factory=ownership_factory,
        client_factory=client_factory,
        collector_executor=_collector_for(spec),
        telemetry_provider=_telemetry,
        native_cancel_enabled=False,
    )

    state = store.read_state(job_id)
    assert code == 0
    assert state["status"] == "completed"
    assert state["completed_levels"] == 3
    assert state["convergence_summary"]["scientific_disposition"] == "accepted"
    assert ownership.acquired is True and ownership.released is True
    assert factory_calls == {"ownership": 1, "client": 1}
    assert len(client.loaded) == 3
    assert client.mutation_blocked == 3
    assert client.clear_count == 4
    assert all(
        Path(level["spectral_job"]["source_model_path"]).read_bytes().startswith(b"model-level-")
        for level in spec["levels"]
    )


def test_failed_later_level_resumes_without_rerunning_completed_level(tmp_path, ascii_tmp_path):
    store, spec, job_id = _created_job(tmp_path, ascii_tmp_path)
    second_configuration = spec["levels"][1]["spectral_job"]["configuration_sha256"]
    first_client = _Client()
    first = _run(
        str(store.root),
        job_id,
        ownership_factory=lambda *_args: _Ownership(),
        client_factory=lambda _spec: first_client,
        collector_executor=_collector_for(spec, fail_configuration=second_configuration),
        telemetry_provider=_telemetry,
        native_cancel_enabled=False,
    )
    assert first == 1
    assert store.read_state(job_id)["status"] == "failed"
    rows = read_convergence_campaign_levels(
        store.job_dir(job_id) / "convergence_levels.jsonl",
        spec,
        artifact_root=store.job_dir(job_id),
    )
    assert [row["level_id"] for row in rows] == ["mesh-0"]

    identity = process_identity(os.getpid())
    store.update_state(
        job_id,
        "starting",
        patch={
            "attempt": 2,
            "worker_pid": identity["pid"],
            "worker_process_create_time": identity["process_create_time"],
            "worker_command_signature": identity["command_signature"],
            "last_error": None,
            "progress": {"completed": 0, "total": spec["maximum_total_points"]},
        },
        event="test_resume",
    )
    second_client = _Client()
    second = _run(
        str(store.root),
        job_id,
        ownership_factory=lambda *_args: _Ownership(),
        client_factory=lambda _spec: second_client,
        collector_executor=_collector_for(spec),
        telemetry_provider=_telemetry,
        native_cancel_enabled=False,
    )
    assert second == 0
    assert store.read_state(job_id)["status"] == "completed"
    assert second_client.loaded == [
        spec["levels"][1]["spectral_job"]["source_model_path"],
        spec["levels"][2]["spectral_job"]["source_model_path"],
    ]
    rows = read_convergence_campaign_levels(
        store.job_dir(job_id) / "convergence_levels.jsonl",
        spec,
        artifact_root=store.job_dir(job_id),
    )
    assert [row["level_id"] for row in rows] == ["mesh-0", "mesh-1", "mesh-2"]
    expected_points = sum(
        len(
            read_spectral_rows(
                convergence_level_directory(store.job_dir(job_id), level["ordinal"])
                / "spectral_rows.jsonl",
                level["spectral_job"],
                artifact_root=convergence_level_directory(store.job_dir(job_id), level["ordinal"]),
            )
        )
        for level in spec["levels"]
    )
    assert store.read_state(job_id)["progress"] == {
        "completed": expected_points,
        "total": expected_points,
    }


def test_cleanup_failure_prevents_false_completed_state(tmp_path, ascii_tmp_path):
    store, spec, job_id = _created_job(tmp_path, ascii_tmp_path)
    code = _run(
        str(store.root),
        job_id,
        ownership_factory=lambda *_args: _Ownership(release_success=False),
        client_factory=lambda _spec: _Client(),
        collector_executor=_collector_for(spec),
        telemetry_provider=_telemetry,
        native_cancel_enabled=False,
    )
    state = store.read_state(job_id)
    assert code == 1
    assert state["status"] == "failed"
    assert "lease_release" in state["last_error"]["message"]


def test_manager_exact_resubmission_observes_existing_campaign(
    tmp_path, ascii_tmp_path, monkeypatch
):
    raw = _raw_campaign(tmp_path / "sources")
    raw["convergence_policy"]["declared_cap_reached"] = False
    preflight_calls = []

    def preflight(**_kwargs):
        preflight_calls.append(True)
        return {"ready": True}

    manager = JobManager(
        ascii_tmp_path / "manager" / "jobs",
        preflight=preflight,
        reconcile_on_start=False,
    )
    launches = []

    def launch_once(*_args):
        launches.append(True)
        return process_identity(os.getpid())

    monkeypatch.setattr(manager, "_launch_worker", launch_once)

    first = manager.submit(raw)
    second = manager.submit(raw)
    status = manager.status(first["job_id"])

    assert second == {
        "success": True,
        "job_id": first["job_id"],
        "status": "submitted",
        "duplicate": True,
        "action": "observe_existing",
    }
    assert launches == [True]
    assert preflight_calls == [True]
    assert status["convergence_progress"] == {
        "declared_levels": 3,
        "completed_levels": 0,
        "pending_levels": 3,
        "completed_level_ids": [],
        "last_level_row_sha256": None,
        "maximum_total_points": 30,
    }


def test_early_cancellation_reconciles_cleanup_failure(tmp_path, ascii_tmp_path):
    store, spec, job_id = _created_job(tmp_path, ascii_tmp_path)
    ownership = _Ownership(release_success=False)
    client = _Client()

    def cancelling_client(_spec):
        store.request_cancel(job_id, requester_identity=process_identity(os.getpid()))
        return client

    code = _run(
        str(store.root),
        job_id,
        ownership_factory=lambda *_args: ownership,
        client_factory=cancelling_client,
        collector_executor=_collector_for(spec),
        telemetry_provider=_telemetry,
        native_cancel_enabled=False,
    )

    state = store.read_state(job_id)
    assert code == 1
    assert state["status"] == "cancel_requested"
    assert state["cancel"]["cooperative_observation"]["request_id"] == state["cancel"]["request_id"]
    assert "lease_release" in state["cancel"]["worker_error"]["message"]


def test_final_source_hash_error_becomes_durable_failure(tmp_path, ascii_tmp_path, monkeypatch):
    store, spec, job_id = _created_job(tmp_path, ascii_tmp_path)
    final_verification = False
    real_hash = worker_module._sha256_file

    def controlled_hash(path):
        if final_verification:
            raise OSError("injected final convergence hash failure")
        return real_hash(path)

    def fault_hook(phase, _context):
        nonlocal final_verification
        if phase == "during_cleanup":
            final_verification = True

    monkeypatch.setattr(worker_module, "_sha256_file", controlled_hash)
    code = _run(
        str(store.root),
        job_id,
        ownership_factory=lambda *_args: _Ownership(),
        client_factory=lambda _spec: _Client(),
        collector_executor=_collector_for(spec),
        telemetry_provider=_telemetry,
        native_cancel_enabled=False,
        fault_hook=fault_hook,
    )

    state = store.read_state(job_id)
    assert code == 1
    assert state["status"] == "failed"
    assert state["last_error"]["type"] == "OSError"
    assert "final convergence hash failure" in state["last_error"]["message"]


def test_native_cancel_timeout_blocks_client_and_lease_cleanup(
    tmp_path, ascii_tmp_path, monkeypatch
):
    from src.jobs import native_cancel_probe
    from src.jobs import worker as production_worker

    store, spec, job_id = _created_job(tmp_path, ascii_tmp_path)
    ownership = _Ownership()
    client = _Client()
    native_started = threading.Event()
    native_release = threading.Event()
    native_finished = threading.Event()
    base_collector = _collector_for(spec)
    cancellation_requested = False

    def blocked_native_cancel():
        native_started.set()
        native_release.wait(timeout=5)
        return {"attempted": True, "supported": False}

    real_record = production_worker._record_native_cancel

    def tracked_record(*args, **kwargs):
        try:
            return real_record(*args, **kwargs)
        finally:
            native_finished.set()

    def cancelling_collector(point, collector, artifact_dir):
        nonlocal cancellation_requested
        result = base_collector(point, collector, artifact_dir)
        if not cancellation_requested:
            cancellation_requested = True
            store.request_cancel(job_id, requester_identity=process_identity(os.getpid()))
            assert native_started.wait(timeout=2)
        return result

    monkeypatch.setattr(native_cancel_probe, "request_native_cancel_once", blocked_native_cancel)
    monkeypatch.setattr(production_worker, "_record_native_cancel", tracked_record)
    try:
        code = _run(
            str(store.root),
            job_id,
            ownership_factory=lambda *_args: ownership,
            client_factory=lambda _spec: client,
            collector_executor=cancelling_collector,
            telemetry_provider=_telemetry,
            native_cancel_enabled=True,
        )
    finally:
        native_release.set()
        assert native_finished.wait(timeout=2)

    state = store.read_state(job_id)
    assert code == 1
    assert ownership.released is False
    assert client.clear_count == 1
    assert "native_cancel_thread" in state["cancel"]["worker_error"]["message"]


def test_invalid_driver_identity_becomes_durable_startup_failure(tmp_path, ascii_tmp_path):
    store, spec, job_id = _created_job(tmp_path, ascii_tmp_path)
    spec["driver_identity"]["package_content_sha256"] = "0" * 64
    atomic_write_json(store.job_dir(job_id) / "spec.json", spec)
    calls = []

    code = _run(
        str(store.root),
        job_id,
        ownership_factory=lambda *_args: calls.append("ownership"),
        client_factory=lambda _spec: calls.append("client"),
        collector_executor=_collector_for(spec),
        telemetry_provider=_telemetry,
        native_cancel_enabled=False,
    )

    state = store.read_state(job_id)
    assert code == 1
    assert state["status"] == "failed"
    assert state["last_error"]["type"] == "ValueError"
    assert calls == []


def test_cancel_during_cleanup_is_durably_observed(tmp_path, ascii_tmp_path):
    store, spec, job_id = _created_job(tmp_path, ascii_tmp_path)
    requested = False

    def cancel_during_cleanup(phase, _context):
        nonlocal requested
        if phase == "during_cleanup" and not requested:
            requested = True
            store.request_cancel(job_id, requester_identity=process_identity(os.getpid()))

    code = _run(
        str(store.root),
        job_id,
        ownership_factory=lambda *_args: _Ownership(),
        client_factory=lambda _spec: _Client(),
        collector_executor=_collector_for(spec),
        telemetry_provider=_telemetry,
        native_cancel_enabled=False,
        fault_hook=cancel_during_cleanup,
    )

    state = store.read_state(job_id)
    assert code == 0
    assert state["status"] == "cancel_requested"
    assert state["cancel"]["cooperative_observation"]["message"] == (
        "Stopped before terminal state publication"
    )
