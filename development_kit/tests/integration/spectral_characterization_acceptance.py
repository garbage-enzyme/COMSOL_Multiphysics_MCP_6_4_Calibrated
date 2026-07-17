"""Explicit serial licensed acceptance runner for one durable spectral job."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable

from src.build_identity import get_build_identity
from src.jobs.spectral_characterization import normalize_spectral_characterization_job_spec
from src.jobs.spectral_rows import read_spectral_rows
from src.jobs.spectral_stages import read_spectral_stage_plans
from src.jobs.spectral_worker import _run
from src.jobs.store import JobStore, process_identity


MAX_INPUT_BYTES = 1024 * 1024


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("spectral acceptance input is missing or oversized")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("spectral acceptance input must contain one JSON object")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite acceptance receipt: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _require_ascii_root(path: Path) -> Path:
    resolved = path.resolve()
    try:
        str(resolved).encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("runtime root must be ASCII-only") from exc
    return resolved


def _row_receipt(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "point_id": row["point_id"],
        "row_sha256": row["row_sha256"],
        "requested_wavelength_m": row["requested_wavelength_m"],
        "evaluated_wavelength_m": row["evaluated_wavelength_m"],
        "frequency_wavelength_m": row["frequency_wavelength_m"],
        "R": row["R"],
        "T": row["T"],
        "A": row["A"],
        "closure_abs": abs(1.0 - row["R"] - row["T"] - row["A"]),
        "wavelength_sync_abs_m": max(
            abs(row["requested_wavelength_m"] - row["evaluated_wavelength_m"]),
            abs(row["requested_wavelength_m"] - row["frequency_wavelength_m"]),
        ),
        "mesh_element_count": row["mesh_element_count"],
        "mesh_vertex_count": row["mesh_vertex_count"],
        "solve_seconds": row["solve_seconds"],
        "audit_artifact": row["audit_artifact"],
    }


def run_acceptance(
    *,
    raw_spec: dict[str, Any],
    runtime_root: Path,
    output: Path,
    dry_run: bool = False,
    worker_runner: Callable[..., int] = _run,
) -> dict[str, Any]:
    """Normalize, execute in the shared runtime, and write one bounded receipt."""
    runtime = _require_ascii_root(runtime_root)
    spec = normalize_spectral_characterization_job_spec(raw_spec)
    source = Path(spec["source_model_path"])
    source_before = _sha256_file(source)
    started = time.perf_counter()
    if dry_run:
        receipt = {
            "success": True,
            "dry_run": True,
            "comsol_client_started": False,
            "spec_fingerprint": spec["spec_fingerprint"],
            "driver_identity": spec["driver_identity"],
            "source_model_sha256": source_before,
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
            "progress": {"completed": 0, "total": spec["maximum_points"]},
            "last_error": None,
        },
    )
    exit_code = worker_runner(str(store.root), job_id, native_cancel_enabled=True)
    directory = store.job_dir(job_id)
    state = store.read_state(job_id)
    rows = read_spectral_rows(
        directory / "spectral_rows.jsonl", spec, artifact_root=directory
    )
    stages = read_spectral_stage_plans(directory, spec)
    source_after = _sha256_file(source)
    lease_absent = not (runtime / "solver_owner.json").exists()
    receipt = {
        "success": exit_code == 0 and state["status"] == "completed" and source_after == source_before and lease_absent,
        "dry_run": False,
        "job_id": job_id,
        "worker_exit_code": exit_code,
        "outer_seconds": time.perf_counter() - started,
        "spec_fingerprint": spec["spec_fingerprint"],
        "driver_identity": spec["driver_identity"],
        "source_model_sha256_before": source_before,
        "source_model_sha256_after": source_after,
        "source_unchanged": source_after == source_before,
        "state": state,
        "stage_plans": [
            {
                "stage_index": stage["stage_index"],
                "stage_kind": stage["stage_kind"],
                "window": stage["window"],
                "requested_wavelengths_m": stage["requested_wavelengths_m"],
                "stage_sha256": stage["stage_sha256"],
            }
            for stage in stages
        ],
        "rows": [_row_receipt(row) for row in rows],
        "cleanup": {
            "lease_absent": lease_absent,
            "worker_state_cleanup": state.get("cleanup"),
            "external_process_absence": "parent_must_verify_after_runner_exit",
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
