"""Detached production worker for one durable physical-validation matrix."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .process_control import contain_current_process_tree
from .store import JobStore, cancel_request_targets_attempt, process_identity
from .validation_rows import completed_point_fingerprints


class NativeCleanupUnverified(RuntimeError):
    """The native worker cannot prove retirement of its owned resources."""


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
    java_environment_reader: Callable[[str], str | None] | None = None,
) -> int:
    worker_started = time.monotonic()
    store = JobStore(Path(root))
    directory = store.job_dir(job_id)
    spec = store.read_spec(job_id)
    if spec.get("job_type") != "validation_matrix":
        raise ValueError("Validation worker accepts only validation_matrix jobs")
    identity = process_identity(os.getpid())
    store.bind_worker_identity(job_id, identity)
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
        raise ValueError(f"Validation worker cannot start from {state['status']}")

    client = None
    ownership = None
    lease_acquired = False
    cancel_stop = threading.Event()
    cancel_thread: threading.Thread | None = None
    native_monitor_errors: list[Exception] = []
    frozen_source: tempfile.TemporaryDirectory[str] | None = None
    previous_temporary_directory = os.environ.get("COMSOL_TMPDIR")
    requested_temporary_directory = spec.get("comsol_temporary_directory")
    native_execution = any(
        collector["name"] in {"native_condition_gradient", "native_condition_forward"}
        for point in spec["points"]
        for collector in point["collectors"]
    )
    native_completion_count: int | None = None
    try:
        source = Path(spec["source_model_path"])
        if _sha256_file(source) != spec["source_model_sha256"]:
            raise RuntimeError("Immutable validation source hash changed before client startup")
        completed_point_fingerprints(directory / "matrix_rows.jsonl", spec, artifact_root=directory)
        if native_execution:
            from comsol_mcp.contracts.startup_admission import evaluate_robust_startup_admission
            from comsol_mcp.durable import atomic_write_json

            from .native_wall_watchdog import await_native_wall_watchdog
            from .resource_admission import collect_resource_telemetry

            await_native_wall_watchdog(store, job_id, spec, attempt)
            admission = evaluate_robust_startup_admission(
                spec["startup_admission"],
                collect_resource_telemetry(stage="pre_mesh", runtime_path=directory),
            )
            atomic_write_json(directory / "startup-admission.json", admission)
            if admission["ready"] is not True:
                raise RuntimeError(
                    "Caller-declared native startup RAM/disk admission refused the job"
                )
            frozen_source = tempfile.TemporaryDirectory(
                prefix="native-source-", dir=directory, delete=False
            )
            snapshot = Path(frozen_source.name) / "source.mph"
            digest = hashlib.sha256()
            with source.open("rb") as original, snapshot.open("xb") as copied:
                for block in iter(lambda: original.read(1024 * 1024), b""):
                    digest.update(block)
                    copied.write(block)
            if digest.hexdigest() != spec["source_model_sha256"]:
                raise RuntimeError("Immutable native source changed before snapshot capture")
            source = snapshot
            if cancel_request_targets_attempt(store.read_control(job_id), attempt):
                store.record_cooperative_cancel_observed(
                    job_id, attempt=attempt, message="Stopped before native client startup"
                )
                return 0
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
        if requested_temporary_directory is not None:
            os.environ["COMSOL_TMPDIR"] = requested_temporary_directory
        client = client_factory(spec)
        if requested_temporary_directory is not None:
            if java_environment_reader is None:
                import jpype

                def read_java_environment(name: str) -> str:
                    return str(jpype.JClass("java.lang.System").getenv(name))

                java_environment_reader = read_java_environment
            observed = java_environment_reader("COMSOL_TMPDIR")
            from comsol_mcp.durable import atomic_write_json

            atomic_write_json(
                directory / "comsol-temporary-directory.json",
                {
                    "control": "COMSOL_TMPDIR",
                    "requested_path": requested_temporary_directory,
                    "process_environment_path": os.environ.get("COMSOL_TMPDIR"),
                    "java_environment_path": observed,
                    "matches": observed == requested_temporary_directory,
                },
            )
            if observed != requested_temporary_directory:
                raise RuntimeError("COMSOL temporary directory readback mismatch")
        ownership.heartbeat(model_path=str(source), refresh_server_processes=True)
        model = client.load(str(source))
        model_name = str(model.name())

        from comsol_mcp.jobs.resource_admission import (
            ResourceStageAdapter,
            collect_resource_telemetry,
        )
        from comsol_mcp.jobs.validation_collectors import execute_validation_collector
        from comsol_mcp.jobs.validation_runner import run_pending_validation_points
        from comsol_mcp.jobs.worker import _record_native_cancel
        from comsol_mcp.tools.mesh import get_mesh_info

        rows_path = directory / "matrix_rows.jsonl"

        def completed_ids() -> set[str]:
            return completed_point_fingerprints(rows_path, spec, artifact_root=directory)

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
            except Exception:  # noqa: S110 - optional mesh telemetry must not fail admission
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
            try:
                while not cancel_stop.wait(0.05):
                    if not should_stop():
                        continue
                    from comsol_mcp.jobs.native_cancel_probe import request_native_cancel_once

                    _record_native_cancel(store, job_id, attempt, request_native_cancel_once())
                    return
            except Exception as exc:
                native_monitor_errors.append(exc)
                try:
                    store.record_cooperative_cancel_observed(
                        job_id,
                        attempt=attempt,
                        message="Native cancellation monitor failed",
                        worker_error={
                            "type": type(exc).__name__,
                            "message": str(exc) or "native cancellation monitor failed",
                            "cleanup_errors": [],
                        },
                    )
                except Exception as record_exc:
                    native_monitor_errors.append(record_exc)

        if native_cancel_enabled:
            cancel_thread = threading.Thread(
                target=native_monitor, name="comsol-native-cancel", daemon=True
            )
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
                cancel_requested=should_stop,
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
            store.record_cooperative_cancel_observed(
                job_id, attempt=attempt, message="Stopped before matrix"
            )
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
            store.record_cooperative_cancel_observed(
                job_id, attempt=attempt, message="Stopped between matrix operations"
            )
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
        if (
            native_execution
            and _sha256_file(Path(spec["source_model_path"])) != spec["source_model_sha256"]
        ):
            raise RuntimeError("Immutable native original source changed during matrix execution")
        current = store.read_state(job_id)["status"]
        if current == "smoke_running":
            store.update_state(job_id, "smoke_validated", event="first_point_validated")
        if native_execution:
            native_completion_count = completed
        else:
            store.update_state(
                job_id,
                "completed",
                patch={
                    "progress": {"completed": completed, "total": total},
                    "source_unchanged": True,
                },
                event="completed",
            )
        return 0
    except Exception as exc:
        current = store.read_state(job_id)["status"]
        if current in {"cancel_requested", "cancelling"}:
            store.record_cooperative_cancel_observed(
                job_id,
                attempt=attempt,
                message="Stopped between blocking operations",
                worker_error={
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "cleanup_errors": [],
                },
            )
        elif current not in {"completed", "interrupted"}:
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
        cleanup_errors: list[str] = []
        native_cancel_active = False
        if cancel_thread is not None:
            cancel_thread.join(timeout=1.0)
            native_cancel_active = cancel_thread.is_alive()
            if native_cancel_active:
                cleanup_errors.append("native_cancel_monitor_active")
                print(
                    "Native cancel thread is still active; preserving client and lease",
                    file=sys.stderr,
                    flush=True,
                )
        if native_monitor_errors:
            print(
                f"Native cancel monitor warning: {type(native_monitor_errors[0]).__name__}",
                file=sys.stderr,
                flush=True,
            )
        if client is not None and not native_cancel_active:
            try:
                client.clear()
            except Exception as exc:
                cleanup_errors.append(f"client_clear:{type(exc).__name__}")
                print(f"Client clear warning: {exc}", file=sys.stderr, flush=True)
            if getattr(client, "port", None):
                try:
                    client.disconnect()
                except Exception as exc:
                    cleanup_errors.append(f"client_disconnect:{type(exc).__name__}")
                    print(f"Client disconnect warning: {exc}", file=sys.stderr, flush=True)
        if (
            ownership is not None
            and lease_acquired
            and not native_cancel_active
            and not (native_execution and cleanup_errors)
        ):
            try:
                release = ownership.release()
                if not release.get("success"):
                    cleanup_errors.append("lease_release_refused")
                    print(json.dumps(release, ensure_ascii=False), file=sys.stderr, flush=True)
            except Exception as exc:
                if not native_execution:
                    raise
                cleanup_errors.append(f"lease_release:{type(exc).__name__}")
        if not native_cancel_active and not (native_execution and cleanup_errors):
            if frozen_source is not None:
                try:
                    Path(frozen_source.name).resolve().relative_to(directory.resolve())
                    frozen_source.cleanup()
                except Exception as exc:
                    if not native_execution:
                        raise
                    cleanup_errors.append(f"source_snapshot_cleanup:{type(exc).__name__}")
        if not native_cancel_active:
            if requested_temporary_directory is not None:
                if previous_temporary_directory is None:
                    os.environ.pop("COMSOL_TMPDIR", None)
                else:
                    os.environ["COMSOL_TMPDIR"] = previous_temporary_directory
        if native_execution:
            from comsol_mcp.durable import atomic_write_json

            cleanup = {"success": not cleanup_errors, "errors": cleanup_errors}
            atomic_write_json(directory / "native-worker-cleanup.json", cleanup)
            patch: dict[str, Any] = {
                "owned_cleanup": cleanup,
                "cleanup_pending": bool(cleanup_errors),
            }
            current = store.read_state(job_id)["status"]
            if native_completion_count is not None and current not in {
                "cancel_requested",
                "cancelling",
            }:
                patch["progress"] = {
                    "completed": native_completion_count,
                    "total": native_completion_count,
                }
                if cleanup_errors:
                    patch["last_error"] = {
                        "type": "NativeCleanupUnverified",
                        "message": "Native owned resources could not be proved cleaned.",
                    }
                else:
                    patch["source_unchanged"] = True
                store.update_state(
                    job_id,
                    "failed" if cleanup_errors else "completed",
                    patch=patch,
                    event="owned_cleanup_unverified" if cleanup_errors else "completed",
                )
            else:
                store.update_state(job_id, patch=patch, event="owned_cleanup_recorded")
            if cleanup_errors:
                raise NativeCleanupUnverified("Native owned cleanup remains unverified")


def run(root: str, job_id: str) -> int:
    try:
        return _run(root, job_id)
    except NativeCleanupUnverified:
        return 1


if __name__ == "__main__":
    code = run(sys.argv[1], sys.argv[2])
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
