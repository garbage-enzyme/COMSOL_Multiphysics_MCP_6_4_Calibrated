"""Detached production worker for one durable staged COMSOL sweep job."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from dataclasses import replace
from typing import Any

from .process_control import contain_current_process_tree
from .attached_runtime import (
    AttachedExecutionTarget,
    normalize_attached_execution_target,
    verify_attached_model_inventory,
    verify_attached_model_revision,
    verify_attached_process_preservation,
)
from .store import JobStore, atomic_write_json, cancel_request_targets_attempt, process_identity
from src.shared_session.locking import build_shared_model_revision


def _valid_row_count(csv_path: Path, config_id: str) -> int:
    if not csv_path.is_file() or not csv_path.stat().st_size:
        return 0
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        return len(
            {
                row.get("parameter_value")
                for row in csv.DictReader(handle)
                if row.get("status") == "ok" and row.get("config_id") == config_id
            }
        )


def _resource_gate_stop(store: JobStore, job_id: str, result: dict[str, Any]) -> bool:
    """Checkpoint a bounded resource stop as resumable, not as a solve failure."""
    reason = result.get("stop_reason")
    if reason not in {
        "before_point_await_confirmation",
        "before_point_checkpoint_no_start",
        "after_durable_row_await_confirmation",
        "after_durable_row_checkpoint_no_start",
    }:
        return False
    entries = store.read_resource_journal(job_id)
    latest = entries[-1] if entries else None
    gate = {
        "stop_reason": reason,
        "latest_entry_sha256": latest.get("entry_sha256") if latest else None,
        "point_id": latest.get("point_id") if latest else None,
        "stage": latest.get("stage") if latest else None,
        "decision": latest.get("decision") if latest else None,
    }
    store.update_state(
        job_id,
        "interrupted",
        patch={
            "resource_gate": gate,
            "last_error": {
                "type": "ResourceAdmissionStop",
                "message": f"Resource admission stopped the worker: {reason}",
            },
        },
        event="resource_gate_stopped",
        event_data=gate,
    )
    return True


def _runner_kwargs(spec: dict[str, Any], directory: Path) -> dict[str, Any]:
    return {
        "parameter_unit": spec.get("parameter_unit"),
        "study_name": spec.get("study_name"),
        "study_step_tag": spec.get("study_step_tag"),
        "study_step_property": spec.get("study_step_property", "plist"),
        "study_step_unit": spec.get("study_step_unit"),
        "study_step_unit_property": spec.get("study_step_unit_property", "punit"),
        "csv_path": str(directory / "results.csv"),
        "resume_csv": True,
        "max_retries": int(spec.get("max_retries", 0)),
        "continue_on_error": bool(spec.get("continue_on_error", False)),
        "checkpoint_model_path": str(directory / "checkpoint.mph"),
        "checkpoint_every": int(spec.get("checkpoint_every", 1)),
        "save_model_copy": spec.get("execution_backend") is not None,
        "manifest_path": str(directory / "results.csv.manifest.json"),
        "source_model_path": spec["source_model_path"],
        "config_id": spec["spec_fingerprint"],
        "record_wavelength_controls": spec.get("record_wavelength_controls"),
        "physical_bounds": spec.get("physical_bounds"),
        "response_tail": 2,
    }


def _source_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _attached_revision_from_client(
    client: Any,
    target: AttachedExecutionTarget,
    *,
    sequence: int,
) -> dict[str, Any]:
    from src.shared_session.lifecycle import (
        _default_model_inventory_reader,
        _default_model_revision_reader,
    )

    inventory = _default_model_inventory_reader(client)
    verify_attached_model_inventory(target, inventory)
    structural, state = _default_model_revision_reader(client, target.model.tag)
    return build_shared_model_revision(
        target.model,
        sequence=sequence,
        structural_readback=structural,
        state_readback=state,
    ).to_dict()


def _select_attached_model(
    client: Any,
    target: AttachedExecutionTarget,
    expected_revision: dict[str, Any],
):
    from src.shared_session.lifecycle import (
        _default_model_inventory_reader,
        _default_model_revision_reader,
    )

    inventory = _default_model_inventory_reader(client)
    verify_attached_model_inventory(target, inventory)
    structural, state = _default_model_revision_reader(client, target.model.tag)
    verify_attached_model_revision(
        replace(target, expected_revision=expected_revision),
        structural_readback=structural,
        state_readback=state,
    )
    matches = [
        model
        for model in list(client.models())
        if str(model.java.tag()) == target.model.tag
    ]
    if len(matches) != 1:
        raise ValueError("attached server model changed after revision verification")
    return matches[0]


def _persisted_attached_revision(
    state: dict[str, Any], target: AttachedExecutionTarget
) -> dict[str, Any]:
    attached_execution = state.get("attached_execution")
    if not isinstance(attached_execution, dict):
        return dict(target.expected_revision)
    revision = attached_execution.get("current_revision")
    if revision is None:
        return dict(target.expected_revision)
    if not isinstance(revision, dict):
        raise ValueError("persisted attached current revision is not an object")
    return dict(revision)


def _attached_point_start_result(context: dict[str, Any]) -> dict[str, Any]:
    """Return the attached revision gate result accepted by the sweep hook contract."""
    return {
        "action": "start_point",
        "start_authorized": True,
        "stage": context.get("stage"),
        "point_id": context.get("point_id"),
    }


def _collect_attached_process_preservation(
    target: AttachedExecutionTarget,
) -> dict[str, Any]:
    from src.shared_session.process_probe import collect_shared_preflight_snapshot

    return verify_attached_process_preservation(
        target,
        first_probe=collect_shared_preflight_snapshot(),
        second_probe=collect_shared_preflight_snapshot(),
    )


def _cleanup_attached_execution(
    *,
    client: Any,
    ownership: Any,
    lease_acquired: bool,
    target: AttachedExecutionTarget,
) -> dict[str, Any]:
    from src.shared_session.lifecycle import _default_model_inventory_reader

    model_identity_preserved = False
    model_error = None
    client_disconnected = client is None
    if client is not None:
        try:
            inventory = _default_model_inventory_reader(client)
            verify_attached_model_inventory(target, inventory)
            model_identity_preserved = True
        except Exception as exc:
            model_error = f"{type(exc).__name__}: {exc}"
        try:
            client.disconnect()
            client_disconnected = True
        except Exception as exc:
            client_disconnected = False
            if model_error is None:
                model_error = f"{type(exc).__name__}: {exc}"
    release = {
        "success": not lease_acquired,
        "released": False,
        "message": "No attached lease was acquired.",
    }
    if lease_acquired and client_disconnected:
        release = ownership.release()
    elif lease_acquired:
        release = {
            "success": False,
            "released": False,
            "error": "Attached lease retained because client disconnect was not verified.",
        }
    lease_path = getattr(ownership, "lease_path", None)
    lease_absent = bool(
        release.get("success")
        and (lease_path is None or not Path(lease_path).exists())
    )
    try:
        preservation = _collect_attached_process_preservation(target)
    except Exception as exc:
        preservation = {
            "success": False,
            "state": "attached_preservation_probe_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    success = bool(
        client is not None
        and model_identity_preserved
        and client_disconnected
        and lease_absent
        and preservation.get("success")
    )
    return {
        "success": success,
        "state": "attached_cleanup_verified" if success else "attached_cleanup_unverified",
        "model_identity_preserved": model_identity_preserved,
        "model_error": model_error,
        "client_disconnected": client_disconnected,
        "lease_release": release,
        "lease_absent": lease_absent,
        "external_resources": preservation,
        "model_clear_attempted": False,
        "external_server_shutdown_attempted": False,
        "external_server_termination_attempted": False,
    }


def _record_native_cancel(store: JobStore, job_id: str, attempt: int, result: dict[str, Any]) -> bool:
    """Merge native-cancel evidence without overwriting coordinator state."""
    with store.lock(job_id):
        state = store.read_state(job_id)
        control = store.read_control(job_id)
        if (
            control.get("request") != "cancel_requested"
            or int(control.get("target_attempt", -1)) != int(attempt)
            or state.get("status") not in {"cancel_requested", "cancelling"}
        ):
            return False
        cancel = dict(state.get("cancel") or {})
        cancel["native"] = dict(result)
        state["cancel"] = cancel
        state["updated_at_epoch"] = time.time()
        atomic_write_json(store.job_dir(job_id) / "state.json", state)
        store._append_event_unlocked(job_id, "native_cancel_attempted", result, state["status"])
        return True


def _run(root: str, job_id: str) -> int:
    worker_started_monotonic = time.monotonic()
    process_tree_contained = contain_current_process_tree()
    store = JobStore(Path(root))
    directory = store.job_dir(job_id)
    spec = store.read_spec(job_id)
    if spec.get("job_type") != "staged_sweep":
        raise ValueError("Production worker accepts only staged_sweep jobs")
    identity = process_identity(os.getpid())
    deadline = time.monotonic() + 3.0
    while store.read_state(job_id).get("worker_pid") != identity["pid"]:
        if time.monotonic() >= deadline:
            raise RuntimeError("Control plane did not durably record the worker identity")
        time.sleep(0.01)

    store.update_state(
        job_id,
        patch={"process_tree_contained": bool(process_tree_contained)},
        event="worker_containment_recorded",
        event_data={"process_tree_contained": bool(process_tree_contained)},
    )

    state = store.read_state(job_id)
    attempt = int(state.get("attempt", 1))
    if state["status"] == "cancel_requested" or cancel_request_targets_attempt(store.read_control(job_id), attempt):
        store.record_cooperative_cancel_observed(
            job_id,
            attempt=attempt,
            message="Stopped before startup",
        )
        return 0
    if state["status"] == "submitted":
        store.update_state(job_id, "starting", event="worker_started")
    elif state["status"] != "starting":
        raise ValueError(f"Production worker cannot start from {state['status']}")

    client = None
    ownership = None
    lease_acquired = False
    attached_target: AttachedExecutionTarget | None = None
    attached_completion_count: int | None = None
    native_monitor_stop = threading.Event()
    native_monitor: threading.Thread | None = None
    try:
        from src.tools.ownership import SolverOwnership

        ownership = SolverOwnership(
            store.root.parent,
            owner=f"job:{job_id}",
        )
        if spec.get("execution_backend") is not None:
            attached_target = normalize_attached_execution_target(
                spec["execution_backend"]
            )
            if _source_sha256(spec["source_model_path"]) != spec["source_model_sha256"]:
                raise RuntimeError(
                    "Immutable source SHA-256 changed before attached worker startup"
                )
            claim = ownership.acquire_attached(attached_target.server)
        else:
            preflight = ownership.preflight(
                model_path=spec["source_model_path"],
                output_path=str(directory / "results.csv"),
                requested_version=spec.get("version"),
            )
            if not preflight["ready"]:
                raise RuntimeError(f"Worker preflight failed: {preflight['blockers']}")
            claim = ownership.acquire(
                mode="durable-job", model_path=spec["source_model_path"]
            )
        if not claim["success"]:
            raise RuntimeError(claim["error"])
        lease_acquired = True

        import mph
        from src.jobs.resource_admission import ResourceStageAdapter, collect_resource_telemetry
        from src.tools.mesh import get_mesh_info
        from src.tools.workflow import _sweep_point_id, run_staged_parametric_sweep

        if attached_target is not None:
            acquisition_id = claim.get("lease", {}).get("acquisition_id")
            if not isinstance(acquisition_id, str) or len(acquisition_id) != 32:
                raise RuntimeError("Attached lease acquisition identity is unavailable")
            client = mph.Client(
                host=attached_target.server.endpoint.host,
                port=attached_target.server.endpoint.port,
            )
            if not ownership.heartbeat():
                raise RuntimeError("Attached lease heartbeat failed after connection")
            expected_revision = _persisted_attached_revision(state, attached_target)
            model = _select_attached_model(client, attached_target, expected_revision)
            attached_execution = {
                "backend_identity_sha256": attached_target.backend[
                    "backend_identity_sha256"
                ],
                "server_identity_sha256": attached_target.server.identity_sha256,
                "model_identity_sha256": attached_target.model.identity_sha256,
                "initial_revision_sha256": attached_target.expected_revision[
                    "revision_sha256"
                ],
                "current_revision": expected_revision,
                "lease_acquisition_id": acquisition_id,
                "resource_ownership": "external_user_owned_server",
                "initial_revision_verified": True,
            }
            store.update_state(
                job_id,
                patch={"attached_execution": attached_execution},
                event="attached_target_verified",
                event_data=attached_execution,
            )
        else:
            client_kwargs = {
                "cores": spec.get("cores"),
                "version": spec.get("version"),
            }
            client = mph.Client(
                **{key: value for key, value in client_kwargs.items() if value is not None}
            )
            ownership.heartbeat(
                model_path=spec["source_model_path"],
                refresh_server_processes=True,
            )
            model = client.load(spec["source_model_path"])

        resource_adapter = None
        if spec.get("resource_policy") is not None:
            results_path = directory / "results.csv"

            def completed_point_ids() -> set[str]:
                if not results_path.is_file() or not results_path.stat().st_size:
                    return set()
                with results_path.open(newline="", encoding="utf-8-sig") as handle:
                    return {
                        _sweep_point_id(spec["parameter_name"], str(row["parameter_value"]))
                        for row in csv.DictReader(handle)
                        if row.get("status") == "ok"
                        and row.get("config_id") == spec["spec_fingerprint"]
                        and row.get("parameter_value")
                    }

            def telemetry_provider(stage: str, _point_id: str) -> dict[str, Any]:
                mesh_elements = None
                try:
                    mesh_result = get_mesh_info(model)
                    if mesh_result.get("success"):
                        mesh_elements = mesh_result.get("mesh", {}).get("num_elements")
                except Exception:
                    # The collector and admission result preserve unavailable
                    # mesh evidence; never invent a count or weaken a policy.
                    pass
                durable_result_epoch = (
                    results_path.stat().st_mtime if results_path.is_file() else None
                )
                return collect_resource_telemetry(
                    stage=stage,
                    runtime_path=store.root,
                    process_id=os.getpid(),
                    mesh_elements=mesh_elements,
                    elapsed_wall_seconds=time.monotonic() - worker_started_monotonic,
                    durable_result_epoch=durable_result_epoch,
                )

            resource_adapter = ResourceStageAdapter(
                store=store,
                job_id=job_id,
                attempt=attempt,
                policy=spec["resource_policy"],
                telemetry_provider=telemetry_provider,
                completed_point_ids_provider=completed_point_ids,
            )

        def resource_hook(context: dict[str, Any]) -> dict[str, Any]:
            if resource_adapter is None:
                raise RuntimeError("resource hook is disabled")
            if context.get("config_id") != spec["spec_fingerprint"]:
                raise ValueError("resource hook received a mismatched configuration")
            return resource_adapter.evaluate(
                stage=str(context["stage"]),
                point_id=str(context["point_id"]),
            )

        def before_attached_point_hook(context: dict[str, Any]) -> dict[str, Any]:
            if attached_target is None:
                raise RuntimeError("attached revision hook is unavailable")
            expected = _persisted_attached_revision(
                store.read_state(job_id), attached_target
            )
            current = _attached_revision_from_client(
                client,
                attached_target,
                sequence=int(expected["sequence"]),
            )
            if current != expected:
                raise RuntimeError(
                    "ExternalModelChangeDetected: attached model revision differs "
                    "from the last durable revision"
                )
            if resource_adapter is not None:
                return resource_hook(context)
            return _attached_point_start_result(context)

        progress = {
            "completed": _valid_row_count(directory / "results.csv", spec["spec_fingerprint"]),
            "total": len(spec["parameter_values"]),
        }

        def should_stop() -> bool:
            return cancel_request_targets_attempt(store.read_control(job_id), attempt)

        def native_cancel_monitor() -> None:
            while not native_monitor_stop.wait(0.05):
                if not should_stop():
                    continue
                from src.jobs.native_cancel_probe import request_native_cancel_once

                result = request_native_cancel_once()
                _record_native_cancel(store, job_id, attempt, result)
                return

        native_monitor = threading.Thread(target=native_cancel_monitor, name="comsol-native-cancel", daemon=True)
        native_monitor.start()

        def on_row(row: dict[str, Any]) -> None:
            progress["completed"] = _valid_row_count(
                directory / "results.csv", spec["spec_fingerprint"]
            )
            store.update_state(
                job_id,
                patch={"progress": dict(progress), "last_row": row},
                event="durable_row",
                event_data={"status": row.get("status"), "parameter_value": row.get("parameter_value")},
            )
            if attached_target is not None:
                current_state = store.read_state(job_id)
                expected = _persisted_attached_revision(
                    current_state, attached_target
                )
                current_revision = _attached_revision_from_client(
                    client,
                    attached_target,
                    sequence=int(expected["sequence"]) + 1,
                )
                attached_execution = dict(
                    current_state.get("attached_execution") or {}
                )
                attached_execution["current_revision"] = current_revision
                store.update_state(
                    job_id,
                    patch={"attached_execution": attached_execution},
                    event="attached_revision_advanced",
                    event_data={
                        "revision_sha256": current_revision["revision_sha256"],
                        "sequence": current_revision["sequence"],
                    },
                )
            heartbeat = (
                ownership.heartbeat()
                if attached_target is not None
                else ownership.heartbeat(
                    model_path=spec["source_model_path"],
                    refresh_server_processes=True,
                )
            )
            if not heartbeat:
                raise RuntimeError("Solver lease heartbeat failed after durable row")

        if should_stop():
            store.record_cooperative_cancel_observed(
                job_id,
                attempt=attempt,
                message="Stopped before smoke",
            )
            return 0
        store.update_state(job_id, "smoke_running", event="smoke_started")
        existing = progress["completed"]
        smoke_needed = max(0, int(spec["smoke_points"]) - existing)
        common = _runner_kwargs(spec, directory)
        before_point_hook = (
            before_attached_point_hook
            if attached_target is not None
            else (resource_hook if resource_adapter is not None else None)
        )
        after_point_hook = resource_hook if resource_adapter is not None else None
        smoke = run_staged_parametric_sweep(
            model,
            spec["parameter_name"],
            spec["parameter_values"],
            spec["expressions"],
            **common,
            max_new_points=smoke_needed,
            should_stop=should_stop,
            on_durable_row=on_row,
            before_point_hook=before_point_hook,
            after_durable_row_hook=after_point_hook,
        )
        if should_stop() or smoke.get("stop_reason") == "control_request":
            store.record_cooperative_cancel_observed(
                job_id,
                attempt=attempt,
                message="Stopped between points",
            )
            return 0
        if _resource_gate_stop(store, job_id, smoke):
            return 0
        if not smoke.get("success") or _valid_row_count(
            directory / "results.csv", spec["spec_fingerprint"]
        ) < int(spec["smoke_points"]):
            raise RuntimeError(f"Smoke sweep failed: {smoke}")
        store.update_state(job_id, "smoke_validated", event="smoke_validated")

        if _valid_row_count(directory / "results.csv", spec["spec_fingerprint"]) < len(
            spec["parameter_values"]
        ):
            store.update_state(job_id, "running", event="broad_phase_started")
            broad = run_staged_parametric_sweep(
                model,
                spec["parameter_name"],
                spec["parameter_values"],
                spec["expressions"],
                **common,
                should_stop=should_stop,
                on_durable_row=on_row,
                before_point_hook=before_point_hook,
                after_durable_row_hook=after_point_hook,
            )
            if should_stop() or broad.get("stop_reason") == "control_request":
                store.record_cooperative_cancel_observed(
                    job_id,
                    attempt=attempt,
                    message="Stopped between points",
                )
                return 0
            if _resource_gate_stop(store, job_id, broad):
                return 0
            if not broad.get("success"):
                raise RuntimeError(f"Broad sweep failed: {broad}")
        completed = _valid_row_count(directory / "results.csv", spec["spec_fingerprint"])
        if completed != len(spec["parameter_values"]):
            raise RuntimeError(f"Expected {len(spec['parameter_values'])} valid rows, found {completed}")
        if attached_target is not None:
            attached_completion_count = completed
        else:
            store.update_state(
                job_id,
                "completed",
                patch={"progress": {"completed": completed, "total": completed}},
                event="completed",
            )
        return 0
    except Exception as exc:
        current = store.read_state(job_id)["status"]
        if current == "cancel_requested":
            store.record_cooperative_cancel_observed(
                job_id,
                attempt=attempt,
                message="Stopped between blocking operations",
            )
        elif current == "cancelling":
            # The detached coordinator owns the terminal decision once it has
            # claimed a cancellation request. It will verify process/lease
            # cleanup after this worker exits.
            pass
        elif current not in {"completed", "interrupted"}:
            store.update_state(
                job_id,
                "failed",
                patch={"last_error": {"type": type(exc).__name__, "message": str(exc)}},
                event="worker_failed",
            )
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        native_monitor_stop.set()
        if native_monitor is not None:
            native_monitor.join(timeout=1.0)
        if attached_target is not None:
            cleanup = _cleanup_attached_execution(
                client=client,
                ownership=ownership,
                lease_acquired=lease_acquired,
                target=attached_target,
            )
            current = store.read_state(job_id)["status"]
            if attached_completion_count is not None and current not in {
                "cancel_requested",
                "cancelling",
            }:
                if cleanup["success"]:
                    store.update_state(
                        job_id,
                        "completed",
                        patch={
                            "progress": {
                                "completed": attached_completion_count,
                                "total": attached_completion_count,
                            },
                            "attached_cleanup": cleanup,
                        },
                        event="attached_cleanup_verified",
                        event_data={"success": True},
                    )
                else:
                    store.update_state(
                        job_id,
                        "failed",
                        patch={
                            "attached_cleanup": cleanup,
                            "last_error": {
                                "type": "AttachedCleanupUnverified",
                                "message": "Attached resources could not be proved preserved.",
                            },
                        },
                        event="attached_cleanup_unverified",
                        event_data={"success": False},
                    )
            elif current != "completed":
                store.update_state(
                    job_id,
                    patch={"attached_cleanup": cleanup},
                    event=(
                        "attached_cleanup_verified"
                        if cleanup["success"]
                        else "attached_cleanup_unverified"
                    ),
                    event_data={"success": cleanup["success"]},
                )
        else:
            if client is not None:
                try:
                    client.clear()
                except Exception as exc:
                    print(f"Client clear warning: {exc}", file=sys.stderr, flush=True)
                if getattr(client, "port", None):
                    try:
                        client.disconnect()
                    except Exception as exc:
                        print(f"Client disconnect warning: {exc}", file=sys.stderr, flush=True)
        if attached_target is None and ownership is not None and lease_acquired:
            release = ownership.release()
            if not release.get("success"):
                print(json.dumps(release, ensure_ascii=False), file=sys.stderr, flush=True)


def run(root: str, job_id: str) -> int:
    try:
        return _run(root, job_id)
    except ValueError:
        store = JobStore(Path(root))
        state = store.read_state(job_id)
        if state.get("status") == "cancel_requested":
            store.record_cooperative_cancel_observed(
                job_id,
                attempt=int(state.get("attempt", 1)),
                message="Stopped between state transitions",
            )
            return 0
        raise


if __name__ == "__main__":
    code = run(sys.argv[1], sys.argv[2])
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
