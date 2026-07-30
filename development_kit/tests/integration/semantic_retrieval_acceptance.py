"""Real semantic retrieval isolated-worker acceptance against the pinned local MiniLM index."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Callable

from src.durable.io import atomic_write_json_exclusive
from src.knowledge.semantic_index import index_file_snapshot, read_current
from src.knowledge.semantic_process import SemanticWorkerManager
from src.tools.ownership import ownership_manager

from development_kit.scripts.acceptance_cleanup import CleanupRecorder

DEFAULT_ROOT = Path("D:/comsol_semantic")
DEFAULT_LEXICAL = Path("D:/comsol_docs_fts/manuals.sqlite3")
DEFAULT_MODEL = (
    DEFAULT_ROOT / "models" / "all-MiniLM-L6-v2" / "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
)
QUERIES = (
    (
        "exact",
        "CopyFace source destination mesh selection",
        "hybrid",
        ("COMSOL_Multiphysics/COMSOL_ProgrammingReferenceManual.pdf", 469),
    ),
    (
        "paraphrase",
        "Make the opposite periodic surface reuse exactly the same triangular elements before filling tetrahedra",
        "hybrid",
        ("COMSOL_Multiphysics/COMSOL_ProgrammingReferenceManual.pdf", 469),
    ),
    (
        "chinese",
        "如何用一个单元模拟无限重复的光学阵列并计算反射透射？",
        "hybrid",
        ("Wave_Optics_Module/WaveOpticsModuleUsersGuide.pdf", 33),
    ),
    (
        "vector_only",
        "Wave Excitation at this Port input power listener port boundary mode analysis",
        "vector",
        ("Wave_Optics_Module/WaveOpticsModuleUsersGuide.pdf", 144),
    ),
)


def _failure(exc: BaseException) -> dict[str, str]:
    return {"type": type(exc).__name__, "message": str(exc)[:1000]}


def run_acceptance(
    *,
    manager: SemanticWorkerManager,
    index_path: Path,
    before: dict[str, Any],
    ownership_before: dict[str, Any],
    output_path: Path,
    snapshot: Callable[[Path], dict[str, Any]] = index_file_snapshot,
    ownership_status: Callable[[], dict[str, Any]] = ownership_manager.status,
) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {
        "schema_version": "1",
        "phase": "semantic_retrieval",
        "success": False,
    }
    query_results: list[dict[str, Any]] = []
    repeat: dict[str, Any] | None = None
    health: dict[str, Any] | None = None
    try:
        started = time.perf_counter()
        startup = manager.start()
        cold_seconds = time.perf_counter() - started
        if not startup.get("success"):
            raise RuntimeError(f"semantic worker startup failed: {startup}")
        for label, query, mode, expected_relevant in QUERIES:
            query_started = time.perf_counter()
            response = manager.query(query, limit=5, retrieval_mode=mode)
            elapsed = time.perf_counter() - query_started
            if not response.get("success"):
                raise RuntimeError(f"semantic query failed: {response}")
            encoded = json.dumps(
                response,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            ranked = [
                (str(item.get("source")), int(item.get("page")))
                for item in response["results"][:5]
                if isinstance(item, dict)
                and isinstance(item.get("source"), str)
                and isinstance(item.get("page"), int)
                and not isinstance(item.get("page"), bool)
            ]
            relevant_rank = next(
                (
                    index + 1
                    for index, citation in enumerate(ranked)
                    if citation == expected_relevant
                ),
                None,
            )
            query_results.append(
                {
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
                    "expected_relevant": {
                        "source": expected_relevant[0],
                        "page": expected_relevant[1],
                    },
                    "relevant_rank_at_5": relevant_rank,
                }
            )
        repeat = manager.query(QUERIES[0][1], limit=5, retrieval_mode="hybrid")
        if not repeat.get("success"):
            raise RuntimeError(f"repeat query failed: {repeat}")
        health = manager.health()
        if not health.get("success"):
            raise RuntimeError(f"health failed: {health}")
        result.update(
            {
                "success": True,
                "cold_start_seconds": cold_seconds,
                "queries": query_results,
                "repeat_results_identical": repeat["results"] == query_results[0]["results"],
                "final_load_count": health["status"]["load_count"],
                "final_query_count": health["status"]["query_count"],
            }
        )
    except Exception as exc:
        result["error"] = _failure(exc)
    finally:
        cleanup = CleanupRecorder(result)
        cleanup.run(
            "worker_reset",
            manager.reset,
            passed=lambda value: isinstance(value, dict) and value.get("success") is True,
        )

    try:
        after = snapshot(index_path)
        ownership_after = ownership_status()
        result.update(
            {
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
        )
        if result.get("success") is True:
            if result["final_load_count"] != 1:
                raise RuntimeError("model loaded more than once")
            if not result["repeat_results_identical"] or not result["index_immutable"]:
                raise RuntimeError("determinism or immutability gate failed")
            if max(item["elapsed_seconds"] for item in query_results) >= 5.0:
                raise RuntimeError("a real query exceeded the hard deadline")
            if max(item["response_bytes"] for item in query_results) > 65_536:
                raise RuntimeError("a real response exceeded the public byte limit")
            if any(item["relevant_rank_at_5"] is None for item in query_results):
                raise RuntimeError("one or more judged queries missed relevant evidence at rank 5")
            if result["ownership_after"] != {
                "lease": "absent",
                "external_solver_processes": 0,
                "collision": False,
            }:
                raise RuntimeError("solver ownership changed during semantic acceptance")
    except Exception as exc:
        result["success"] = False
        field = "postcheck_error" if "error" in result else "error"
        result[field] = _failure(exc)

    exit_code = cleanup.finalize()
    atomic_write_json_exclusive(output_path, result)
    return result, exit_code


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", default="D:/comsol_runtime/semantic_retrieval/real_retrieval.json"
    )
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
    output, exit_code = run_acceptance(
        manager=manager,
        index_path=index_path,
        before=before,
        ownership_before=ownership_before,
        output_path=Path(args.output),
    )
    print(json.dumps(output, ensure_ascii=False, allow_nan=False, indent=2))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
