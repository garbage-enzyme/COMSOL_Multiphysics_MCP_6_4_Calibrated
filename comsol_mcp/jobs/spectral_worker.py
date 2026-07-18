"""Detached production worker for one durable adaptive spectral job."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable, Mapping

from .process_control import contain_current_process_tree
from .spectral_characterization import validate_spectral_driver_identity
from .spectral_rows import completed_spectral_point_fingerprints, read_spectral_rows
from .store import JobStore, cancel_request_targets_attempt, process_identity


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _default_ownership_factory(runtime_root: Path, owner: str):
    from comsol_mcp.tools.ownership import SolverOwnership

    return SolverOwnership(runtime_root, owner=owner)


def _default_client_factory(spec: dict[str, Any]):
    import mph

    kwargs = {"cores": spec.get("cores"), "version": spec.get("version")}
    return mph.Client(**{key: value for key, value in kwargs.items() if value is not None})


def _run(
    root: str,
    job_id: str,
    *,
    ownership_factory: Callable[[Path, str], Any] = _default_ownership_factory,
    client_factory: Callable[[dict[str, Any]], Any] = _default_client_factory,
    collector_executor: Callable[[dict[str, Any], dict[str, Any], Path], Mapping[str, Any]] | None = None,
    telemetry_provider: Callable[[str, str, Any, Path, float], dict[str, Any]] | None = None,
    native_cancel_enabled: bool = True,
    fault_hook: Callable[[str, Mapping[str, Any]], Any] | None = None,
) -> int:
    """Run one worker attempt; injected boundaries keep process tests solver-free."""
    worker_started = time.monotonic()
    store = JobStore(Path(root))
    directory = store.job_dir(job_id)
    spec = store.read_spec(job_id)
    if spec.get("job_type") != "spectral_characterization":
        raise ValueError("Spectral worker accepts only spectral_characterization jobs")
    validate_spectral_driver_identity(spec)
    identity = process_identity(os.getpid())
    deadline = time.monotonic() + 3.0
    while store.read_state(job_id).get("worker_pid") != identity["pid"]:
        if time.monotonic() >= deadline:
            raise RuntimeError("Control plane did not durably record the worker identity")
        time.sleep(0.01)
    contained = contain_current_process_tree()
    store.update_state(
        job_id,
        patch={"process_tree_contained": bool(contained)},
        event="worker_containment_recorded",
        event_data={"process_tree_contained": bool(contained)},
    )
    state = store.read_state(job_id)
    attempt = int(state.get("attempt", 1))
    if state["status"] == "cancel_requested" or cancel_request_targets_attempt(
        store.read_control(job_id), attempt
    ):
        store.record_cooperative_cancel_observed(
            job_id, attempt=attempt, message="Stopped before startup"
        )
        return 0
    if state["status"] == "submitted":
        store.update_state(job_id, "starting", event="worker_started")
    elif state["status"] != "starting":
        raise ValueError(f"Spectral worker cannot start from {state['status']}")

    client = None
    ownership = None
    lease_acquired = False
    cancel_stop = threading.Event()
    cancel_thread: threading.Thread | None = None
    pending_terminal: dict[str, Any] | None = None
    worker_error: Exception | None = None
    cleanup_errors: list[str] = []
    source = Path(spec["source_model_path"])
    try:
        if _sha256_file(source) != spec["source_model_sha256"]:
            raise RuntimeError("Immutable spectral source hash changed before client startup")
        completed_spectral_point_fingerprints(
            directory / "spectral_rows.jsonl", spec, artifact_root=directory
        )
        ownership = ownership_factory(store.root.parent, f"job:{job_id}")
        preflight = ownership.preflight(
            model_path=str(source),
            output_path=str(directory / "spectral_rows.jsonl"),
            requested_version=spec.get("version"),
        )
        if not preflight.get("ready"):
            raise RuntimeError(f"Worker preflight failed: {preflight.get('blockers')}")
        claim = ownership.acquire(mode="durable-job", model_path=str(source))
        if not claim.get("success"):
            raise RuntimeError(claim.get("error", "solver ownership claim failed"))
        lease_acquired = True
        client = client_factory(spec)
        ownership.heartbeat(model_path=str(source), refresh_server_processes=True)
        model = client.load(str(source))
        model_name = str(model.name())

        from comsol_mcp.jobs.resource_admission import ResourceStageAdapter, collect_resource_telemetry
        from comsol_mcp.jobs.spectral_runner import run_spectral_characterization
        from comsol_mcp.jobs.validation_collectors import execute_validation_collector
        from comsol_mcp.jobs.worker import _record_native_cancel
        from comsol_mcp.tools.mesh import get_mesh_info

        rows_path = directory / "spectral_rows.jsonl"

        def completed_ids() -> set[str]:
            return completed_spectral_point_fingerprints(
                rows_path, spec, artifact_root=directory
            )

        def sample(stage: str, point_id: str) -> dict[str, Any]:
            if telemetry_provider is not None:
                return telemetry_provider(
                    stage, point_id, model, directory, time.monotonic() - worker_started
                )
            mesh_elements = None
            try:
                mesh = get_mesh_info(model)
                if mesh.get("success"):
                    mesh_elements = mesh.get("mesh", {}).get("num_elements")
            except Exception:
                pass
            return collect_resource_telemetry(
                stage=stage,
                runtime_path=store.root,
                process_id=os.getpid(),
                mesh_elements=mesh_elements,
                elapsed_wall_seconds=time.monotonic() - worker_started,
                durable_result_epoch=(
                    rows_path.stat().st_mtime if rows_path.is_file() else None
                ),
            )

        resource = ResourceStageAdapter(
            store=store,
            job_id=job_id,
            attempt=attempt,
            policy=spec["resource_policy"],
            telemetry_provider=sample,
            completed_point_ids_provider=completed_ids,
        )
        latest_resource_decision: dict[str, Any] | None = None

        def should_stop() -> bool:
            return cancel_request_targets_attempt(store.read_control(job_id), attempt)

        def resource_control(context: Mapping[str, Any]) -> dict[str, Any]:
            nonlocal latest_resource_decision
            if should_stop():
                return {"action": "cancel"}
            point = context.get("point")
            if not isinstance(point, Mapping):
                raise ValueError("resource control point is unavailable")
            decision = resource.evaluate(
                stage="pre_solve", point_id=str(point["point_id"])
            )
            latest_resource_decision = decision
            return {
                "action": (
                    "continue"
                    if decision["action"] == "start_point"
                    else "stop"
                )
            }

        def resource_after(row: Mapping[str, Any]) -> dict[str, Any]:
            nonlocal latest_resource_decision
            decision = resource.evaluate(
                stage="post_solve", point_id=str(row["point_id"])
            )
            latest_resource_decision = decision
            return {
                "action": (
                    "continue"
                    if decision["action"] in {"start_point", "skip_completed"}
                    else "stop"
                )
            }

        def native_monitor() -> None:
            while not cancel_stop.wait(0.05):
                if not should_stop():
                    continue
                from comsol_mcp.jobs.native_cancel_probe import request_native_cancel_once

                _record_native_cancel(
                    store, job_id, attempt, request_native_cancel_once()
                )
                return

        if native_cancel_enabled:
            cancel_thread = threading.Thread(
                target=native_monitor,
                name="comsol-native-cancel",
                daemon=True,
            )
            cancel_thread.start()

        def execute(point: dict[str, Any], artifact_dir: Path) -> Mapping[str, Any]:
            collector = spec["collector"]
            if collector_executor is not None:
                return collector_executor(point, collector, artifact_dir)
            return execute_validation_collector(
                point,
                collector,
                artifact_dir,
                model=model,
                client=client,
                model_name=model_name,
                job_id=job_id,
                expected_source_sha256=spec["source_model_sha256"],
                session_state={"connected": True},
                ownership_preflight=preflight,
            )

        def on_row(row: Mapping[str, Any]) -> None:
            completed = len(completed_ids())
            current = store.read_state(job_id)["status"]
            if current == "smoke_running":
                store.update_state(
                    job_id, "smoke_validated", event="first_point_validated"
                )
                store.update_state(
                    job_id, "running", event="adaptive_spectral_phase_started"
                )
            store.update_state(
                job_id,
                patch={
                    "progress": {
                        "completed": completed,
                        "total": spec["maximum_points"],
                    },
                    "last_row": {
                        "point_id": row["point_id"],
                        "row_sha256": row["row_sha256"],
                        "requested_wavelength_m": row["requested_wavelength_m"],
                    },
                },
                event="durable_row",
                event_data={
                    "point_id": row["point_id"],
                    "row_sha256": row["row_sha256"],
                },
            )
            ownership.heartbeat(
                model_path=str(source), refresh_server_processes=True
            )

        if should_stop():
            store.record_cooperative_cancel_observed(
                job_id, attempt=attempt, message="Stopped before spectrum"
            )
            return 0
        store.update_state(job_id, "smoke_running", event="spectral_job_started")
        result = run_spectral_characterization(
            spec,
            directory,
            attempt=attempt,
            point_executor=execute,
            control_hook=resource_control,
            after_durable_row_hook=resource_after,
            on_durable_row=on_row,
            fault_hook=fault_hook,
        )
        if should_stop() or result.get("stop_reason") in {
            "before_solve_cancel",
            "after_durable_row_cancel",
        }:
            store.record_cooperative_cancel_observed(
                job_id,
                attempt=attempt,
                message="Stopped between spectral point operations",
            )
        elif not result.get("completed"):
            pending_terminal = {
                "status": "interrupted",
                "event": "resource_gate_stopped",
                "patch": {
                    "resource_gate": latest_resource_decision,
                    "last_error": {
                        "type": "ResourceAdmissionStop",
                        "message": str(result.get("stop_reason")),
                    },
                },
            }
        else:
            pending_terminal = {
                "status": "completed",
                "event": "completed",
                "patch": {
                    "progress": {
                        "completed": result["progress"]["row_count"],
                        "total": result["progress"]["row_count"],
                    },
                    "source_unchanged": True,
                    "spectral_summary": {
                        "scientific_disposition": result["progress"]["scientific_disposition"],
                        "reason_code": result["progress"]["reason_code"],
                        "declared_cap_reached": result["progress"]["declared_cap_reached"],
                        "summary_sha256": result["summary"]["summary_sha256"],
                        "summary_artifact": result["summary_artifact"],
                    },
                },
            }
    except Exception as exc:
        worker_error = exc
    finally:
        cancel_stop.set()
        if cancel_thread is not None:
            cancel_thread.join(timeout=1.0)
        if client is not None:
            try:
                client.clear()
            except Exception as exc:
                cleanup_errors.append(f"client_clear:{type(exc).__name__}:{exc}")
            if getattr(client, "port", None):
                try:
                    client.disconnect()
                except Exception as exc:
                    cleanup_errors.append(
                        f"client_disconnect:{type(exc).__name__}:{exc}"
                    )
        if fault_hook is not None:
            try:
                fault_hook("during_cleanup", {"job_id": job_id, "attempt": attempt})
            except Exception as exc:
                cleanup_errors.append(f"cleanup_hook:{type(exc).__name__}:{exc}")
        if ownership is not None and lease_acquired:
            try:
                release = ownership.release()
                if not release.get("success"):
                    cleanup_errors.append(f"lease_release:{json.dumps(release, ensure_ascii=False)}")
            except Exception as exc:
                cleanup_errors.append(f"lease_release:{type(exc).__name__}:{exc}")

    if worker_error is None and _sha256_file(source) != spec["source_model_sha256"]:
        worker_error = RuntimeError("Immutable spectral source hash changed after execution")
    if cleanup_errors and worker_error is None:
        worker_error = RuntimeError("; ".join(cleanup_errors)[:2000])
    current = store.read_state(job_id)["status"]
    if worker_error is not None:
        if current == "cancel_requested":
            store.record_cooperative_cancel_observed(
                job_id, attempt=attempt, message="Stopped between blocking operations"
            )
        elif current != "cancelling" and current not in {
            "completed",
            "interrupted",
        }:
            store.update_state(
                job_id,
                "failed",
                patch={
                    "last_error": {
                        "type": type(worker_error).__name__,
                        "message": str(worker_error)[:2000],
                    },
                    "cleanup_errors": cleanup_errors,
                },
                event="worker_failed",
            )
        print(
            f"{type(worker_error).__name__}: {worker_error}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    if pending_terminal is not None:
        current = store.read_state(job_id)["status"]
        if current not in {"cancel_requested", "cancelling"}:
            store.update_state(
                job_id,
                pending_terminal["status"],
                patch={
                    **pending_terminal["patch"],
                    "cleanup": {
                        "client_cleared": client is not None,
                        "lease_released": lease_acquired,
                        "errors": [],
                    },
                },
                event=pending_terminal["event"],
            )
    return 0


def run(root: str, job_id: str) -> int:
    return _run(root, job_id)


if __name__ == "__main__":
    code = run(sys.argv[1], sys.argv[2])
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
