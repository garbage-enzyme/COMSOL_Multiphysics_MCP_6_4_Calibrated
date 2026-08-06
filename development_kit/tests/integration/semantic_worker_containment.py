"""semantic soak forced 30-second hang/crash containment with live control-plane polls."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

import psutil
from src.jobs.manager import JobManager
from src.jobs.store import JobLock
from src.knowledge.lexical_manual import run_bounded
from src.knowledge.semantic_process import SemanticWorkerManager
from src.tools.capabilities import get_capabilities
from src.tools.ownership import SolverOwnership

ROOT = Path("D:/comsol_runtime/semantic_soak")
MODEL = "D:/comsol_semantic/models/all-MiniLM-L6-v2/1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
RUN_LOCK = ROOT / "containment.acceptance.lock"
CONTROL_WATCHDOG_SECONDS = 10.0


def _manager(*, fault: str, deadline: float) -> SemanticWorkerManager:
    return SemanticWorkerManager(
        backend="hybrid",
        deployment_root="D:/comsol_semantic",
        lexical_index="D:/comsol_docs_fts/manuals.sqlite3",
        model_path=MODEL,
        startup_deadline=20.0,
        query_deadline=deadline,
        idle_ttl=300.0,
        fault=fault,
    )


def _semantic_worker_pids() -> list[int]:
    found = []
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            command = process.info.get("cmdline") or []
            if any(
                command[index : index + 2] == ["-m", "comsol_mcp.knowledge.semantic_worker"]
                for index in range(max(0, len(command) - 1))
            ):
                found.append(int(process.info["pid"]))
        except psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess:
            continue
    return found


def _collect_controls(runtime: Path, job_id: str) -> dict:
    job_manager = JobManager(runtime / "jobs", reconcile_on_start=False)
    ownership = SolverOwnership(runtime_dir=runtime / "solver-runtime")
    timings = {}
    started = time.perf_counter()
    capabilities = get_capabilities()
    timings["capabilities"] = time.perf_counter() - started
    started = time.perf_counter()
    solver = ownership.status()
    timings["solver_status"] = time.perf_counter() - started
    started = time.perf_counter()
    job = job_manager.status(job_id)
    timings["job_status"] = time.perf_counter() - started
    started = time.perf_counter()
    lexical = run_bounded(
        "search",
        {
            "query": "CopyFace source destination",
            "limit": 3,
            "index_path": "D:/comsol_docs_fts/manuals.sqlite3",
            "mode": "auto",
        },
        timeout=2.0,
    )
    timings["manual_search"] = time.perf_counter() - started
    if capabilities.get("success") is not True:
        raise RuntimeError("capabilities control probe failed")
    if (
        solver["lease"]["state"] != "absent"
        or solver["external_solver_processes"] != []
        or solver["collision"] is not False
    ):
        raise RuntimeError("solver control probe changed or found ownership")
    if job.get("success") is not True or job.get("status") != "completed":
        raise RuntimeError("job control probe failed")
    if lexical.get("success") is not True or not lexical.get("results"):
        raise RuntimeError("manual-search control probe failed")
    return {"timings": timings, "lexical_count": lexical["count"]}


def _poll_controls(runtime: Path, job_id: str) -> dict:
    command = [
        sys.executable,
        "-m",
        "development_kit.tests.integration.semantic_worker_containment",
        "--control-probe",
        "--runtime",
        str(runtime),
        "--job-id",
        job_id,
    ]
    completed = subprocess.run(
        command,
        cwd=Path(__file__).parents[3],
        capture_output=True,
        text=True,
        timeout=CONTROL_WATCHDOG_SECONDS,
        creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
    )
    if completed.returncode != 0:
        raise RuntimeError("control-plane watchdog subprocess failed")
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise RuntimeError("control-plane watchdog returned malformed output")
    payload = json.loads(lines[0])
    if not isinstance(payload, dict):
        raise RuntimeError("control-plane watchdog returned a non-object")
    return payload


def _require_future_result(future, timeout: float) -> dict:
    try:
        result = future.result(timeout=timeout)
    except FutureTimeout as exc:
        raise RuntimeError("semantic query did not terminate before its deadline") from exc
    if not isinstance(result, dict):
        raise RuntimeError("semantic query thread returned a non-object")
    return result


def _atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".tmp-{uuid.uuid4().hex[:8]}")
    try:
        with temporary.open("wb") as handle:
            handle.write(
                json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
                + b"\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _run_containment() -> None:
    runtime = ROOT / f"containment-{uuid.uuid4().hex}"
    ownership = SolverOwnership(runtime_dir=runtime / "solver-runtime")
    jobs = JobManager(runtime / "jobs", reconcile_on_start=False)
    job_id = jobs.store.create(
        {"schema_version": "2", "job_type": "test_sequence"},
        {"schema_version": "2", "status": "completed", "worker_pid": None},
    )
    try:
        query_started = time.perf_counter()
        with _manager(fault="query_hang", deadline=30.0) as hanging:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(hanging.query, "forced thirty second hang")
                deadline = time.monotonic() + hanging.startup_deadline + 5.0
                while (
                    hanging._process is None or hanging._port is None
                ) and time.monotonic() < deadline:
                    time.sleep(0.05)
                if hanging._process is None or hanging._port is None:
                    raise RuntimeError("hanging worker did not become ready")
                hang_pid = hanging._process.pid
                polls = []
                for index in range(4):
                    polls.append({"poll": index + 1, **_poll_controls(runtime, job_id)})
                    if index < 3:
                        time.sleep(5.0)
                result = _require_future_result(future, 35.0)
            hang_wall = time.perf_counter() - query_started
            assert result["success"] is False
            assert result["retried"] is False
            assert result["cleanup"]["absent"] is True
            assert not psutil.pid_exists(hang_pid)

        with _manager(fault="crash_before_response", deadline=5.0) as crashing:
            crash_started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=1) as pool:
                crash = _require_future_result(
                    pool.submit(crashing.query, "forced crash"),
                    crashing.startup_deadline + crashing.query_deadline + 5.0,
                )
            crash_wall = time.perf_counter() - crash_started
            assert crash["success"] is False
            assert crash["cleanup"]["absent"] is True
        final_solver = ownership.status()
        assert final_solver["lease"]["state"] == "absent"
        assert final_solver["external_solver_processes"] == []
        assert not _semantic_worker_pids()
        output = {
            "schema_version": "1",
            "success": True,
            "hang": {
                "worker_pid": hang_pid,
                "wall_seconds": hang_wall,
                "result": result,
                "control_polls": polls,
            },
            "crash": {"wall_seconds": crash_wall, "result": crash},
            "final": {
                "semantic_worker_pids": _semantic_worker_pids(),
                "solver_lease": final_solver["lease"]["state"],
                "external_solver_processes": final_solver["external_solver_processes"],
                "collision": final_solver["collision"],
            },
        }
        _atomic_write(ROOT / "containment.json", output)
        print(
            json.dumps(
                {
                    "success": True,
                    "hang_wall_seconds": hang_wall,
                    "crash_wall_seconds": crash_wall,
                    "poll_max_seconds": {
                        key: max(poll["timings"][key] for poll in polls)
                        for key in polls[0]["timings"]
                    },
                    "hang_cleanup_absent": result["cleanup"]["absent"],
                    "crash_cleanup_absent": crash["cleanup"]["absent"],
                    "final": output["final"],
                    "artifact": str(ROOT / "containment.json"),
                },
                indent=2,
            )
        )
    finally:
        shutil.rmtree(runtime, ignore_errors=True)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-probe", action="store_true")
    parser.add_argument("--runtime")
    parser.add_argument("--job-id")
    args = parser.parse_args()
    if args.control_probe:
        if not args.runtime or not args.job_id:
            raise SystemExit("control probe requires --runtime and --job-id")
        print(json.dumps(_collect_controls(Path(args.runtime), args.job_id)))
        return
    with JobLock(RUN_LOCK, timeout=5.0):
        _run_containment()


if __name__ == "__main__":
    main()
