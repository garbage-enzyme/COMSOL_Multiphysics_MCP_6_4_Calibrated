"""H4f forced 30-second hang/crash containment with live control-plane polls."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
import uuid

import psutil

from src.jobs.manager import JobManager
from src.knowledge.lexical_manual import run_bounded
from src.knowledge.semantic_process import SemanticWorkerManager
from src.tools.capabilities import get_capabilities
from src.tools.ownership import SolverOwnership


ROOT = Path("D:/comsol_runtime/H4f")
MODEL = "D:/comsol_semantic/models/all-MiniLM-L6-v2/1110a243fdf4706b3f48f1d95db1a4f5529b4d41"


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
            command = " ".join(process.info.get("cmdline") or [])
            if "src.knowledge.semantic_worker" in command:
                found.append(int(process.info["pid"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return found


def _poll_controls(job_manager: JobManager, job_id: str, ownership: SolverOwnership) -> dict:
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
    lexical = run_bounded("search", {
        "query": "CopyFace source destination",
        "limit": 3,
        "index_path": "D:/comsol_docs_fts/manuals.sqlite3",
        "mode": "auto",
    }, timeout=2.0)
    timings["manual_search"] = time.perf_counter() - started
    assert capabilities["success"] is True
    assert solver["lease"]["state"] == "absent"
    assert solver["external_solver_processes"] == []
    assert solver["collision"] is False
    assert job["success"] is True and job["status"] == "completed"
    assert lexical["success"] is True and lexical["results"]
    return {"timings": timings, "lexical_count": lexical["count"]}


def _atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".tmp-{uuid.uuid4().hex[:8]}")
    try:
        with temporary.open("wb") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    runtime = ROOT / f"containment-{uuid.uuid4().hex}"
    ownership = SolverOwnership(runtime_dir=runtime / "solver-runtime")
    jobs = JobManager(runtime / "jobs", reconcile_on_start=False)
    job_id = jobs.store.create(
        {"schema_version": "2", "job_type": "test_sequence"},
        {"schema_version": "2", "status": "completed", "worker_pid": None},
    )
    hanging = _manager(fault="query_hang", deadline=30.0)
    result: dict = {}
    query_started = time.perf_counter()
    thread = threading.Thread(
        target=lambda: result.update(hanging.query("forced thirty second hang")),
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 15.0
    while (hanging._process is None or hanging._port is None) and time.monotonic() < deadline:
        time.sleep(0.05)
    if hanging._process is None or hanging._port is None:
        raise RuntimeError("hanging worker did not become ready")
    hang_pid = hanging._process.pid
    polls = []
    for index in range(4):
        polls.append({"poll": index + 1, **_poll_controls(jobs, job_id, ownership)})
        if index < 3:
            time.sleep(5.0)
    thread.join(timeout=20.0)
    hang_wall = time.perf_counter() - query_started
    if thread.is_alive():
        hanging.reset()
        raise RuntimeError("30-second semantic hang did not terminate")
    assert result["success"] is False
    assert result["retried"] is False
    assert result["cleanup"]["absent"] is True
    assert not psutil.pid_exists(hang_pid)

    crashing = _manager(fault="crash_before_response", deadline=5.0)
    crash_started = time.perf_counter()
    crash = crashing.query("forced crash")
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
    print(json.dumps({
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
    }, indent=2))


if __name__ == "__main__":
    main()
