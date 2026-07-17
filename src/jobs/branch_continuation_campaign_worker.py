"""Detached worker for one durable exact-model branch-continuation campaign."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable, Mapping

from .branch_continuation_campaign import (
    validate_branch_continuation_campaign_driver_identity,
)
from .process_control import contain_current_process_tree
from .store import JobStore, cancel_request_targets_attempt, process_identity


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _default_ownership_factory(runtime_root: Path, owner: str):
    from src.tools.ownership import SolverOwnership

    return SolverOwnership(runtime_root, owner=owner)


def _default_client_factory(spec: Mapping[str, Any]):
    import mph

    first = spec["states"][0]["spectral_job"]
    kwargs = {"cores": first.get("cores"), "version": first.get("version")}
    return mph.Client(**{key: value for key, value in kwargs.items() if value is not None})


def _run(
    root: str,
    job_id: str,
    *,
    ownership_factory: Callable[[Path, str], Any] = _default_ownership_factory,
    client_factory: Callable[[Mapping[str, Any]], Any] = _default_client_factory,
    collector_executor: Callable[[dict[str, Any], dict[str, Any], Path], Mapping[str, Any]] | None = None,
    telemetry_provider: Callable[[str, str, Any, Path, float], dict[str, Any]] | None = None,
    native_cancel_enabled: bool = True,
    fault_hook: Callable[[str, Mapping[str, Any]], Any] | None = None,
) -> int:
    """Run one campaign attempt while preserving exact completed child states."""
    worker_started = time.monotonic()
    store = JobStore(Path(root))
    directory = store.job_dir(job_id)
    spec = store.read_spec(job_id)
    if spec.get("job_type") != "branch_continuation_campaign":
        raise ValueError(
            "Branch-continuation worker accepts only branch_continuation_campaign jobs"
        )
    validate_branch_continuation_campaign_driver_identity(spec)
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
            job_id, attempt=attempt, message="Stopped before campaign startup"
        )
        return 0
    if state["status"] == "submitted":
        store.update_state(job_id, "starting", event="worker_started")
    elif state["status"] != "starting":
        raise ValueError(f"Branch-continuation worker cannot start from {state['status']}")

    sources = [Path(item["spectral_job"]["source_model_path"]) for item in spec["states"]]
    client = None
    ownership = None
    lease_acquired = False
    cancel_stop = threading.Event()
    cancel_thread: threading.Thread | None = None
    pending_terminal: dict[str, Any] | None = None
    worker_error: Exception | None = None
    cleanup_errors: list[str] = []
    latest_resource_decision: dict[str, Any] | None = None
    try:
        for source, campaign_state in zip(sources, spec["states"]):
            if _sha256_file(source) != campaign_state["spectral_job"]["source_model_sha256"]:
                raise RuntimeError("Immutable continuation source hash changed before client startup")
        ownership = ownership_factory(store.root.parent, f"job:{job_id}")
        first = spec["states"][0]["spectral_job"]
        preflight = ownership.preflight(
            model_path=str(sources[0]),
            output_path=str(directory / "continuation_states.jsonl"),
            requested_version=first.get("version"),
        )
        if not preflight.get("ready"):
            raise RuntimeError(f"Worker preflight failed: {preflight.get('blockers')}")
        claim = ownership.acquire(mode="durable-job", model_path=str(sources[0]))
        if not claim.get("success"):
            raise RuntimeError(claim.get("error", "solver ownership claim failed"))
        lease_acquired = True
        client = client_factory(spec)

        from src.jobs.branch_continuation_campaign_runner import (
            run_branch_continuation_campaign,
        )
        from src.jobs.spectral_level_execution import execute_loaded_spectral_level
        from src.jobs.worker import _record_native_cancel

        def should_stop() -> bool:
            return cancel_request_targets_attempt(store.read_control(job_id), attempt)

        def native_monitor() -> None:
            while not cancel_stop.wait(0.05):
                if not should_stop():
                    continue
                from src.jobs.native_cancel_probe import request_native_cancel_once

                _record_native_cancel(store, job_id, attempt, request_native_cancel_once())
                return

        if native_cancel_enabled:
            cancel_thread = threading.Thread(
                target=native_monitor,
                name="comsol-native-cancel",
                daemon=True,
            )
            cancel_thread.start()

        completed_points = int(store.read_state(job_id).get("progress", {}).get("completed", 0))

        def point_persisted(
            campaign_state: Mapping[str, Any], row: Mapping[str, Any]
        ) -> None:
            nonlocal completed_points
            completed_points += 1
            current = store.read_state(job_id)["status"]
            if current == "smoke_running":
                store.update_state(job_id, "smoke_validated", event="first_point_validated")
                store.update_state(job_id, "running", event="continuation_campaign_started")
            store.update_state(
                job_id,
                patch={
                    "progress": {"completed": completed_points, "total": spec["maximum_total_points"]},
                    "current_state": {
                        "state_id": campaign_state["state_id"],
                        "ordinal": campaign_state["ordinal"],
                    },
                    "last_point": {"point_id": row["point_id"], "row_sha256": row["row_sha256"]},
                },
                event="durable_spectral_point",
                event_data={
                    "state_id": campaign_state["state_id"],
                    "point_id": row["point_id"],
                },
            )

        def execute_state(
            campaign_state: Mapping[str, Any], state_dir: Path
        ) -> Mapping[str, Any]:
            nonlocal latest_resource_decision
            child = campaign_state["spectral_job"]
            source = Path(child["source_model_path"])
            if _sha256_file(source) != child["source_model_sha256"]:
                raise RuntimeError("Immutable continuation state source hash changed")
            client.clear()
            model = client.load(str(source))
            model_name = str(model.name())
            ownership.heartbeat(model_path=str(source), refresh_server_processes=True)
            execution = execute_loaded_spectral_level(
                store=store,
                job_id=job_id,
                spec=child,
                directory=state_dir,
                attempt=attempt,
                model=model,
                client=client,
                model_name=model_name,
                ownership=ownership,
                preflight=preflight,
                worker_started=worker_started,
                should_stop=should_stop,
                on_durable_row=lambda row: point_persisted(campaign_state, row),
                collector_executor=collector_executor,
                telemetry_provider=telemetry_provider,
                fault_hook=fault_hook,
            )
            latest_resource_decision = execution["latest_resource_decision"]
            if _sha256_file(source) != child["source_model_sha256"]:
                raise RuntimeError("Immutable continuation state source changed after spectrum")
            return execution["result"]

        def state_persisted(row: Mapping[str, Any]) -> None:
            store.update_state(
                job_id,
                patch={
                    "completed_states": row["ordinal"] + 1,
                    "last_state": {
                        "state_id": row["state_id"],
                        "row_sha256": row["row_sha256"],
                    },
                },
                event="durable_continuation_state",
                event_data={"state_id": row["state_id"], "row_sha256": row["row_sha256"]},
            )

        if should_stop():
            store.record_cooperative_cancel_observed(
                job_id, attempt=attempt, message="Stopped before campaign"
            )
            return 0
        store.update_state(job_id, "smoke_running", event="continuation_campaign_worker_started")
        result = run_branch_continuation_campaign(
            spec,
            directory,
            attempt=attempt,
            state_executor=execute_state,
            control_hook=lambda _context: {"action": "cancel" if should_stop() else "continue"},
            on_durable_state=state_persisted,
            fault_hook=fault_hook,
        )
        if should_stop() or result.get("stop_reason") in {
            "before_state_cancel", "before_solve_cancel", "after_durable_row_cancel"
        }:
            store.record_cooperative_cancel_observed(
                job_id, attempt=attempt, message="Stopped between continuation operations"
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
                    "progress": {"completed": completed_points, "total": completed_points},
                    "completed_states": result["summary"]["completed_state_count"],
                    "source_unchanged": True,
                    "branch_continuation_summary": {
                        "scientific_disposition": result["summary"]["scientific_disposition"],
                        "reason_code": result["summary"]["reason_code"],
                        "declared_cap_reached": result["summary"]["declared_cap_reached"],
                        "branch_disappearance_claimed": False,
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
                    cleanup_errors.append(f"client_disconnect:{type(exc).__name__}:{exc}")
        if fault_hook is not None:
            try:
                fault_hook("during_cleanup", {"job_id": job_id, "attempt": attempt})
            except Exception as exc:
                cleanup_errors.append(f"cleanup_hook:{type(exc).__name__}:{exc}")
        if ownership is not None and lease_acquired:
            try:
                release = ownership.release()
                if not release.get("success"):
                    cleanup_errors.append(
                        f"lease_release:{json.dumps(release, ensure_ascii=False)}"
                    )
            except Exception as exc:
                cleanup_errors.append(f"lease_release:{type(exc).__name__}:{exc}")

    for source, campaign_state in zip(sources, spec["states"]):
        if (
            _sha256_file(source) != campaign_state["spectral_job"]["source_model_sha256"]
            and worker_error is None
        ):
            worker_error = RuntimeError("Immutable continuation source changed after execution")
    if cleanup_errors and worker_error is None:
        worker_error = RuntimeError("; ".join(cleanup_errors)[:2000])
    current = store.read_state(job_id)["status"]
    if worker_error is not None:
        if current == "cancel_requested":
            store.record_cooperative_cancel_observed(
                job_id, attempt=attempt, message="Stopped between blocking operations"
            )
        elif current != "cancelling" and current not in {"completed", "interrupted"}:
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
