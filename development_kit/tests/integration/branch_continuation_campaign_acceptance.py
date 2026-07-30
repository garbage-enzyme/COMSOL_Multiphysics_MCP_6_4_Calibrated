"""Explicit serial licensed acceptance runner for one continuation campaign."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

from src.build_identity import get_build_identity
from src.jobs.branch_continuation_campaign import (
    normalize_branch_continuation_campaign_spec,
)
from src.jobs.branch_continuation_campaign_rows import (
    read_branch_continuation_campaign_states,
)
from src.jobs.branch_continuation_campaign_worker import _run
from src.jobs.store import JobStore, process_identity
from src.tools.ownership import SolverOwnership

from comsol_mcp.durable.io import atomic_write_json_exclusive

MAX_INPUT_BYTES = 8 * 1024 * 1024


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("continuation acceptance input is missing or oversized")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("continuation acceptance input must contain one JSON object")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        atomic_write_json_exclusive(path, value)
    except FileExistsError as exc:
        raise FileExistsError(f"refusing to overwrite acceptance receipt: {path}") from exc


def _ascii_root(path: Path) -> Path:
    resolved = path.resolve()
    try:
        str(resolved).encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("runtime root must be ASCII-only") from exc
    return resolved


def _ownership_status(runtime: Path) -> dict[str, Any]:
    return SolverOwnership(runtime, owner="branch-continuation-acceptance").status(
        require_fresh_inventory=True
    )


def _cleanup_evidence(status: object) -> dict[str, Any]:
    value = status if isinstance(status, dict) else {}
    inventory = value.get("process_inventory") or {}
    external = value.get("external_solver_processes")
    evidence = {
        "process_inventory_complete": inventory.get("complete") is True,
        "process_inventory_fresh": inventory.get("fresh") is True,
        "lease_absent": value.get("lease", {}).get("state") == "absent",
        "collision_absent": value.get("collision") is False,
        "external_solver_processes": external if isinstance(external, list) else None,
    }
    evidence["external_processes_absent"] = external == []
    evidence["passed"] = all(
        evidence[field] is True
        for field in (
            "process_inventory_complete",
            "process_inventory_fresh",
            "lease_absent",
            "collision_absent",
            "external_processes_absent",
        )
    )
    return evidence


def _state_coverage(spec: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    expected = [state["state_id"] for state in spec["states"]]
    observed = [row.get("state_id") for row in rows]
    return {
        "expected_state_ids": expected,
        "observed_state_ids": observed,
        "exact_once": len(observed) == len(expected)
        and len(observed) == len(set(observed))
        and set(observed) == set(expected),
    }


def run_acceptance(
    *,
    raw_spec: dict[str, Any],
    runtime_root: Path,
    output: Path,
    dry_run: bool = False,
    worker_runner: Callable[..., int] = _run,
    ownership_provider: Callable[[Path], dict[str, Any]] = _ownership_status,
) -> dict[str, Any]:
    """Normalize, execute in the shared runtime, and write one bounded receipt."""
    runtime = _ascii_root(runtime_root)
    spec = normalize_branch_continuation_campaign_spec(raw_spec)
    source_before = {
        state["state_id"]: _sha256_file(Path(state["spectral_job"]["source_model_path"]))
        for state in spec["states"]
    }
    declared_readbacks = {
        state["state_id"]: state["incidence_readback"] for state in spec["states"]
    }
    incidence_evidence = {
        "source": "normalized_spec_declaration",
        "observed_execution": False,
        "declared_readbacks": declared_readbacks,
    }
    if dry_run:
        receipt = {
            "success": True,
            "dry_run": True,
            "comsol_client_started": False,
            "spec_fingerprint": spec["spec_fingerprint"],
            "driver_identity": spec["driver_identity"],
            "source_model_sha256": source_before,
            "incidence_evidence": incidence_evidence,
            "build_identity": get_build_identity(),
        }
        _write_json(output, receipt)
        return receipt

    store = JobStore(runtime / "jobs")
    identity = process_identity(os.getpid())
    now = time.time()
    job_id = store.create(
        spec,
        {
            "schema_version": "2",
            "status": "submitted",
            "attempt": 1,
            "created_at_epoch": now,
            "updated_at_epoch": now,
            "worker_pid": identity["pid"],
            "worker_process_create_time": identity["process_create_time"],
            "worker_command_signature": identity["command_signature"],
            "progress": {"completed": 0, "total": spec["maximum_total_points"]},
            "last_error": None,
        },
    )
    started = time.perf_counter()
    directory = store.job_dir(job_id)
    exit_code = None
    state = None
    rows = []
    source_after = None
    phase_error = None
    try:
        exit_code = worker_runner(str(store.root), job_id, native_cancel_enabled=True)
        state = store.read_state(job_id)
        rows = read_branch_continuation_campaign_states(
            directory / "continuation_states.jsonl", spec, artifact_root=directory
        )
        source_after = {
            item["state_id"]: _sha256_file(Path(item["spectral_job"]["source_model_path"]))
            for item in spec["states"]
        }
    except BaseException as exc:
        phase_error = {"type": type(exc).__name__, "message": str(exc)}
        try:
            state = store.read_state(job_id)
        except Exception:
            state = None
    try:
        cleanup = _cleanup_evidence(ownership_provider(runtime))
    except BaseException as exc:
        cleanup = _cleanup_evidence(None)
        cleanup["error_type"] = type(exc).__name__
    coverage = _state_coverage(spec, rows)
    source_unchanged = source_after == source_before
    success = (
        phase_error is None
        and exit_code == 0
        and isinstance(state, dict)
        and state.get("status") == "completed"
        and source_unchanged
        and coverage["exact_once"] is True
        and cleanup["passed"] is True
    )
    receipt = {
        "success": success,
        "dry_run": False,
        "job_id": job_id,
        "worker_exit_code": exit_code,
        "outer_seconds": time.perf_counter() - started,
        "spec_fingerprint": spec["spec_fingerprint"],
        "driver_identity": spec["driver_identity"],
        "source_model_sha256_before": source_before,
        "source_model_sha256_after": source_after,
        "source_unchanged": source_unchanged,
        "incidence_evidence": incidence_evidence,
        "phase_error": phase_error,
        "state": state,
        "states": rows,
        "state_coverage": coverage,
        "cleanup": {
            **cleanup,
            "worker_state_cleanup": state.get("cleanup") if isinstance(state, dict) else None,
        },
    }
    _write_json(output, receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm")
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.dry_run and args.confirm != "RUN_REAL_COMSOL":
        raise ValueError("licensed execution requires --confirm RUN_REAL_COMSOL")
    receipt = run_acceptance(
        raw_spec=_load_json(args.spec),
        runtime_root=args.runtime_root,
        output=args.output,
        dry_run=args.dry_run,
    )
    print(
        json.dumps(
            {
                "success": receipt["success"],
                "dry_run": receipt["dry_run"],
                "job_id": receipt.get("job_id"),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0 if receipt["success"] else 1


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
