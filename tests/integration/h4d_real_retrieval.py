"""Real H4d isolated-worker acceptance against the pinned local MiniLM index."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
import uuid

from src.knowledge.semantic_index import index_file_snapshot, read_current
from src.knowledge.semantic_process import SemanticWorkerManager
from src.tools.ownership import ownership_manager


DEFAULT_ROOT = Path("D:/comsol_semantic")
DEFAULT_LEXICAL = Path("D:/comsol_docs_fts/manuals.sqlite3")
DEFAULT_MODEL = DEFAULT_ROOT / "models" / "all-MiniLM-L6-v2" / "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"


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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="D:/comsol_runtime/H4d/real_retrieval.json")
    args = parser.parse_args()
    current = read_current(DEFAULT_ROOT)
    index_path = Path(current["pointer"]["index_path"])
    before = index_file_snapshot(index_path)
    ownership_before = ownership_manager.status()
    manager = SemanticWorkerManager(
        backend="hybrid",
        deployment_root=str(DEFAULT_ROOT),
        lexical_index=str(DEFAULT_LEXICAL),
        model_path=str(DEFAULT_MODEL),
        startup_deadline=20.0,
        query_deadline=5.0,
        idle_ttl=300.0,
    )
    queries = [
        ("exact", "CopyFace source destination mesh", "hybrid"),
        ("paraphrase", "How can periodic boundary faces be forced to use identical discretization?", "hybrid"),
        ("chinese", "周期端口的相邻介质为什么必须均匀", "hybrid"),
        ("vector_only", "homogeneous medium next to a Floquet excitation boundary", "vector"),
    ]
    results = []
    try:
        started = time.perf_counter()
        startup = manager.start()
        cold_seconds = time.perf_counter() - started
        if not startup.get("success"):
            raise RuntimeError(f"semantic worker startup failed: {startup}")
        for label, query, mode in queries:
            query_started = time.perf_counter()
            response = manager.query(query, limit=5, retrieval_mode=mode)
            elapsed = time.perf_counter() - query_started
            if not response.get("success"):
                raise RuntimeError(f"semantic query failed: {response}")
            encoded = json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            results.append({
                "label": label,
                "query": query,
                "retrieval_mode": mode,
                "elapsed_seconds": elapsed,
                "response_bytes": len(encoded),
                "count": response["count"],
                "results": response["results"],
                "ranker": response["ranker"],
                "load_count": response["load_count"],
                "query_count": response["query_count"],
            })
        repeat = manager.query(queries[0][1], limit=5, retrieval_mode="hybrid")
        if not repeat.get("success"):
            raise RuntimeError(f"repeat query failed: {repeat}")
        health = manager.health()
        if not health.get("success"):
            raise RuntimeError(f"health failed: {health}")
    finally:
        reset = manager.reset()
    after = index_file_snapshot(index_path)
    ownership_after = ownership_manager.status()
    output = {
        "schema_version": "1",
        "phase": "H4d",
        "success": True,
        "cold_start_seconds": cold_seconds,
        "queries": results,
        "repeat_results_identical": repeat["results"] == results[0]["results"],
        "final_load_count": health["status"]["load_count"],
        "final_query_count": health["status"]["query_count"],
        "worker_reset": reset,
        "index_immutable": before == after,
        "ownership_before": {
            "lease": ownership_before["lease"]["state"],
            "external_solver_processes": len(ownership_before["external_solver_processes"]),
            "collision": ownership_before["collision"],
        },
        "ownership_after": {
            "lease": ownership_after["lease"]["state"],
            "external_solver_processes": len(ownership_after["external_solver_processes"]),
            "collision": ownership_after["collision"],
        },
    }
    if output["final_load_count"] != 1:
        raise RuntimeError("model loaded more than once")
    if not output["repeat_results_identical"] or not output["index_immutable"]:
        raise RuntimeError("determinism or immutability gate failed")
    if max(item["elapsed_seconds"] for item in results) >= 5.0:
        raise RuntimeError("a real query exceeded the hard deadline")
    if max(item["response_bytes"] for item in results) > 65_536:
        raise RuntimeError("a real response exceeded the public byte limit")
    if output["ownership_after"] != {"lease": "absent", "external_solver_processes": 0, "collision": False}:
        raise RuntimeError("solver ownership changed during semantic acceptance")
    _atomic_write(Path(args.output), output)
    print(json.dumps(output, ensure_ascii=False, allow_nan=False, indent=2))


if __name__ == "__main__":
    main()
