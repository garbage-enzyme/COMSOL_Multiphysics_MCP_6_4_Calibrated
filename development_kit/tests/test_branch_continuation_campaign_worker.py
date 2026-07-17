"""Injected continuation worker ownership, recovery, and cleanup tests."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import time
import uuid

import pytest

from development_kit.tests.spectral_job_fixtures import write_fake_point_audit
from development_kit.tests.test_branch_continuation_campaign_job import _raw_campaign
from src.jobs.branch_continuation_campaign import normalize_branch_continuation_campaign_spec
from src.jobs.branch_continuation_campaign_rows import (
    read_branch_continuation_campaign_states,
)
from src.jobs.branch_continuation_campaign_worker import _run
from src.jobs.store import JobStore, process_identity


class _Model:
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _Client:
    port = None

    def __init__(self):
        self.loaded = []
        self.clear_count = 0

    def load(self, path):
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


@pytest.fixture
def ascii_root():
    root = Path("D:/comsol_runtime_test") / f"continuation-worker-{uuid.uuid4().hex}"
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _created_job(tmp_path, ascii_root):
    raw = _raw_campaign(tmp_path / "sources")
    for state in raw["states"]:
        state["spectral_job"]["measurement_configuration"]["peak_method"] = (
            "quadratic_interpolation"
        )
        state["spectral_job"]["refinement_policy"]["peak_shift_abs_tolerance_m"] = 1e-6
    spec = normalize_branch_continuation_campaign_spec(raw)
    store = JobStore(ascii_root / "runtime" / "jobs")
    now = time.time()
    identity = process_identity(os.getpid())
    state = {
        "schema_version": "2", "status": "submitted", "attempt": 1,
        "created_at_epoch": now, "updated_at_epoch": now,
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
        item["spectral_job"]["configuration_sha256"]: item for item in spec["states"]
    }

    def collect(point, _collector, artifact_dir):
        configuration = point["configuration_sha256"]
        if configuration == fail_configuration:
            raise RuntimeError("injected state solve failure")
        state = by_configuration[configuration]
        ordinal = state["ordinal"]
        wavelength = point["wavelength"]["value"]
        center = 5e-6 + ordinal * 20e-9
        absorption = 0.1 + 0.8 / (1.0 + ((wavelength - center) / 0.4e-6) ** 2)
        return write_fake_point_audit(
            artifact_dir, state["spectral_job"], point, absorption=absorption
        )

    return collect


def test_worker_uses_one_owner_and_client_for_all_exact_states(tmp_path, ascii_root):
    store, spec, job_id = _created_job(tmp_path, ascii_root)
    ownership = _Ownership()
    client = _Client()
    code = _run(
        str(store.root),
        job_id,
        ownership_factory=lambda *_args: ownership,
        client_factory=lambda _spec: client,
        collector_executor=_collector_for(spec),
        telemetry_provider=_telemetry,
        native_cancel_enabled=False,
    )

    state = store.read_state(job_id)
    assert code == 0
    assert state["status"] == "completed"
    assert state["completed_states"] == 3
    assert state["branch_continuation_summary"]["scientific_disposition"] == "accepted"
    assert ownership.acquired is True and ownership.released is True
    assert len(client.loaded) == 3
    assert client.clear_count == 4


def test_failed_later_state_resumes_without_rerunning_completed_state(tmp_path, ascii_root):
    store, spec, job_id = _created_job(tmp_path, ascii_root)
    second_configuration = spec["states"][1]["spectral_job"]["configuration_sha256"]
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
    rows = read_branch_continuation_campaign_states(
        store.job_dir(job_id) / "continuation_states.jsonl",
        spec,
        artifact_root=store.job_dir(job_id),
    )
    assert [row["state_id"] for row in rows] == ["angle-0"]

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
    assert len(second_client.loaded) == 2


def test_cleanup_failure_prevents_false_completed_state(tmp_path, ascii_root):
    store, spec, job_id = _created_job(tmp_path, ascii_root)
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
