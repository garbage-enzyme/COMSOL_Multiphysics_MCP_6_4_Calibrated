"""Detached production worker for one durable physical-validation matrix."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable

from .process_control import contain_current_process_tree
from .store import JobStore, cancel_request_targets_attempt, process_identity
from .validation_rows import completed_point_fingerprints


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


def _resource_stop(store: JobStore, job_id: str, result: dict[str, Any]) -> bool:
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
                "message": f"Resource admission stopped the validation worker: {reason}",
            },
        },
        event="resource_gate_stopped",
        event_data=gate,
    )
    return True


def _run(
    root: str,
    job_id: str,
    *,
    ownership_factory: Callable[[Path, str], Any] = _default_ownership_factory,
    client_factory: Callable[[dict[str, Any]], Any] = _default_client_factory,
    collector_executor: Callable[[dict[str, Any], dict[str, Any], Path], Any] | None = None,
    telemetry_provider: Callable[[str, str, Any, Path, float], dict[str, Any]] | None = None,
    native_cancel_enabled: bool = True,
) -> int:
    worker_started = time.monotonic()
    store = JobStore(Path(root))
    directory = store.job_dir(job_id)
    spec = store.read_spec(job_id)
    if spec.get("job_type") != "validation_matrix":
        raise ValueError("Validation worker accepts only validation_matrix jobs")
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
        store.record_cooperative_cancel_observed(job_id, attempt=attempt, message="Stopped before startup")
        return 0
    if state["status"] == "submitted":
        store.update_state(job_id, "starting", event="worker_started")
    elif state["status"] != "starting":
        raise ValueError(f"Validation worker cannot start from {state['status']}")

    client = None
    ownership = None
    lease_acquired = False
    cancel_stop = threading.Event()
    cancel_thread: threading.Thread | None = None
    try:
        source = Path(spec["source_model_path"])
        if _sha256_file(source) != spec["source_model_sha256"]:
            raise RuntimeError("Immutable validation source hash changed before client startup")
        completed_point_fingerprints(directory / "matrix_rows.jsonl", spec)
        ownership = ownership_factory(store.root.parent, f"job:{job_id}")
        preflight = ownership.preflight(
            model_path=str(source),
            output_path=str(directory / "matrix_rows.jsonl"),
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
        from comsol_mcp.jobs.validation_collectors import execute_validation_collector
        from comsol_mcp.jobs.validation_runner import run_pending_validation_points
        from comsol_mcp.tools.mesh import get_mesh_info
        from comsol_mcp.jobs.worker import _record_native_cancel

        rows_path = directory / "matrix_rows.jsonl"

        def completed_ids() -> set[str]:
            return completed_point_fingerprints(rows_path, spec)

        def sample(stage: str, point_id: str) -> dict[str, Any]:
            if telemetry_provider is not None:
                return telemetry_provider(stage, point_id, model, directory, time.monotonic() - worker_started)
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
                durable_result_epoch=rows_path.stat().st_mtime if rows_path.is_file() else None,
            )

        resource = ResourceStageAdapter(
            store=store,
            job_id=job_id,
            attempt=attempt,
            policy=spec["resource_policy"],
            telemetry_provider=sample,
            completed_point_ids_provider=completed_ids,
        )

        def resource_hook(context: dict[str, Any]) -> dict[str, Any]:
            if context.get("config_id") != spec["spec_fingerprint"]:
                raise ValueError("resource hook received a mismatched configuration")
            return resource.evaluate(stage=context["stage"], point_id=context["point_id"])

        def should_stop() -> bool:
            return cancel_request_targets_attempt(store.read_control(job_id), attempt)

        def native_monitor() -> None:
            while not cancel_stop.wait(0.05):
                if not should_stop():
                    continue
                from comsol_mcp.jobs.native_cancel_probe import request_native_cancel_once

                _record_native_cancel(store, job_id, attempt, request_native_cancel_once())
                return

        if native_cancel_enabled:
            cancel_thread = threading.Thread(target=native_monitor, name="comsol-native-cancel", daemon=True)
            cancel_thread.start()

        def execute(point: dict[str, Any], collector: dict[str, Any], artifact_dir: Path):
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

        total = len(spec["points"])
        progress = {"completed": len(completed_ids()), "total": total}

        def on_row(row: dict[str, Any]) -> None:
            progress["completed"] = len(completed_ids())
            current = store.read_state(job_id)["status"]
            if row["status"] == "ok" and current == "smoke_running":
                store.update_state(job_id, "smoke_validated", event="first_point_validated")
                if progress["completed"] < total:
                    store.update_state(job_id, "running", event="matrix_phase_started")
            store.update_state(
                job_id,
                patch={"progress": dict(progress), "last_row": row},
                event="durable_row",
                event_data={"status": row["status"], "point_id": row["point_id"]},
            )
            ownership.heartbeat(model_path=str(source), refresh_server_processes=True)

        if should_stop():
            store.record_cooperative_cancel_observed(job_id, attempt=attempt, message="Stopped before matrix")
            return 0
        store.update_state(job_id, "smoke_running", event="matrix_started")
        result = run_pending_validation_points(
            spec,
            directory,
            attempt=attempt,
            collector_executor=execute,
            should_stop=should_stop,
            on_durable_row=on_row,
            before_point_hook=resource_hook,
            after_durable_row_hook=resource_hook,
        )
        if should_stop() or result.get("stop_reason") == "control_request":
            store.record_cooperative_cancel_observed(job_id, attempt=attempt, message="Stopped between matrix operations")
            return 0
        if _resource_stop(store, job_id, result):
            return 0
        if not result.get("success"):
            raise RuntimeError(f"Validation matrix failed: {result}")
        completed = len(completed_ids())
        if completed != total:
            raise RuntimeError(f"Expected {total} complete validation rows, found {completed}")
        if _sha256_file(source) != spec["source_model_sha256"]:
            raise RuntimeError("Immutable validation source hash changed after matrix execution")
        current = store.read_state(job_id)["status"]
        if current == "smoke_running":
            store.update_state(job_id, "smoke_validated", event="first_point_validated")
        store.update_state(
            job_id,
            "completed",
            patch={"progress": {"completed": completed, "total": total}, "source_unchanged": True},
            event="completed",
        )
        return 0
    except Exception as exc:
        current = store.read_state(job_id)["status"]
        if current == "cancel_requested":
            store.record_cooperative_cancel_observed(job_id, attempt=attempt, message="Stopped between blocking operations")
        elif current != "cancelling" and current not in {"completed", "interrupted"}:
            store.update_state(
                job_id,
                "failed",
                patch={"last_error": {"type": type(exc).__name__, "message": str(exc)[:2000]}},
                event="worker_failed",
            )
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        cancel_stop.set()
        if cancel_thread is not None:
            cancel_thread.join(timeout=1.0)
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
        if ownership is not None and lease_acquired:
            release = ownership.release()
            if not release.get("success"):
                print(json.dumps(release, ensure_ascii=False), file=sys.stderr, flush=True)


def run(root: str, job_id: str) -> int:
    return _run(root, job_id)


if __name__ == "__main__":
    code = run(sys.argv[1], sys.argv[2])
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
