"""Detached durable worker for one thermo-optomechanical replay."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Callable, Mapping

from comsol_mcp.path_policy import pin_validated_reads, validated_read_pin

from .process_control import contain_current_process_tree
from .store import JobStore, cancel_request_targets_attempt, process_identity
from .thermo_optomechanical_replay import validate_thermo_optomechanical_driver_identity


class _CooperativeCancellation(Exception):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _default_ownership_factory(runtime_root: Path, owner: str) -> Any:
    from comsol_mcp.tools.ownership import SolverOwnership

    return SolverOwnership(runtime_root, owner=owner)


def _default_client_factory(spec: Mapping[str, Any]) -> Any:
    import mph

    kwargs = {"cores": spec.get("cores"), "version": spec.get("version")}
    return mph.Client(**{key: value for key, value in kwargs.items() if value is not None})


def _run(
    root: str,
    job_id: str,
    *,
    ownership_factory: Callable[[Path, str], Any] = _default_ownership_factory,
    client_factory: Callable[[Mapping[str, Any]], Any] = _default_client_factory,
    stage_executor_factory: Callable[
        [Any, Mapping[str, Any], Path],
        Callable[[str, Path, Mapping[str, Any]], Mapping[str, Any]],
    ]
    | None = None,
    native_cancel_enabled: bool = True,
    fault_hook: Callable[[str, Mapping[str, Any]], Any] | None = None,
) -> int:
    """Run one exact attempt and publish terminal state only after cleanup."""
    store = JobStore(Path(root))
    directory = store.job_dir(job_id)
    spec: dict[str, Any] = {}
    source: Path | None = None
    submission_manifest: Path | None = None
    attempt = 1
    client = None
    ownership = None
    lease_acquired = False
    cancel_stop = threading.Event()
    cancel_thread: threading.Thread | None = None
    pending_terminal: dict[str, Any] | None = None
    cancel_message: str | None = None
    worker_error: Exception | None = None
    cleanup_errors: list[str] = []
    native_monitor_errors: list[Exception] = []
    source_pins = ExitStack()
    try:
        spec = store.read_spec(job_id)
        state = store.read_state(job_id)
        attempt = int(state.get("attempt", 1))
        if spec.get("job_type") != "thermo_optomechanical_replay":
            raise ValueError(
                "Thermo-optomechanical worker accepts only thermo_optomechanical_replay jobs"
            )
        validate_thermo_optomechanical_driver_identity(spec)
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
        if state["status"] == "cancel_requested" or cancel_request_targets_attempt(
            store.read_control(job_id), attempt
        ):
            raise _CooperativeCancellation("Stopped before thermo-optomechanical startup")
        if state["status"] == "submitted":
            try:
                store.update_state(job_id, "starting", event="worker_started")
            except ValueError:
                current = store.read_state(job_id)
                if current.get("status") == "cancel_requested" or cancel_request_targets_attempt(
                    store.read_control(job_id), attempt
                ):
                    raise _CooperativeCancellation(
                        "Stopped during thermo-optomechanical startup"
                    ) from None
                raise
        elif state["status"] != "starting":
            raise ValueError(f"Thermo-optomechanical worker cannot start from {state['status']}")

        source = Path(spec["source_model_path"])
        submission_manifest = Path(spec["submission_manifest_path"])
        source_pins.enter_context(
            pin_validated_reads(
                (
                    validated_read_pin(source, source.parent),
                    validated_read_pin(submission_manifest, submission_manifest.parent),
                )
            )
        )
        if _sha256_file(source) != spec["source_model_sha256"]:
            raise RuntimeError(
                "Immutable thermo-optomechanical source changed before client startup"
            )
        if _sha256_file(submission_manifest) != spec["submission_manifest_sha256"]:
            raise RuntimeError(
                "Immutable thermo-optomechanical manifest changed before client startup"
            )
        ownership = ownership_factory(store.root.parent, f"job:{job_id}")
        preflight = ownership.preflight(
            model_path=str(source),
            output_path=str(directory / "thermo_optomechanical_stages.jsonl"),
            requested_version=spec.get("version"),
        )
        if not preflight.get("ready"):
            raise RuntimeError(f"Worker preflight failed: {preflight.get('blockers')}")
        claim = ownership.acquire(mode="durable-job", model_path=str(source))
        if not claim.get("success"):
            raise RuntimeError(claim.get("error", "solver ownership claim failed"))
        lease_acquired = True
        client = client_factory(spec)

        from .thermo_optomechanical_replay_execution import (
            ThermoOptomechanicalComsolExecutor,
        )
        from .thermo_optomechanical_replay_runner import run_thermo_optomechanical_replay
        from .worker import _record_native_cancel

        executor_factory = stage_executor_factory or ThermoOptomechanicalComsolExecutor
        stage_executor = executor_factory(client, spec, directory)

        def should_stop() -> bool:
            return bool(cancel_request_targets_attempt(store.read_control(job_id), attempt))

        def native_monitor() -> None:
            try:
                while not cancel_stop.wait(0.05):
                    if not should_stop():
                        continue
                    from .native_cancel_probe import request_native_cancel_once

                    _record_native_cancel(store, job_id, attempt, request_native_cancel_once())
                    return
            except Exception as exc:
                native_monitor_errors.append(exc)

        if native_cancel_enabled:
            cancel_thread = threading.Thread(
                target=native_monitor, name="comsol-native-cancel", daemon=True
            )
            cancel_thread.start()

        def stage_persisted(row: Mapping[str, Any]) -> None:
            current = store.read_state(job_id)["status"]
            if current == "smoke_running":
                store.update_state(
                    job_id,
                    "smoke_validated",
                    event="first_thermo_optomechanical_stage_validated",
                )
                store.update_state(job_id, "running", event="thermo_optomechanical_replay_started")
            completed = int(row["ordinal"]) + 1
            store.update_state(
                job_id,
                patch={
                    "progress": {"completed": completed, "total": len(spec["declared_stages"])},
                    "last_stage": {
                        "stage_id": row["stage_id"],
                        "row_sha256": row["row_sha256"],
                    },
                },
                event="durable_thermo_optomechanical_stage",
                event_data={"stage_id": row["stage_id"], "row_sha256": row["row_sha256"]},
            )

        if should_stop():
            raise _CooperativeCancellation("Stopped before thermo-optomechanical stages")
        store.update_state(job_id, "smoke_running", event="thermo_optomechanical_worker_started")
        result = run_thermo_optomechanical_replay(
            spec,
            directory,
            attempt=attempt,
            stage_executor=stage_executor,
            control_hook=lambda _context: {"action": "cancel" if should_stop() else "continue"},
            on_durable_stage=stage_persisted,
            fault_hook=fault_hook,
        )
        if should_stop() or result.get("stop_reason") == "before_stage_cancel":
            cancel_message = "Stopped between thermo-optomechanical stages"
        elif not result.get("completed"):
            pending_terminal = {
                "status": "interrupted",
                "event": "thermo_optomechanical_stage_interrupted",
                "patch": {
                    "last_error": {
                        "type": "ThermoOptomechanicalStageStop",
                        "message": str(result.get("stop_reason")),
                    }
                },
            }
        else:
            summary = result["summary"]
            pending_terminal = {
                "status": "completed",
                "event": "completed",
                "patch": {
                    "progress": {
                        "completed": len(spec["declared_stages"]),
                        "total": len(spec["declared_stages"]),
                    },
                    "completed_stages": len(spec["declared_stages"]),
                    "source_unchanged": True,
                    "thermo_optomechanical_summary": {
                        "scientific_disposition": summary["scientific_disposition"],
                        "reason_codes": summary["reason_codes"],
                        "summary_sha256": summary["summary_sha256"],
                        "summary_artifact": result["summary_artifact"],
                    },
                },
            }
    except _CooperativeCancellation as exc:
        cancel_message = str(exc)
    except Exception as exc:
        worker_error = exc
    finally:
        cancel_stop.set()
        native_cancel_inflight = False
        if cancel_thread is not None:
            cancel_thread.join(timeout=1.0)
            native_cancel_inflight = cancel_thread.is_alive()
            if native_cancel_inflight:
                cleanup_errors.append("native_cancel_thread:still_active_after_join_timeout")
        if native_monitor_errors and worker_error is None:
            worker_error = RuntimeError(
                f"native cancel monitor failed: {type(native_monitor_errors[0]).__name__}"
            )
        if client is not None and not native_cancel_inflight:
            try:
                client.clear()
            except Exception as exc:
                cleanup_errors.append(f"client_clear:{type(exc).__name__}:{exc}")
            if getattr(client, "port", None):
                try:
                    client.disconnect()
                except Exception as exc:
                    cleanup_errors.append(f"client_disconnect:{type(exc).__name__}:{exc}")
        if fault_hook is not None:
            try:
                fault_hook("during_cleanup", {"job_id": job_id, "attempt": attempt})
            except Exception as exc:
                cleanup_errors.append(f"cleanup_hook:{type(exc).__name__}:{exc}")
        if ownership is not None and lease_acquired and not native_cancel_inflight:
            try:
                release = ownership.release()
                if not release.get("success"):
                    cleanup_errors.append(
                        f"lease_release:{json.dumps(release, ensure_ascii=False)}"
                    )
            except Exception as exc:
                cleanup_errors.append(f"lease_release:{type(exc).__name__}:{exc}")
        try:
            if source is not None and _sha256_file(source) != spec.get("source_model_sha256"):
                if worker_error is None:
                    worker_error = RuntimeError(
                        "Immutable thermo-optomechanical source changed after execution"
                    )
            if (
                submission_manifest is not None
                and _sha256_file(submission_manifest) != spec.get("submission_manifest_sha256")
                and worker_error is None
            ):
                worker_error = RuntimeError(
                    "Immutable thermo-optomechanical manifest changed after execution"
                )
        except Exception as exc:
            if worker_error is None:
                worker_error = exc
            else:
                cleanup_errors.append(f"final_source_verification:{type(exc).__name__}:{exc}")
        try:
            source_pins.close()
        except Exception as exc:
            cleanup_errors.append(f"source_pin_close:{type(exc).__name__}:{exc}")
    if cleanup_errors and worker_error is None:
        worker_error = RuntimeError("; ".join(cleanup_errors)[:2000])
    current = store.read_state(job_id)["status"]
    if worker_error is not None:
        if current in {"cancel_requested", "cancelling"}:
            store.record_cooperative_cancel_observed(
                job_id,
                attempt=attempt,
                message=(
                    cancel_message or "Stopped between blocking thermo-optomechanical operations"
                ),
                worker_error={
                    "type": type(worker_error).__name__,
                    "message": str(worker_error),
                    "cleanup_errors": cleanup_errors,
                },
            )
        elif current not in {"completed", "interrupted"}:
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
        print(f"{type(worker_error).__name__}: {worker_error}", file=sys.stderr, flush=True)
        return 1
    if cancel_message is not None:
        current = store.read_state(job_id)["status"]
        if current in {"cancel_requested", "cancelling"}:
            store.record_cooperative_cancel_observed(
                job_id, attempt=attempt, message=cancel_message
            )
        return 0
    if pending_terminal is not None:
        current = store.read_state(job_id)["status"]
        if current in {"cancel_requested", "cancelling"}:
            store.record_cooperative_cancel_observed(
                job_id,
                attempt=attempt,
                message="Stopped before thermo-optomechanical terminal publication",
            )
        else:
            if current == "smoke_running" and pending_terminal["status"] == "completed":
                store.update_state(
                    job_id,
                    "smoke_validated",
                    event="thermo_optomechanical_rows_revalidated",
                )
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
