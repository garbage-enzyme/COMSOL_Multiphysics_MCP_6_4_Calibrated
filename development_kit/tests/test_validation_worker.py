from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
from src.jobs.manager import JobManager, validate_staged_sweep_spec
from src.jobs.store import JobStore, process_identity
from src.jobs.validation_matrix import normalize_validation_matrix_spec
from src.jobs.validation_rows import read_validation_rows
from src.jobs.validation_worker import _run


@pytest.fixture
def ascii_root(ascii_tmp_path):
    yield ascii_tmp_path


def _raw_spec(source: Path, *, points=2):
    matrix = []
    for index in range(points):
        point_id = f"point-{index}"
        matrix.append(
            {
                "point_id": point_id,
                "configuration_sha256": f"{index + 1:x}" * 64,
                "wavelength": {"value": 5.1 + 0.1 * index, "unit": "um", "parameter": "wl"},
                "collectors": [
                    {"name": "wave_optics_point_audit", "inputs": {"component_tag": "comp1"}}
                ],
                "expected_artifact_ids": [f"audit-{index}"],
            }
        )
    return {
        "job_type": "validation_matrix",
        "source_model_path": str(source),
        "points": matrix,
        "point_limit": points,
        "cores": 1,
        "resource_policy": {
            "wall_time_budget_seconds": 120,
            "minimum_next_point_seconds": 1,
            "max_mesh_elements": 100,
        },
    }


def _create_job(root: Path, spec):
    store = JobStore(root)
    identity = process_identity(os.getpid())
    job_id = store.create(
        spec,
        {
            "schema_version": "2",
            "status": "submitted",
            "attempt": 1,
            "worker_pid": identity["pid"],
            "worker_process_create_time": identity["process_create_time"],
            "worker_command_signature": identity["command_signature"],
            "progress": {"completed": 0, "total": len(spec["points"])},
            "last_error": None,
        },
    )
    return store, job_id


class FakeModel:
    def name(self):
        return "fixture"


class FakeClient:
    port = None

    def __init__(self):
        self.cleared = False

    def load(self, _path):
        return FakeModel()

    def clear(self):
        self.cleared = True


class FakeOwnership:
    def __init__(self):
        self.acquired = False
        self.released = False
        self.heartbeats = 0

    def preflight(self, **_kwargs):
        return {"ready": True, "blockers": []}

    def acquire(self, **_kwargs):
        self.acquired = True
        return {"success": True}

    def heartbeat(self, **_kwargs):
        self.heartbeats += 1
        return {"success": True}

    def release(self):
        self.released = True
        return {"success": True}


def _telemetry(mesh_elements=10):
    def provide(stage, _point_id, _model, _directory, elapsed):
        return {
            "stage": stage,
            "observed_at_epoch": 1.0 + elapsed,
            "mesh_elements": mesh_elements,
            "elapsed_wall_seconds": elapsed,
        }

    return provide


def _collector(point, _collector, artifact_dir):
    manifest = artifact_dir / "manifest.json"
    manifest.write_text(json.dumps({"point": point["point_id"]}), encoding="utf-8")
    return {
        "success": True,
        "audit_status": "measurement_complete",
        "artifacts": {"manifest": str(manifest)},
    }


def test_validation_worker_reuses_one_claim_and_completes_exact_rows(tmp_path, ascii_root):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    spec = normalize_validation_matrix_spec(_raw_spec(source))
    store, job_id = _create_job(ascii_root / "jobs", spec)
    ownership = FakeOwnership()
    client = FakeClient()

    code = _run(
        str(store.root),
        job_id,
        ownership_factory=lambda *_args: ownership,
        client_factory=lambda _spec: client,
        collector_executor=_collector,
        telemetry_provider=_telemetry(),
        native_cancel_enabled=False,
    )

    state = store.read_state(job_id)
    assert code == 0
    assert state["status"] == "completed"
    assert state["progress"] == {"completed": 2, "total": 2}
    assert state["source_unchanged"] is True
    assert ownership.acquired and ownership.released
    assert ownership.heartbeats >= 3
    assert client.cleared
    assert [
        row["point_id"]
        for row in read_validation_rows(store.job_dir(job_id) / "matrix_rows.jsonl", spec)
    ] == [
        "point-0",
        "point-1",
    ]


def test_validation_worker_resource_refusal_is_resumable_without_false_row(tmp_path, ascii_root):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    spec = normalize_validation_matrix_spec(_raw_spec(source, points=1))
    store, job_id = _create_job(ascii_root / "jobs", spec)
    ownership = FakeOwnership()
    client = FakeClient()

    code = _run(
        str(store.root),
        job_id,
        ownership_factory=lambda *_args: ownership,
        client_factory=lambda _spec: client,
        collector_executor=lambda *_args: (_ for _ in ()).throw(AssertionError("must not solve")),
        telemetry_provider=_telemetry(mesh_elements=101),
        native_cancel_enabled=False,
    )

    state = store.read_state(job_id)
    assert code == 0
    assert state["status"] == "interrupted"
    assert state["last_error"]["type"] == "ResourceAdmissionStop"
    assert not (store.job_dir(job_id) / "matrix_rows.jsonl").exists()
    assert ownership.released
    assert client.cleared


def test_manager_routes_validation_submit_and_resume_to_dedicated_worker(
    tmp_path, ascii_root, monkeypatch
):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    manager = JobManager(ascii_root / "jobs", preflight=lambda **_kwargs: {"ready": True})
    identity = process_identity(os.getpid())
    modules = []

    def launch(_job_id, module):
        modules.append(module)
        return identity

    monkeypatch.setattr(manager, "_launch_worker", launch)
    submitted = manager.submit(_raw_spec(source))
    assert manager.store.read_state(submitted["job_id"])["progress"] == {
        "completed": 0,
        "total": 2,
    }
    assert modules == ["comsol_mcp.jobs.validation_worker"]

    manager.store.update_state(submitted["job_id"], "interrupted", event="test_interrupt")
    resumed = manager.resume(submitted["job_id"])
    assert resumed["attempt"] == 2
    assert modules == [
        "comsol_mcp.jobs.validation_worker",
        "comsol_mcp.jobs.validation_worker",
    ]


def test_manager_rejects_matrix_bounds_before_preflight_or_launch(
    tmp_path, ascii_root, monkeypatch
):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    calls = []
    manager = JobManager(ascii_root / "jobs", preflight=lambda **_kwargs: calls.append("preflight"))
    monkeypatch.setattr(manager, "_launch_worker", lambda *_args: calls.append("launch"))
    raw = _raw_spec(source, points=1)
    raw["point_limit"] = 0

    try:
        manager.submit(raw)
    except ValueError as exc:
        assert "point_limit" in str(exc)
    else:
        raise AssertionError("invalid matrix must fail")
    assert calls == []


def test_exact_duplicate_submit_returns_existing_job_without_second_launch(
    tmp_path, ascii_root, monkeypatch
):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    manager = JobManager(ascii_root / "jobs", preflight=lambda **_kwargs: {"ready": True})
    identity = process_identity(os.getpid())
    launches = []
    monkeypatch.setattr(
        manager,
        "_launch_worker",
        lambda job_id, module: launches.append((job_id, module)) or identity,
    )

    first = manager.submit(_raw_spec(source))
    second = manager.submit(_raw_spec(source))

    assert second["duplicate"] is True
    assert second["job_id"] == first["job_id"]
    assert second["action"] == "observe_existing"
    assert len(launches) == 1


def test_malformed_prior_row_fails_before_ownership_or_client(tmp_path, ascii_root):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    spec = normalize_validation_matrix_spec(_raw_spec(source, points=1))
    store, job_id = _create_job(ascii_root / "jobs", spec)
    (store.job_dir(job_id) / "matrix_rows.jsonl").write_text("{bad json}\n", encoding="utf-8")
    calls = []

    code = _run(
        str(store.root),
        job_id,
        ownership_factory=lambda *_args: calls.append("ownership"),
        client_factory=lambda _spec: calls.append("client"),
        collector_executor=_collector,
        telemetry_provider=_telemetry(),
        native_cancel_enabled=False,
    )

    assert code == 1
    assert calls == []
    assert store.read_state(job_id)["status"] == "failed"


def test_interrupted_matrix_resumes_only_pending_exact_point_and_status_is_bounded(
    tmp_path, ascii_root, monkeypatch
):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    spec = normalize_validation_matrix_spec(_raw_spec(source))
    store, job_id = _create_job(ascii_root / "jobs", spec)
    first_ownership = FakeOwnership()

    def stop_after_first(stage, point_id, _model, _directory, elapsed):
        second_id = spec["points"][1]["point_fingerprint"]
        return {
            "stage": stage,
            "observed_at_epoch": 1.0 + elapsed,
            "mesh_elements": 101 if stage == "pre_solve" and point_id == second_id else 10,
            "elapsed_wall_seconds": elapsed,
        }

    first_code = _run(
        str(store.root),
        job_id,
        ownership_factory=lambda *_args: first_ownership,
        client_factory=lambda _spec: FakeClient(),
        collector_executor=_collector,
        telemetry_provider=stop_after_first,
        native_cancel_enabled=False,
    )
    assert first_code == 0
    assert store.read_state(job_id)["status"] == "interrupted"
    assert len(read_validation_rows(store.job_dir(job_id) / "matrix_rows.jsonl", spec)) == 1

    manager = JobManager(store.root, preflight=lambda **_kwargs: {"ready": True})
    identity = process_identity(os.getpid())
    monkeypatch.setattr(manager, "_launch_worker", lambda *_args: identity)
    assert manager.resume(job_id)["attempt"] == 2
    second_ownership = FakeOwnership()
    second_code = _run(
        str(store.root),
        job_id,
        ownership_factory=lambda *_args: second_ownership,
        client_factory=lambda _spec: FakeClient(),
        collector_executor=_collector,
        telemetry_provider=_telemetry(),
        native_cancel_enabled=False,
    )

    rows = read_validation_rows(store.job_dir(job_id) / "matrix_rows.jsonl", spec)
    status = manager.status(job_id)
    assert second_code == 0
    assert [row["point_id"] for row in rows] == ["point-0", "point-1"]
    assert [row["attempt"] for row in rows] == [1, 2]
    assert status["status"] == "completed"
    assert status["matrix_summary"] == {
        "total_declared": 2,
        "rows": 2,
        "complete": 2,
        "errors": 0,
        "pending": 0,
        "last_row_sha256": rows[-1]["row_sha256"],
        "last_error_type": None,
    }


def test_validation_worker_observes_attempt_bound_cancel_before_client_start(tmp_path, ascii_root):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    spec = normalize_validation_matrix_spec(_raw_spec(source, points=1))
    store, job_id = _create_job(ascii_root / "jobs", spec)
    request = store.request_cancel(job_id, requester_identity=process_identity(os.getpid()))
    calls = []

    code = _run(
        str(store.root),
        job_id,
        ownership_factory=lambda *_args: calls.append("ownership"),
        client_factory=lambda _spec: calls.append("client"),
        collector_executor=_collector,
        telemetry_provider=_telemetry(),
        native_cancel_enabled=False,
    )

    state = store.read_state(job_id)
    assert request["accepted"] is True
    assert code == 0
    assert calls == []
    assert state["status"] == "cancel_requested"
    assert state["cancel"]["cooperative_observation"]["target_attempt"] == 1


def test_validation_transition_error_remains_bound_to_concurrent_cancel(
    tmp_path, ascii_root, monkeypatch
):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    spec = normalize_validation_matrix_spec(_raw_spec(source, points=1))
    store, job_id = _create_job(ascii_root / "jobs", spec)
    identity = process_identity(os.getpid())
    real_update = JobStore.update_state

    def cancel_before_validation_transition(self, target_job_id, *args, **kwargs):
        if target_job_id == job_id and kwargs.get("event") == "first_point_validated":
            self.request_cancel(job_id, requester_identity=identity)
        return real_update(self, target_job_id, *args, **kwargs)

    monkeypatch.setattr(JobStore, "update_state", cancel_before_validation_transition)
    code = _run(
        str(store.root),
        job_id,
        ownership_factory=lambda *_args: FakeOwnership(),
        client_factory=lambda _spec: FakeClient(),
        collector_executor=_collector,
        telemetry_provider=_telemetry(),
        native_cancel_enabled=False,
    )

    state = store.read_state(job_id)
    assert code == 1
    assert state["status"] == "cancel_requested"
    assert state["cancel"]["worker_error"]["type"] == "ValueError"
    assert "Invalid job state transition" in state["cancel"]["worker_error"]["message"]


def test_blocked_native_cancel_preserves_validation_client_and_lease(
    tmp_path, ascii_root, monkeypatch
):
    from src.jobs import native_cancel_probe

    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    spec = normalize_validation_matrix_spec(_raw_spec(source, points=1))
    store, job_id = _create_job(ascii_root / "jobs", spec)
    entered = threading.Event()
    release = threading.Event()
    ownership = FakeOwnership()
    client = FakeClient()

    def blocked_probe():
        entered.set()
        assert release.wait(timeout=10)
        return {"requested": False}

    def cancel_collector(point, collector, artifact_dir):
        store.request_cancel(job_id, requester_identity=process_identity(os.getpid()))
        assert entered.wait(timeout=2)
        return _collector(point, collector, artifact_dir)

    monkeypatch.setattr(native_cancel_probe, "request_native_cancel_once", blocked_probe)
    try:
        code = _run(
            str(store.root),
            job_id,
            ownership_factory=lambda *_args: ownership,
            client_factory=lambda _spec: client,
            collector_executor=cancel_collector,
            telemetry_provider=_telemetry(),
            native_cancel_enabled=True,
        )
        assert code == 0
        assert store.read_state(job_id)["status"] in {"cancel_requested", "cancelling"}
        assert ownership.acquired and not ownership.released
        assert not client.cleared
    finally:
        monitors = [
            thread for thread in threading.enumerate() if thread.name == "comsol-native-cancel"
        ]
        release.set()
        for monitor in monitors:
            monitor.join(timeout=2)
            assert not monitor.is_alive()


def test_native_cancel_monitor_failure_is_recorded_durably(tmp_path, ascii_root, monkeypatch):
    from src.jobs import native_cancel_probe

    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    spec = normalize_validation_matrix_spec(_raw_spec(source, points=1))
    store, job_id = _create_job(ascii_root / "jobs", spec)
    monitor_failed = threading.Event()

    def fail_native_cancel():
        monitor_failed.set()
        raise RuntimeError("injected native monitor failure")

    def request_cancel_then_wait(point, collector, artifact_dir):
        store.request_cancel(job_id, requester_identity=process_identity(os.getpid()))
        assert monitor_failed.wait(timeout=1.0)
        return _collector(point, collector, artifact_dir)

    monkeypatch.setattr(native_cancel_probe, "request_native_cancel_once", fail_native_cancel)
    code = _run(
        str(store.root),
        job_id,
        ownership_factory=lambda *_args: FakeOwnership(),
        client_factory=lambda _spec: FakeClient(),
        collector_executor=request_cancel_then_wait,
        telemetry_provider=_telemetry(),
        native_cancel_enabled=True,
    )

    state = store.read_state(job_id)
    assert code == 0
    assert state["status"] == "cancel_requested"
    assert state["cancel"]["worker_error"] == {
        "type": "RuntimeError",
        "message": "injected native monitor failure",
        "cleanup_errors": [],
    }


def test_duplicate_admission_works_beyond_the_old_scan_bound(tmp_path, ascii_root, monkeypatch):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    manager = JobManager(ascii_root / "jobs", preflight=lambda **_kwargs: {"ready": True})
    identity = process_identity(os.getpid())
    launches = []
    monkeypatch.setattr(
        manager,
        "_launch_worker",
        lambda job_id, module: launches.append((job_id, module)) or identity,
    )
    for index in range(1200):
        directory = ascii_root / "jobs" / f"job-hist{index:05d}"
        directory.mkdir()
        (directory / "spec.json").write_text(
            json.dumps(
                {
                    "job_type": "validation_matrix",
                    "spec_fingerprint": f"{index:064x}",
                }
            ),
            encoding="ascii",
        )

    first = manager.submit(_raw_spec(source))

    other = tmp_path / "other.mph"
    other.write_bytes(b"different-model")
    second = manager.submit(_raw_spec(other))
    assert second["job_id"] != first["job_id"]
    assert len(launches) == 2

    historical = manager._find_exact_duplicate("validation_matrix", f"{7:064x}")
    assert historical == "job-hist00007"

    duplicate = manager.submit(_raw_spec(source))
    assert duplicate["duplicate"] is True
    assert duplicate["job_id"] == first["job_id"]
    assert len(launches) == 2


def test_duplicate_index_recovers_from_corruption_deletion_and_stale_entries(
    tmp_path, ascii_root, monkeypatch
):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    jobs_root = ascii_root / "jobs"
    manager = JobManager(jobs_root, preflight=lambda **_kwargs: {"ready": True})
    identity = process_identity(os.getpid())
    monkeypatch.setattr(manager, "_launch_worker", lambda job_id, module: identity)
    first = manager.submit(_raw_spec(source))
    index_path = jobs_root / "duplicate-index.json"

    index_path.write_text("{corrupt", encoding="ascii")
    rebuilt = manager.submit(_raw_spec(source))
    assert rebuilt["duplicate"] is True
    assert rebuilt["job_id"] == first["job_id"]

    index_path.unlink()
    after_restart = JobManager(jobs_root, preflight=lambda **_kwargs: {"ready": True})
    monkeypatch.setattr(after_restart, "_launch_worker", lambda job_id, module: identity)
    restarted = after_restart.submit(_raw_spec(source))
    assert restarted["duplicate"] is True
    assert restarted["job_id"] == first["job_id"]

    # A stale entry names a job whose directory was fully removed by the
    # normal retention lifecycle: rebuild never sees the directory, and the
    # entry must not block other admissions.
    stale_name = "job-removed0000000000000000000000000001"
    index_path.write_text(
        json.dumps({"entries": {("a" * 64): stale_name}}),
        encoding="ascii",
    )
    assert manager._find_exact_duplicate("validation_matrix", "a" * 64) is None
    other = tmp_path / "other.mph"
    other.write_bytes(b"different-model")
    admitted = manager.submit(_raw_spec(other))
    assert admitted.get("duplicate") is not True


def test_concurrent_identical_submissions_admit_exactly_one_job(tmp_path, ascii_root, monkeypatch):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    manager = JobManager(ascii_root / "jobs", preflight=lambda **_kwargs: {"ready": True})
    identity = process_identity(os.getpid())
    launches = []
    monkeypatch.setattr(
        manager,
        "_launch_worker",
        lambda job_id, module: launches.append(job_id) or identity,
    )
    results = []

    def submit_once():
        results.append(manager.submit(_raw_spec(source)))

    threads = [threading.Thread(target=submit_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    job_ids = {result["job_id"] for result in results}
    assert len(job_ids) == 1
    assert sum(1 for result in results if result.get("duplicate")) == 1
    assert len(launches) == 1


def test_resource_journal_capacity_gates_admission_before_launch(tmp_path, ascii_root, monkeypatch):
    from src.jobs.manager import JobManager

    import comsol_mcp.jobs.resource_admission as admission_module

    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    manager = JobManager(ascii_root / "jobs", preflight=lambda **_kwargs: {"ready": True})
    identity = process_identity(os.getpid())
    launches = []
    monkeypatch.setattr(
        manager,
        "_launch_worker",
        lambda job_id, module: launches.append(job_id) or identity,
    )

    oversized = _raw_spec(source, points=2045)
    oversized["parameter_name"] = "wl"
    oversized["parameter_values"] = [1.0 + 0.001 * index for index in range(2045)]
    with pytest.raises(ValueError, match="resource journal cannot hold"):
        manager.submit(
            {
                "job_type": "staged_sweep",
                "source_model_path": str(source),
                "parameter_name": "wl",
                "parameter_values": [1.0 + 0.001 * index for index in range(2045)],
                "expressions": ["ewfd.Rtotal"],
            }
        )
    assert launches == []
    assert not any(path.name.startswith("job-") for path in (ascii_root / "jobs").iterdir())

    admitted = manager.submit(
        {
            "job_type": "staged_sweep",
            "source_model_path": str(source),
            "parameter_name": "wl",
            "parameter_values": [1.0],
            "expressions": ["ewfd.Rtotal"],
        }
    )
    assert admitted["success"] is True
    assert len(launches) == 1

    # Four journal entries per point (pre-solve and post-solve telemetry and
    # decision pairs) plus the startup reserve; the earlier two-per-point
    # bound admitted sweeps that could only die at the 4096-entry limit.
    verdict = admission_module.resource_journal_capacity_verdict(1023)
    assert verdict == {
        "total_points": 1023,
        "required_entries_lower_bound": 4100,
        "max_entries": 4096,
        "fits": False,
    }
    assert admission_module.resource_journal_capacity_verdict(1022)["fits"] is True


def test_request_id_binds_response_loss_retries_to_one_attempt(tmp_path, ascii_root, monkeypatch):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    manager = JobManager(ascii_root / "jobs", preflight=lambda **_kwargs: {"ready": True})
    identity = process_identity(os.getpid())
    launches = []
    monkeypatch.setattr(
        manager, "_launch_worker", lambda job_id, module: launches.append(job_id) or identity
    )
    spec = {
        "job_type": "staged_sweep",
        "source_model_path": str(source),
        "parameter_name": "wl",
        "parameter_values": [1.0],
        "expressions": ["ewfd.Rtotal"],
    }

    first = manager.submit(spec, request_id="retry-me-1")
    retry = manager.submit(spec, request_id="retry-me-1")

    assert first["job_id"] == retry["job_id"]
    assert retry["duplicate"] is True
    assert retry["action"] == "request_replay"
    assert launches == [first["job_id"]]

    # The same request id cannot silently rebind to a different submission.
    changed = dict(spec)
    changed["parameter_values"] = [2.0]
    with pytest.raises(ValueError, match="already durably bound"):
        manager.submit(changed, request_id="retry-me-1")

    # Without a request id, identical submissions stay independent by design.
    independent_a = manager.submit(spec)
    independent_b = manager.submit(spec)
    assert independent_a["job_id"] != independent_b["job_id"]
    assert len(launches) == 3

    with pytest.raises(ValueError, match="non-empty ASCII string"):
        manager.submit(spec, request_id="not-ascii-陆")


def test_request_id_unknown_record_resumes_the_preallocated_attempt(
    tmp_path, ascii_root, monkeypatch
):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    jobs_root = ascii_root / "jobs"
    manager = JobManager(jobs_root, preflight=lambda **_kwargs: {"ready": True})
    identity = process_identity(os.getpid())
    launches = []
    monkeypatch.setattr(
        manager, "_launch_worker", lambda job_id, module: launches.append(job_id) or identity
    )
    spec = {
        "job_type": "staged_sweep",
        "source_model_path": str(source),
        "parameter_name": "wl",
        "parameter_values": [1.0],
        "expressions": ["ewfd.Rtotal"],
    }
    normalized = validate_staged_sweep_spec(spec)
    # Simulate the crash window: the write-ahead record exists as UNKNOWN
    # with a pre-allocated id, but no job was ever published under it.
    preallocated = "job-preallocated000000000000000000000001"
    manager._write_request_record(
        "interrupted-1", normalized["spec_fingerprint"], preallocated, "unknown"
    )

    resumed = manager.submit(spec, request_id="interrupted-1")

    assert resumed["job_id"] == preallocated
    assert launches == [preallocated]

    # A DISPATCHED record whose job evidence vanished is uncertain, and
    # uncertainty must stop the dependent resubmission.
    manager._write_request_record(
        "vanished-1",
        normalized["spec_fingerprint"],
        "job-missing0000000000000000000000001",
        "dispatched",
    )
    with pytest.raises(ValueError, match="whose evidence is missing"):
        manager.submit(spec, request_id="vanished-1")


def test_duplicate_type_request_binds_to_the_existing_duplicate(tmp_path, ascii_root, monkeypatch):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    manager = JobManager(ascii_root / "jobs", preflight=lambda **_kwargs: {"ready": True})
    identity = process_identity(os.getpid())
    monkeypatch.setattr(manager, "_launch_worker", lambda job_id, module: identity)
    first = manager.submit(_raw_spec(source))

    bound = manager.submit(_raw_spec(source), request_id="vm-1")
    assert bound["duplicate"] is True
    assert bound["job_id"] == first["job_id"]

    replay = manager.submit(_raw_spec(source), request_id="vm-1")
    assert replay["action"] == "request_replay"
    assert replay["job_id"] == first["job_id"]


def test_lost_duplicate_index_entries_are_reconciled_from_evidence(
    tmp_path, ascii_root, monkeypatch
):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    jobs_root = ascii_root / "jobs"
    manager = JobManager(jobs_root, preflight=lambda **_kwargs: {"ready": True})
    identity = process_identity(os.getpid())
    monkeypatch.setattr(manager, "_launch_worker", lambda job_id, module: identity)
    first = manager.submit(_raw_spec(source))

    # A readable but emptied index over retained evidence is index loss, not
    # proof of absence; admission reconciles from the retained specs.
    (jobs_root / "duplicate-index.json").write_text('{"entries": {}}', encoding="ascii")
    second = manager.submit(_raw_spec(source))
    assert second["duplicate"] is True
    assert second["job_id"] == first["job_id"]


def test_unreadable_retained_spec_fails_closed_for_new_submissions(
    tmp_path, ascii_root, monkeypatch
):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    jobs_root = ascii_root / "jobs"
    manager = JobManager(jobs_root, preflight=lambda **_kwargs: {"ready": True})
    identity = process_identity(os.getpid())
    monkeypatch.setattr(manager, "_launch_worker", lambda job_id, module: identity)
    first = manager.submit(_raw_spec(source))

    (jobs_root / first["job_id"] / "spec.json").write_text("{torn", encoding="ascii")
    with pytest.raises(ValueError, match="unreadable spec"):
        manager.submit(_raw_spec(source))


def test_corrupt_request_binding_refuses_instead_of_relauRnching(tmp_path, ascii_root, monkeypatch):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    jobs_root = ascii_root / "jobs"
    manager = JobManager(jobs_root, preflight=lambda **_kwargs: {"ready": True})
    identity = process_identity(os.getpid())
    launches = []
    monkeypatch.setattr(
        manager, "_launch_worker", lambda job_id, module: launches.append(job_id) or identity
    )
    spec = {
        "job_type": "staged_sweep",
        "source_model_path": str(source),
        "parameter_name": "wl",
        "parameter_values": [1.0],
        "expressions": ["ewfd.Rtotal"],
    }
    manager.submit(spec, request_id="torn-1")
    binding = jobs_root / "request-bindings"
    record = next(binding.glob("*.json"))
    record.write_text("{torn", encoding="ascii")
    with pytest.raises(ValueError, match="unreadable"):
        manager.submit(spec, request_id="torn-1")
    assert len(launches) == 1


def test_partial_duplicate_index_loss_reconciles_from_evidence(tmp_path, ascii_root, monkeypatch):
    source = tmp_path / "a.mph"
    source.write_bytes(b"a")
    other = tmp_path / "b.mph"
    other.write_bytes(b"b")
    jobs_root = ascii_root / "jobs"
    manager = JobManager(jobs_root, preflight=lambda **_kwargs: {"ready": True})
    identity = process_identity(os.getpid())
    launches = []
    monkeypatch.setattr(
        manager, "_launch_worker", lambda job_id, module: launches.append(job_id) or identity
    )
    first = manager.submit(_raw_spec(source))
    second = manager.submit(_raw_spec(other))
    # Remove only the FIRST fingerprint from a still-valid index: the miss
    # must be verified against retained evidence, not trusted as absence.
    import json as _json

    entries = _json.loads((jobs_root / "duplicate-index.json").read_text(encoding="ascii"))
    fingerprint = next(key for key, value in entries["entries"].items() if value == first["job_id"])
    del entries["entries"][fingerprint]
    (jobs_root / "duplicate-index.json").write_text(_json.dumps(entries), encoding="ascii")
    retry = manager.submit(_raw_spec(source))
    assert retry["duplicate"] is True
    assert retry["job_id"] == first["job_id"]
    assert len(launches) == 2


def test_published_without_launch_orphan_is_not_replayed_as_success(
    tmp_path, ascii_root, monkeypatch
):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    manager = JobManager(ascii_root / "jobs", preflight=lambda **_kwargs: {"ready": True})
    identity = process_identity(os.getpid())
    launches = []
    monkeypatch.setattr(
        manager, "_launch_worker", lambda job_id, module: launches.append(job_id) or identity
    )
    spec = {
        "job_type": "staged_sweep",
        "source_model_path": str(source),
        "parameter_name": "wl",
        "parameter_values": [1.0],
        "expressions": ["ewfd.Rtotal"],
    }
    normalized = validate_staged_sweep_spec(spec)
    # Simulate the crash window: the dispatched binding points at a job that
    # was published but whose worker never launched.
    orphan = "job-orphan00000000000000000000000000001"
    from src.jobs.store import JobStore

    store = JobStore(ascii_root / "jobs")
    store.create(
        dict(normalized),
        {
            "schema_version": "2",
            "status": "submitted",
            "attempt": 1,
            "worker_pid": None,
            "progress": {"completed": 0, "total": 1},
            "last_error": None,
        },
        job_id=orphan,
    )
    manager._write_request_record("orphan-1", normalized["spec_fingerprint"], orphan, "dispatched")
    result = manager.submit(spec, request_id="orphan-1")
    assert result["success"] is False
    assert result["action"] == "reconcile_orphan_then_resume"
    assert store.read_state(orphan)["status"] == "interrupted"
    assert launches == []


def test_concurrent_same_request_conflicting_specs_refuse_one(tmp_path, ascii_root, monkeypatch):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"model")
    manager = JobManager(ascii_root / "jobs", preflight=lambda **_kwargs: {"ready": True})
    identity = process_identity(os.getpid())
    launches = []
    monkeypatch.setattr(
        manager, "_launch_worker", lambda job_id, module: launches.append(job_id) or identity
    )
    base = _raw_spec(source)
    first = manager.submit(base, request_id="race-1")

    outcomes = []
    barrier = threading.Barrier(2)

    def submit_variant(spec):
        def run():
            try:
                barrier.wait(timeout=10)
                outcomes.append(("ok", manager.submit(spec, request_id="race-1")))
            except ValueError as exc:
                outcomes.append(("refused", str(exc)[:40]))

        return run

    threads = [
        threading.Thread(target=submit_variant(base)),
        threading.Thread(target=submit_variant(_raw_spec(source, points=3))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    kinds = [kind for kind, _ in outcomes]
    # The replay wins and the conflicting spec is refused; exactly one job
    # id is ever acknowledged under this request.
    assert kinds.count("refused") == 1
    assert kinds.count("ok") == 1
    ok = [value for kind, value in outcomes if kind == "ok"][0]
    assert ok["job_id"] == first["job_id"]
