"""Solver-free contracts for immutable durable attached execution targets."""

from __future__ import annotations

from copy import deepcopy
import csv
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types

import pytest

from src.jobs.attached_backend import normalize_attached_execution_backend
from src.jobs.attached_runtime import (
    normalize_attached_execution_target,
    verify_attached_model_inventory,
    verify_attached_model_revision,
)
from src.jobs.manager import JobManager, validate_staged_sweep_spec
from src.jobs.store import JobStore, process_identity
import src.jobs.worker as production_worker
from src.shared_session.identity import normalize_attached_server_identity
from src.shared_session.locking import (
    build_shared_model_revision,
    normalize_shared_model_identity,
)


@pytest.fixture
def ascii_job_root():
    base = Path("D:/") if Path("D:/").exists() else Path("C:/Windows/Temp")
    root = Path(tempfile.mkdtemp(prefix="comsol_attached_worker_", dir=base))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _backend() -> dict:
    server = normalize_attached_server_identity(
        {
            "endpoint": {"host": "127.0.0.1", "port": 2036},
            "server_pid": 4200,
            "server_process_create_time": 1234.5,
            "server_command_signature": "a" * 64,
            "listener_bind_scope": "wildcard",
            "listener_observed_at_epoch": 2345.6,
        }
    )
    model = normalize_shared_model_identity(
        {
            "tag": "Model1",
            "label": "working.mph",
            "file_path": "D:/models/working.mph",
            "unsaved": False,
        }
    )
    revision = build_shared_model_revision(
        model,
        sequence=0,
        structural_readback={
            "components": ["comp1"],
            "studies": ["std1"],
            "datasets": [],
        },
        state_readback={"parameters": {"gap": "10[nm]"}},
    )
    return {
        "kind": "attached_shared_server",
        "user_confirmed_automation_exclusive": True,
        "source_model_lock_sha256": "b" * 64,
        "attached_server": server.to_dict(),
        "model": model.to_dict(),
        "expected_revision": revision.to_dict(),
    }


def test_attached_backend_is_deterministic_idempotent_and_non_owned():
    first = normalize_attached_execution_backend(_backend())
    second = normalize_attached_execution_backend(deepcopy(first))

    assert first == second
    assert first["attached_server"]["ownership"] == "external_user_owned"
    assert first["attached_server"]["listener_bind_scope"] == "wildcard"
    assert first["model"]["tag"] == "Model1"
    assert len(first["backend_identity_sha256"]) == 64


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(user_confirmed_automation_exclusive=False),
        lambda value: value.update(kind="standalone_owned"),
        lambda value: value.update(source_model_lock_sha256="short"),
        lambda value: value.update(secret="not-allowed"),
        lambda value: value["attached_server"].update(ownership="owned"),
        lambda value: value["attached_server"].update(identity_sha256="0" * 64),
        lambda value: value["model"].update(identity_sha256="0" * 64),
        lambda value: value["expected_revision"].update(
            model_identity_sha256="0" * 64
        ),
        lambda value: value["expected_revision"].update(revision_sha256="0" * 64),
    ],
)
def test_attached_backend_rejects_ambiguous_or_tampered_identity(mutation):
    raw = _backend()
    mutation(raw)

    with pytest.raises(ValueError):
        normalize_attached_execution_backend(raw)


def test_attached_backend_rejects_tampered_aggregate_identity():
    normalized = normalize_attached_execution_backend(_backend())
    normalized["backend_identity_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="backend identity SHA-256"):
        normalize_attached_execution_backend(normalized)


def test_attached_backend_contract_import_does_not_import_mph():
    code = """
import sys
from development_kit.tests.test_attached_job_backend import _backend
from src.jobs.attached_backend import normalize_attached_execution_backend
assert 'mph' not in sys.modules
assert normalize_attached_execution_backend(_backend())['kind'] == 'attached_shared_server'
assert 'mph' not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_staged_sweep_fingerprint_binds_normalized_attached_backend(tmp_path):
    source = tmp_path / "immutable-source.mph"
    source.write_bytes(b"immutable source")
    spec = validate_staged_sweep_spec(
        {
            "job_type": "staged_sweep",
            "source_model_path": str(source),
            "parameter_name": "gap",
            "parameter_values": [10.0, 11.0],
            "expressions": ["A"],
            "execution_backend": _backend(),
        }
    )

    assert spec["execution_backend"]["kind"] == "attached_shared_server"
    assert len(spec["execution_backend"]["backend_identity_sha256"]) == 64
    assert spec["spec_fingerprint"] == validate_staged_sweep_spec(
        {
            "job_type": "staged_sweep",
            "source_model_path": str(source),
            "parameter_name": "gap",
            "parameter_values": [10.0, 11.0],
            "expressions": ["A"],
            "execution_backend": _backend(),
        }
    )["spec_fingerprint"]


def test_attached_runtime_restores_exact_target_and_accepts_unchanged_readback():
    target = normalize_attached_execution_target(_backend())

    selected = verify_attached_model_inventory(
        target,
        [target.model.to_dict(), {
            "tag": "Other",
            "label": "other.mph",
            "file_path": "D:/models/other.mph",
            "unsaved": False,
        }],
    )
    revision = verify_attached_model_revision(
        target,
        structural_readback={
            "components": ["comp1"],
            "studies": ["std1"],
            "datasets": [],
        },
        state_readback={"parameters": {"gap": "10[nm]"}},
    )

    assert selected == target.model.to_dict()
    assert revision == target.expected_revision
    assert target.server.listener_bind_scope == "wildcard"


@pytest.mark.parametrize(
    "inventory",
    [
        [],
        [{
            "tag": "Model1",
            "label": "different.mph",
            "file_path": "D:/models/different.mph",
            "unsaved": False,
        }],
        [
            {
                "tag": "Model1",
                "label": "working.mph",
                "file_path": "D:/models/working.mph",
                "unsaved": False,
            },
            {
                "tag": "Model1",
                "label": "working-copy.mph",
                "file_path": "D:/models/working-copy.mph",
                "unsaved": False,
            },
        ],
    ],
)
def test_attached_runtime_rejects_missing_changed_or_duplicate_model(inventory):
    target = normalize_attached_execution_target(_backend())

    with pytest.raises(ValueError, match="server model|matching"):
        verify_attached_model_inventory(target, inventory)


def test_attached_runtime_rejects_external_revision_change():
    target = normalize_attached_execution_target(_backend())

    with pytest.raises(ValueError, match="revision changed.*readback_sha256"):
        verify_attached_model_revision(
            target,
            structural_readback={
                "components": ["comp1"],
                "studies": ["std1"],
                "datasets": [],
            },
            state_readback={"parameters": {"gap": "11[nm]"}},
        )


def test_attached_production_worker_uses_existing_model_and_never_clears_server(
    ascii_job_root, monkeypatch
):
    import src.tools.ownership as ownership_module
    import src.tools.workflow as workflow_module

    source = ascii_job_root / "immutable-source.mph"
    source.write_bytes(b"immutable source")
    spec = validate_staged_sweep_spec(
        {
            "job_type": "staged_sweep",
            "source_model_path": str(source),
            "parameter_name": "gap",
            "parameter_values": [10.0, 11.0],
            "expressions": ["A"],
            "execution_backend": _backend(),
        }
    )
    store = JobStore(ascii_job_root / "runtime" / "jobs")
    identity = process_identity(os.getpid())
    job_id = store.create(
        spec,
        {
            "schema_version": "1",
            "status": "submitted",
            "attempt": 1,
            "worker_pid": identity["pid"],
            "worker_process_create_time": identity["process_create_time"],
            "worker_command_signature": identity["command_signature"],
            "progress": {"completed": 0, "total": 2},
        },
    )
    events = []
    runner_save_copy = []

    class FakeOwnership:
        def __init__(self, *_args, **_kwargs):
            pass

        def acquire_attached(self, server):
            events.append(("acquire_attached", server.identity_sha256))
            return {
                "success": True,
                "lease": {"acquisition_id": "c" * 32},
            }

        def preflight(self, **_kwargs):
            raise AssertionError("attached worker must not run standalone preflight")

        def acquire(self, **_kwargs):
            raise AssertionError("attached worker must not acquire owned resources")

        def heartbeat(self, **kwargs):
            events.append(("heartbeat", kwargs))
            return True

        def release(self):
            events.append(("release", None))
            return {"success": True, "released": True}

    class FakeJava:
        def tag(self):
            return "Model1"

        def label(self):
            return "working.mph"

        def getFilePath(self):
            return "D:/models/working.mph"

    class FakeModel:
        java = FakeJava()

        def components(self):
            return ["comp1"]

        def studies(self):
            return ["std1"]

        def datasets(self):
            return []

        def parameters(self):
            return {"gap": "10[nm]"}

    class FakeClient:
        def __init__(self, **kwargs):
            events.append(("client", kwargs))
            self.port = kwargs["port"]
            self.model = FakeModel()

        def models(self):
            return [self.model]

        def clear(self):
            raise AssertionError("attached worker must never clear server models")

        def disconnect(self):
            events.append(("disconnect", None))

    def fake_runner(_model, _parameter, values, _expressions, **kwargs):
        runner_save_copy.append(kwargs["save_model_copy"])
        output = Path(kwargs["csv_path"])
        existing = []
        if output.is_file() and output.stat().st_size:
            with output.open(newline="", encoding="utf-8") as handle:
                existing = list(csv.DictReader(handle))
        completed = {
            row["parameter_value"]
            for row in existing
            if row.get("status") == "ok"
        }
        added = 0
        limit = kwargs.get("max_new_points")
        for value in values:
            token = str(value)
            if token in completed or (limit is not None and added >= limit):
                continue
            row = {
                "config_id": spec["spec_fingerprint"],
                "parameter_value": token,
                "status": "ok",
            }
            with output.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(row))
                if handle.tell() == 0:
                    writer.writeheader()
                writer.writerow(row)
                handle.flush()
                os.fsync(handle.fileno())
            kwargs["on_durable_row"](row)
            completed.add(token)
            added += 1
        return {"success": True, "stop_reason": None}

    monkeypatch.setattr(ownership_module, "SolverOwnership", FakeOwnership)
    monkeypatch.setattr(workflow_module, "run_staged_parametric_sweep", fake_runner)
    monkeypatch.setitem(sys.modules, "mph", types.SimpleNamespace(Client=FakeClient))

    code = production_worker.run(str(store.root), job_id)

    state = store.read_state(job_id)
    assert code == 0
    assert state["status"] == "completed"
    assert state["progress"] == {"completed": 2, "total": 2}
    assert state["attached_execution"]["resource_ownership"] == (
        "external_user_owned_server"
    )
    assert runner_save_copy == [True, True]
    assert events[0][0] == "acquire_attached"
    assert ("client", {"host": "127.0.0.1", "port": 2036}) in events
    assert events[-2:] == [("disconnect", None), ("release", None)]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == spec[
        "source_model_sha256"
    ]


def _attached_process_snapshot(*, server_pid=4200, server_created=1234.5):
    return {
        "inventory_complete": True,
        "observed_at_epoch": 3000.0,
        "processes": [
            {
                "pid": 4100,
                "parent_pid": 0,
                "kind": "comsol_desktop",
                "create_time": 1200.0,
                "command_signature": "d" * 64,
                "file_version": "6.4.0.293",
                "window_count": 1,
                "responding": True,
            },
            {
                "pid": server_pid,
                "parent_pid": 0,
                "kind": "comsol_server",
                "create_time": server_created,
                "command_signature": "a" * 64,
                "file_version": "6.4.0.293",
                "window_count": 0,
                "responding": True,
            },
        ],
        "listeners": [{"host": "::", "port": 2036, "pid": server_pid}],
    }


def test_attached_manager_preflight_is_process_only_and_binds_server_identity(
    ascii_job_root, monkeypatch
):
    import src.shared_session.process_probe as process_probe

    source = ascii_job_root / "immutable-source.mph"
    source.write_bytes(b"immutable source")
    spec = validate_staged_sweep_spec(
        {
            "job_type": "staged_sweep",
            "source_model_path": str(source),
            "parameter_name": "gap",
            "parameter_values": [10.0],
            "expressions": ["A"],
            "execution_backend": _backend(),
        }
    )
    monkeypatch.setattr(
        process_probe,
        "collect_shared_preflight_snapshot",
        lambda: _attached_process_snapshot(),
    )

    store = JobStore(ascii_job_root / "runtime" / "jobs")
    manager = JobManager(
        store.root,
        reconcile_on_start=False,
    )
    preflight = manager._run_preflight(spec)

    assert preflight["success"] is True
    assert preflight["ready"] is True
    assert preflight["state"] == "ready_for_attached_worker"
    assert preflight["server_identity_sha256"] == spec["execution_backend"][
        "attached_server"
    ]["identity_sha256"]
    assert preflight["mph_imported"] is False
    assert preflight["client_constructed"] is False


def test_attached_manager_preflight_rejects_changed_server_process(
    ascii_job_root, monkeypatch
):
    import src.shared_session.process_probe as process_probe

    source = ascii_job_root / "immutable-source.mph"
    source.write_bytes(b"immutable source")
    spec = validate_staged_sweep_spec(
        {
            "job_type": "staged_sweep",
            "source_model_path": str(source),
            "parameter_name": "gap",
            "parameter_values": [10.0],
            "expressions": ["A"],
            "execution_backend": _backend(),
        }
    )
    monkeypatch.setattr(
        process_probe,
        "collect_shared_preflight_snapshot",
        lambda: _attached_process_snapshot(server_pid=4300),
    )
    manager = JobManager(
        ascii_job_root / "runtime" / "jobs",
        reconcile_on_start=False,
    )

    preflight = manager._run_preflight(spec)

    assert preflight["success"] is False
    assert preflight["ready"] is False
    assert preflight["state"] == "attached_server_identity_changed"
