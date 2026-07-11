"""Detached production worker for one durable staged COMSOL sweep job."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from .store import JobStore, cancel_request_targets_attempt, process_identity


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
        "manifest_path": str(directory / "results.csv.manifest.json"),
        "source_model_path": spec["source_model_path"],
        "config_id": spec["spec_fingerprint"],
        "record_wavelength_controls": spec.get("record_wavelength_controls"),
        "physical_bounds": spec.get("physical_bounds"),
        "response_tail": 2,
    }


def _run(root: str, job_id: str) -> int:
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

    state = store.read_state(job_id)
    attempt = int(state.get("attempt", 1))
    if state["status"] == "cancel_requested" or cancel_request_targets_attempt(store.read_control(job_id), attempt):
        store.update_state(
            job_id,
            "interrupted",
            patch={"last_error": {"type": "CooperativeCancel", "message": "Stopped before startup"}},
            event="cooperative_cancel_observed",
        )
        return 0
    if state["status"] == "submitted":
        store.update_state(job_id, "starting", event="worker_started")
    elif state["status"] != "starting":
        raise ValueError(f"Production worker cannot start from {state['status']}")

    client = None
    ownership = None
    lease_acquired = False
    try:
        from src.tools.ownership import SolverOwnership

        ownership = SolverOwnership(
            store.root.parent,
            owner=f"job:{job_id}",
        )
        preflight = ownership.preflight(
            model_path=spec["source_model_path"],
            output_path=str(directory / "results.csv"),
            requested_version=spec.get("version"),
        )
        if not preflight["ready"]:
            raise RuntimeError(f"Worker preflight failed: {preflight['blockers']}")
        claim = ownership.acquire(mode="durable-job", model_path=spec["source_model_path"])
        if not claim["success"]:
            raise RuntimeError(claim["error"])
        lease_acquired = True

        import mph
        from src.tools.workflow import run_staged_parametric_sweep

        client_kwargs = {"cores": spec.get("cores"), "version": spec.get("version")}
        client = mph.Client(**{key: value for key, value in client_kwargs.items() if value is not None})
        ownership.heartbeat(model_path=spec["source_model_path"], refresh_server_processes=True)
        model = client.load(spec["source_model_path"])

        progress = {
            "completed": _valid_row_count(directory / "results.csv", spec["spec_fingerprint"]),
            "total": len(spec["parameter_values"]),
        }

        def should_stop() -> bool:
            return cancel_request_targets_attempt(store.read_control(job_id), attempt)

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
            ownership.heartbeat(model_path=spec["source_model_path"], refresh_server_processes=True)

        if should_stop():
            store.update_state(
                job_id,
                "interrupted",
                patch={"last_error": {"type": "CooperativeCancel", "message": "Stopped before smoke"}},
                event="cooperative_cancel_observed",
            )
            return 0
        store.update_state(job_id, "smoke_running", event="smoke_started")
        existing = progress["completed"]
        smoke_needed = max(0, int(spec["smoke_points"]) - existing)
        common = _runner_kwargs(spec, directory)
        smoke = run_staged_parametric_sweep(
            model,
            spec["parameter_name"],
            spec["parameter_values"],
            spec["expressions"],
            **common,
            max_new_points=smoke_needed,
            should_stop=should_stop,
            on_durable_row=on_row,
        )
        if should_stop() or smoke.get("stop_reason") == "control_request":
            store.update_state(
                job_id,
                "interrupted",
                patch={"last_error": {"type": "CooperativeCancel", "message": "Stopped between points"}},
                event="cooperative_cancel_observed",
            )
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
            )
            if should_stop() or broad.get("stop_reason") == "control_request":
                store.update_state(
                    job_id,
                    "interrupted",
                    patch={"last_error": {"type": "CooperativeCancel", "message": "Stopped between points"}},
                    event="cooperative_cancel_observed",
                )
                return 0
            if not broad.get("success"):
                raise RuntimeError(f"Broad sweep failed: {broad}")
        completed = _valid_row_count(directory / "results.csv", spec["spec_fingerprint"])
        if completed != len(spec["parameter_values"]):
            raise RuntimeError(f"Expected {len(spec['parameter_values'])} valid rows, found {completed}")
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
            store.update_state(
                job_id,
                "interrupted",
                patch={"last_error": {"type": "CooperativeCancel", "message": "Stopped between blocking operations"}},
                event="cooperative_cancel_observed",
            )
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
    try:
        return _run(root, job_id)
    except ValueError:
        store = JobStore(Path(root))
        if store.read_state(job_id).get("status") == "cancel_requested":
            store.update_state(
                job_id,
                "interrupted",
                patch={"last_error": {"type": "CooperativeCancel", "message": "Stopped between state transitions"}},
                event="cooperative_cancel_observed",
            )
            return 0
        raise


if __name__ == "__main__":
    code = run(sys.argv[1], sys.argv[2])
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
