"""JobManager and synthetic robust shape worker lifecycle tests."""

from __future__ import annotations

import os

import pytest

from comsol_mcp.jobs import (
    robust_condition_runtime,
    robust_shape_native_runtime,
    robust_shape_worker,
)
from comsol_mcp.jobs.manager import JobManager
from comsol_mcp.jobs.robust_condition_runtime import execute_robust_conditions
from comsol_mcp.jobs.robust_shape_rows import append_robust_shape_row, read_robust_shape_rows
from comsol_mcp.jobs.robust_shape_worker import run as run_robust_worker
from comsol_mcp.jobs.store import atomic_write_json, process_identity, read_json
from development_kit.tests.test_robust_shape_optimization import _write_manifest


def _condition_runtime_spec() -> dict:
    conditions = []
    for index, state_id in enumerate(("OX", "MR")):
        conditions.append(
            {
                "condition_id": f"condition-{index}",
                "order": index,
                "wavelength_m": 8e-7,
                "incidence_elevation_deg": 0.0,
                "incidence_azimuth_deg": 0.0,
                "polarization_basis_id": "x_linear",
                "material_state_id": state_id,
                "objective_role": "objective",
                "observable_id": "transmission_order_0_0",
                "active": True,
            }
        )
    return {
        "spec_fingerprint": "f" * 64,
        "condition_table": {"conditions": conditions},
        "adapter_configuration": {
            "configuration": {
                "condition_controls": {
                    "dataset_tag": "dset1",
                    "solution_tag": "sol1",
                },
                "material_tensor_rows": {
                    "states": [
                        {
                            "state_id": state_id,
                            "rows": [
                                {
                                    "wavelength_m": 8e-7,
                                    "xx_real": 2.0,
                                    "xx_imag": -0.1,
                                    "yy_real": 2.0,
                                    "yy_imag": -0.1,
                                    "zz_real": 3.0,
                                    "zz_imag": -0.2,
                                }
                            ],
                        }
                        for state_id in ("OX", "MR")
                    ]
                }
            }
        },
        "finalist_validation_policy": {
            "mesh_convergence": {
                "max_elements_per_model": 1000,
                "minimum_element_quality": 0.2,
            }
        },
    }


class _ConditionBackend:
    def __init__(
        self,
        *,
        dataset_id="dset1",
        solution_id="sol1",
        mesh_elements=1000,
        minimum_mesh_quality=0.2,
    ):
        self.calls = []
        self.dataset_id = dataset_id
        self.solution_id = solution_id
        self.mesh_elements = mesh_elements
        self.minimum_mesh_quality = minimum_mesh_quality

    def evaluate_condition(self, condition, tensor_expressions):
        self.calls.append((condition["condition_id"], tensor_expressions))
        return {
            "condition_id": condition["condition_id"],
            "observable_id": condition["observable_id"],
            "observable_value": 0.6 + condition["order"] * 0.1,
            "requested_wavelength_m": condition["wavelength_m"],
            "evaluated_wavelength_m": condition["wavelength_m"],
            "solved_wavelength_m": condition["wavelength_m"],
            "reflectance": 0.2,
            "transmittance": 0.6,
            "absorption": 0.2,
            "mesh_elements": self.mesh_elements,
            "minimum_mesh_quality": self.minimum_mesh_quality,
            "dataset_id": self.dataset_id,
            "solution_id": self.solution_id,
        }


def test_condition_runtime_persists_receipts_rows_and_exact_replay(ascii_tmp_path):
    spec = _condition_runtime_spec()
    backend = _ConditionBackend()
    observations = execute_robust_conditions(
        spec,
        ascii_tmp_path,
        attempt=1,
        backend=backend,
        cancel_requested=lambda: False,
    )
    assert [item["value"] for item in observations] == [0.6, 0.7]
    assert len(backend.calls) == 2
    replay_backend = _ConditionBackend()
    replay = execute_robust_conditions(
        spec,
        ascii_tmp_path,
        attempt=1,
        backend=replay_backend,
        cancel_requested=lambda: False,
    )
    assert replay == observations
    assert replay_backend.calls == []


def test_condition_runtime_recovers_receipt_written_before_row(
    ascii_tmp_path, monkeypatch
):
    spec = _condition_runtime_spec()
    spec["condition_table"]["conditions"] = spec["condition_table"]["conditions"][:1]
    backend = _ConditionBackend()
    original = robust_condition_runtime.append_robust_shape_row
    monkeypatch.setattr(
        robust_condition_runtime,
        "append_robust_shape_row",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected row failure")),
    )
    with pytest.raises(OSError, match="injected row failure"):
        execute_robust_conditions(
            spec,
            ascii_tmp_path,
            attempt=1,
            backend=backend,
            cancel_requested=lambda: False,
        )
    assert len(backend.calls) == 1
    monkeypatch.setattr(robust_condition_runtime, "append_robust_shape_row", original)
    recovery_backend = _ConditionBackend()
    execute_robust_conditions(
        spec,
        ascii_tmp_path,
        attempt=1,
        backend=recovery_backend,
        cancel_requested=lambda: False,
    )
    assert recovery_backend.calls == []


@pytest.mark.parametrize(
    ("backend", "message"),
    [
        (_ConditionBackend(dataset_id="dset-other"), "dataset identity"),
        (_ConditionBackend(solution_id="sol-other"), "solution identity"),
    ],
)
def test_condition_runtime_rejects_dataset_or_solution_drift(
    ascii_tmp_path, backend, message
):
    spec = _condition_runtime_spec()
    spec["condition_table"]["conditions"] = spec["condition_table"]["conditions"][:1]
    with pytest.raises(ValueError, match=message):
        execute_robust_conditions(
            spec,
            ascii_tmp_path,
            attempt=1,
            backend=backend,
            cancel_requested=lambda: False,
        )
    assert not (ascii_tmp_path / "condition-0000.json").exists()


@pytest.mark.parametrize(
    ("backend", "message"),
    [
        (_ConditionBackend(mesh_elements=1001), "element cap"),
        (_ConditionBackend(minimum_mesh_quality=0.199), "quality"),
    ],
)
def test_condition_runtime_enforces_caller_mesh_admission(
    ascii_tmp_path, backend, message
):
    spec = _condition_runtime_spec()
    spec["condition_table"]["conditions"] = spec["condition_table"]["conditions"][:1]
    with pytest.raises(ValueError, match=message):
        execute_robust_conditions(
            spec,
            ascii_tmp_path,
            attempt=1,
            backend=backend,
            cancel_requested=lambda: False,
        )
    assert not (ascii_tmp_path / "condition-0000.json").exists()


class _LoadFailureClient:
    port = None

    def __init__(self, *, clear_fails=False):
        self.clear_fails = clear_fails
        self.clear_calls = 0

    def load(self, _path):
        raise RuntimeError("injected load failure")

    def clear(self):
        self.clear_calls += 1
        if self.clear_fails:
            raise RuntimeError("injected clear failure")


@pytest.mark.parametrize("clear_fails", [False, True])
def test_native_runtime_records_observed_client_cleanup_on_startup_failure(
    ascii_tmp_path, clear_fails
):
    source = ascii_tmp_path / "source.mph"
    source.write_bytes(b"fixture")
    client = _LoadFailureClient(clear_fails=clear_fails)
    with pytest.raises(RuntimeError, match="injected load failure"):
        robust_shape_native_runtime.execute_lin2025_conditions(
            {
                "source_model_path": str(source),
                "cores": 2,
                "version": "6.4",
            },
            ascii_tmp_path,
            attempt=1,
            client_factory=lambda **_kwargs: client,
            cancel_requested=lambda: False,
        )
    cleanup = read_json(ascii_tmp_path / "native-cleanup.json")
    assert client.clear_calls == 1
    assert cleanup["client_clear"] is (not clear_fails)
    assert cleanup["source_model_removed"] is False
    assert cleanup["working_model_removed"] is False
    assert cleanup["client_disconnect"] == "not_applicable"
    assert cleanup["errors"] == ([] if not clear_fails else ["client_clear:RuntimeError"])


class _CleanupOwnership:
    def __init__(self, *, inventory_complete=True, external=None, release=True):
        self.inventory_complete = inventory_complete
        self.external = external or []
        self.release_result = release
        self.status_calls = []

    def release(self):
        return {"success": self.release_result, "released": self.release_result}

    def status(self, *, require_fresh_inventory=False):
        self.status_calls.append(require_fresh_inventory)
        return {
            "process_inventory": {"complete": self.inventory_complete},
            "lease": {"state": "absent" if self.release_result else "active"},
            "external_solver_processes": self.external,
        }


def test_licensed_cleanup_uses_native_receipt_and_fresh_ownership_status(ascii_tmp_path):
    atomic_write_json(
        ascii_tmp_path / "native-cleanup.json",
        {
            "source_model_removed": True,
            "working_model_removed": True,
            "client_clear": True,
            "client_disconnect": "not_applicable",
            "errors": [],
        },
    )
    ownership = _CleanupOwnership()
    payload = robust_shape_worker._finalize_licensed_cleanup(
        ascii_tmp_path,
        ownership=ownership,
        lease_acquired=True,
        native_runtime_entered=True,
        source_unchanged=True,
    )
    assert ownership.status_calls == [True]
    assert all(payload.values())
    receipt = read_json(ascii_tmp_path / "licensed-cleanup.json")
    assert receipt["errors"] == []
    assert receipt["inventory_fingerprint"]
    assert receipt["native_cleanup_fingerprint"]


def test_licensed_cleanup_fails_closed_on_false_clear_or_incomplete_inventory(
    ascii_tmp_path,
):
    atomic_write_json(
        ascii_tmp_path / "native-cleanup.json",
        {"client_clear": False, "errors": ["client_clear:RuntimeError"]},
    )
    payload = robust_shape_worker._finalize_licensed_cleanup(
        ascii_tmp_path,
        ownership=_CleanupOwnership(inventory_complete=False),
        lease_acquired=True,
        native_runtime_entered=True,
        source_unchanged=True,
    )
    assert payload["client_clear"] is False
    assert payload["owned_processes_absent"] is False
    assert payload["lease_released"] is True
    assert read_json(ascii_tmp_path / "licensed-cleanup.json")["errors"] == [
        "native_cleanup:reported_errors",
        "owned_processes_absent:unproved"
    ]


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
        "row_count": 29,
        "last_row_sha256": rows[-1]["row_sha256"],
        "cleanup_recorded": True,
    }
    assert [row["kind"] for row in rows[-5:]] == [
        "gradient",
        "iteration",
        "finalist_validation",
        "checkpoint",
        "cleanup",
    ]
    finalist = read_json(manager.store.job_dir(submitted["job_id"]) / "finalist-validation.json")
    assert finalist["accepted"] is True
    assert terminal["finalist_validation_fingerprint"] == finalist["receipt_fingerprint"]


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


def test_rejected_finalist_is_durable_and_cleans_before_terminal_failure(
    ascii_tmp_path, monkeypatch
):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = _manager(ascii_tmp_path / "jobs", monkeypatch)
    original = robust_shape_worker._synthetic_finalist_evidence

    def rejected_evidence(spec, candidate, objective_value):
        evidence = original(spec, candidate, objective_value)
        evidence["manufacturability"]["minimum_gap_m"] = 0.0
        return evidence

    monkeypatch.setattr(robust_shape_worker, "_synthetic_finalist_evidence", rejected_evidence)
    submitted = manager.submit(envelope)
    job_id = submitted["job_id"]
    assert run_robust_worker(str(manager.store.root), job_id) == 1
    spec = manager.store.read_spec(job_id)
    rows = read_robust_shape_rows(
        manager.store.job_dir(job_id) / "robust_shape_rows.jsonl",
        job_fingerprint=spec["spec_fingerprint"],
    )
    assert [row["kind"] for row in rows[-2:]] == ["finalist_validation", "cleanup"]
    assert not any(row["kind"] == "checkpoint" for row in rows)
    assert rows[-2]["payload"]["status"] == "rejected"
    assert rows[-2]["payload"]["reason_codes"] == ["manufacturability_failed"]
    state = manager.store.read_state(job_id)
    assert state["status"] == "failed"
    assert state["solver_started"] is False
