"""Frozen semantic soak retrieval benchmark, 500-query soak, and concurrent burst."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import sqlite3
import statistics
import threading
import time
from typing import Any, Iterable, Mapping
import uuid

import psutil

from development_kit.benchmarks.semantic_benchmark import evaluate_lexical_baseline
from src.knowledge.semantic_contracts import (
    SEMANTIC_PROMOTION_GATE,
    WORKER_PROTOCOL_SCHEMA_VERSION,
    object_sha256,
    validate_evaluation_set,
)
from src.knowledge.semantic_index import index_file_snapshot, read_current
from src.knowledge.semantic_process import SemanticWorkerManager


ROOT = Path(__file__).parents[3]
EVALUATION_PATH = ROOT / "development_kit" / "tests" / "fixtures" / "semantic_retrieval_evaluation.json"
DEPLOYMENT = Path("D:/comsol_semantic")
LEXICAL = Path("D:/comsol_docs_fts/manuals.sqlite3")
MODEL = DEPLOYMENT / "models" / "all-MiniLM-L6-v2" / "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def _dcg(relevance: Iterable[int]) -> float:
    return sum(value / math.log2(index + 2) for index, value in enumerate(relevance))


def _metrics(ranked: list[tuple[str, int]], relevant: set[tuple[str, int]]) -> dict[str, Any]:
    if not relevant:
        return {"negative_abstained": not ranked}
    hits = [1 if item in relevant else 0 for item in ranked[:10]]
    first = next((index + 1 for index, hit in enumerate(hits) if hit), None)
    ideal_dcg = _dcg([1] * min(10, len(relevant)))
    return {
        "recall_at_5": len(set(ranked[:5]) & relevant) / len(relevant),
        "recall_at_10": len(set(ranked[:10]) & relevant) / len(relevant),
        "reciprocal_rank_at_10": 0.0 if first is None else 1.0 / first,
        "ndcg_at_10": _dcg(hits) / ideal_dcg if ideal_dcg else 0.0,
        "miss_at_5": not bool(set(ranked[:5]) & relevant),
    }


def _aggregate(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    positive = [row for row in rows if row["relevant"]]
    negative = [row for row in rows if not row["relevant"]]
    return {
        "query_count": len(rows),
        "positive_query_count": len(positive),
        "negative_query_count": len(negative),
        "recall_at_5": statistics.fmean(row["metrics"]["recall_at_5"] for row in positive) if positive else None,
        "recall_at_10": statistics.fmean(row["metrics"]["recall_at_10"] for row in positive) if positive else None,
        "mrr_at_10": statistics.fmean(row["metrics"]["reciprocal_rank_at_10"] for row in positive) if positive else None,
        "ndcg_at_10": statistics.fmean(row["metrics"]["ndcg_at_10"] for row in positive) if positive else None,
        "misses_at_5": sum(1 for row in positive if row["metrics"]["miss_at_5"]),
        "negative_abstention_rate": statistics.fmean(1.0 if row["metrics"]["negative_abstained"] else 0.0 for row in negative) if negative else None,
    }


def _summaries(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "overall": _aggregate(rows),
        "by_style": {
            style: _aggregate([row for row in rows if row["style"] == style])
            for style in sorted({row["style"] for row in rows})
        },
        "by_category": {
            category: _aggregate([row for row in rows if row["category"] == category])
            for category in sorted({row["category"] for row in rows})
        },
        "paraphrase_multi": _aggregate([
            row for row in rows if row["style"] in {"paraphrase", "multi_concept"}
        ]),
    }


def _manager() -> SemanticWorkerManager:
    return SemanticWorkerManager(
        backend="hybrid",
        deployment_root=str(DEPLOYMENT),
        lexical_index=str(LEXICAL),
        model_path=str(MODEL),
        startup_deadline=20.0,
        query_deadline=5.0,
        idle_ttl=600.0,
    )


def _corpus_citations() -> set[tuple[str, int]]:
    with sqlite3.connect(LEXICAL.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        return {(str(source), int(page)) for source, page in connection.execute("SELECT source, page FROM pages")}


def _evaluate_mode(manager: SemanticWorkerManager, evaluation: Mapping[str, Any], mode: str, corpus: set[tuple[str, int]]) -> dict[str, Any]:
    rows = []
    latencies = []
    response_bytes = []
    valid = 0
    returned = 0
    for number, item in enumerate(evaluation["queries"], 1):
        started = time.perf_counter()
        response = manager.query(item["query"], limit=10, retrieval_mode=mode)
        elapsed = time.perf_counter() - started
        if not response.get("success"):
            raise RuntimeError(f"{mode} query failed for {item['id']}: {response}")
        ranked = [(row["source"], int(row["page"])) for row in response["results"]]
        relevant = {(row["source"], int(row["page"])) for row in item["relevant"]}
        returned += len(ranked)
        valid += sum(1 for citation in ranked if citation in corpus)
        encoded_size = len(json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
        latencies.append(elapsed)
        response_bytes.append(encoded_size)
        rows.append({
            "id": item["id"], "category": item["category"], "style": item["style"],
            "relevant": item["relevant"],
            "ranked_citations": [{"source": source, "page": page} for source, page in ranked],
            "metrics": _metrics(ranked, relevant),
            "elapsed_seconds": elapsed,
            "response_bytes": encoded_size,
        })
        if number % 20 == 0:
            print(json.dumps({"phase": "benchmark", "mode": mode, "completed": number}), flush=True)
    return {
        "mode": mode,
        "summary": _summaries(rows),
        "latency_seconds": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "maximum": max(latencies),
        },
        "response_bytes": {"maximum": max(response_bytes), "p95": _percentile([float(value) for value in response_bytes], 0.95)},
        "citation_validity": valid / returned if returned else 1.0,
        "rows": rows,
    }


def _promotion(lexical: Mapping[str, Any], hybrid: Mapping[str, Any]) -> dict[str, Any]:
    lexical_exact = float(lexical["summary"]["by_style"]["exact"]["recall_at_5"])
    hybrid_exact = float(hybrid["summary"]["by_style"]["exact"]["recall_at_5"])
    lexical_target = float(lexical["summary"]["paraphrase_multi"]["recall_at_5"])
    hybrid_target = float(hybrid["summary"]["paraphrase_multi"]["recall_at_5"])
    absolute_gain = hybrid_target - lexical_target
    relative_gain = absolute_gain / lexical_target if lexical_target else math.inf
    gates = {
        "citation_validity": hybrid["citation_validity"] == SEMANTIC_PROMOTION_GATE["citation_validity"],
        "exact_recall_regression": hybrid_exact - lexical_exact >= -SEMANTIC_PROMOTION_GATE["maximum_exact_symbol_recall_at_5_regression"],
        "target_recall_gain": (
            absolute_gain >= SEMANTIC_PROMOTION_GATE["minimum_target_recall_at_5_absolute_gain"]
            or relative_gain >= SEMANTIC_PROMOTION_GATE["minimum_target_recall_at_5_relative_gain"]
        ),
        "warm_p95": hybrid["latency_seconds"]["p95"] < SEMANTIC_PROMOTION_GATE["maximum_warm_p95_seconds"],
        "hard_deadline": hybrid["latency_seconds"]["maximum"] < SEMANTIC_PROMOTION_GATE["hard_query_deadline_seconds"],
        "negative_abstention": hybrid["summary"]["overall"]["negative_abstention_rate"] == 1.0,
    }
    return {
        "passed": all(gates.values()),
        "gates": gates,
        "measurements": {
            "lexical_exact_recall_at_5": lexical_exact,
            "hybrid_exact_recall_at_5": hybrid_exact,
            "exact_regression": hybrid_exact - lexical_exact,
            "lexical_paraphrase_multi_recall_at_5": lexical_target,
            "hybrid_paraphrase_multi_recall_at_5": hybrid_target,
            "absolute_gain": absolute_gain,
            "relative_gain": relative_gain,
            "hybrid_negative_abstention_rate": hybrid["summary"]["overall"]["negative_abstention_rate"],
        },
        "thresholds": SEMANTIC_PROMOTION_GATE,
        "decision": "promote" if all(gates.values()) else "retain_experimental_lexical_default",
    }


def _raw_request(manager: SemanticWorkerManager, request_id: str, query: str) -> dict[str, Any]:
    payload = {
        "schema_version": WORKER_PROTOCOL_SCHEMA_VERSION,
        "request_id": request_id,
        "token": manager._token,
        "operation": "query",
        "query": query,
        "limit": 3,
        "filters": None,
        "retrieval_mode": "hybrid",
    }
    with socket.create_connection(("127.0.0.1", int(manager._port)), timeout=15.0) as connection:
        connection.settimeout(15.0)
        connection.sendall(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
        data = bytearray()
        while not data.endswith(b"\n"):
            block = connection.recv(4096)
            if not block:
                break
            data.extend(block)
    return json.loads(bytes(data).decode("utf-8"))


def _soak(evaluation: Mapping[str, Any], index_path: Path) -> dict[str, Any]:
    manager = _manager()
    before = index_file_snapshot(index_path)
    startup_started = time.perf_counter()
    startup = manager.start()
    cold = time.perf_counter() - startup_started
    if not startup.get("success"):
        raise RuntimeError(f"soak worker failed to start: {startup}")
    process = psutil.Process(int(startup["identity"]["pid"]))
    rss_start = process.memory_info().rss
    latencies = []
    sizes = []
    errors = []
    samples = []
    try:
        for number in range(500):
            item = evaluation["queries"][number % len(evaluation["queries"])]
            started = time.perf_counter()
            response = manager.query(item["query"], limit=5, retrieval_mode="hybrid")
            elapsed = time.perf_counter() - started
            latencies.append(elapsed)
            sizes.append(len(json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")))
            if not response.get("success"):
                errors.append({"iteration": number, "response": response})
            if number % 50 == 0:
                samples.append({
                    "iteration": number,
                    "query_id": item["id"],
                    "count": response.get("count"),
                    "top": [
                        {"source": row["source"], "page": row["page"]}
                        for row in response.get("results", [])[:2]
                    ],
                })
            if (number + 1) % 100 == 0:
                print(json.dumps({"phase": "soak", "completed": number + 1}), flush=True)
        barrier = threading.Barrier(12)

        def burst(index: int) -> dict[str, Any]:
            barrier.wait(timeout=5.0)
            return _raw_request(manager, f"burst-{index}", "CopyFace source destination mesh")

        with ThreadPoolExecutor(max_workers=12) as pool:
            burst_responses = list(pool.map(burst, range(12)))
        health = manager.health()
        rss_end = process.memory_info().rss
    finally:
        reset = manager.reset()
    after = index_file_snapshot(index_path)
    busy = sum(
        1 for response in burst_responses
        if not response.get("success") and response.get("error", {}).get("code") == "busy"
    )
    return {
        "cold_start_seconds": cold,
        "sequential_queries": 500,
        "errors": errors,
        "latency_seconds": {
            "p50": _percentile(latencies, 0.50), "p95": _percentile(latencies, 0.95),
            "maximum": max(latencies),
        },
        "response_bytes": {"maximum": max(sizes), "p95": _percentile([float(value) for value in sizes], 0.95)},
        "samples": samples,
        "rss_bytes": {"start": rss_start, "end": rss_end, "growth": rss_end - rss_start},
        "load_count": health["status"]["load_count"],
        "query_count": health["status"]["query_count"],
        "burst": {
            "requests": len(burst_responses),
            "successes": sum(1 for response in burst_responses if response.get("success")),
            "busy": busy,
        },
        "reset": reset,
        "index_immutable": before == after,
    }


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
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
    parser.add_argument("--output", default="D:/comsol_runtime/semantic_soak/benchmark_soak.json")
    args = parser.parse_args()
    evaluation = validate_evaluation_set(json.loads(EVALUATION_PATH.read_text(encoding="utf-8")))
    corpus = _corpus_citations()
    current = read_current(DEPLOYMENT)
    index_path = Path(current["pointer"]["index_path"])
    lexical_started = time.perf_counter()
    lexical_full = evaluate_lexical_baseline(evaluation, index_path=LEXICAL)
    lexical = {
        "mode": "lexical",
        "summary": {
            **lexical_full["summary"],
            "paraphrase_multi": _aggregate([
                row for row in lexical_full["queries"]
                if row["style"] in {"paraphrase", "multi_concept"}
            ]),
        },
        "latency_seconds": lexical_full["summary"]["latency_seconds"],
        "citation_validity": lexical_full["summary"]["citation_validity"],
        "wall_seconds": time.perf_counter() - lexical_started,
    }
    benchmark_manager = _manager()
    benchmark_start = benchmark_manager.start()
    if not benchmark_start.get("success"):
        raise RuntimeError(f"benchmark worker failed to start: {benchmark_start}")
    benchmark_process = psutil.Process(int(benchmark_start["identity"]["pid"]))
    rss_before = benchmark_process.memory_info().rss
    try:
        vector = _evaluate_mode(benchmark_manager, evaluation, "vector", corpus)
        hybrid = _evaluate_mode(benchmark_manager, evaluation, "hybrid", corpus)
        benchmark_health = benchmark_manager.health()
        rss_after = benchmark_process.memory_info().rss
    finally:
        benchmark_reset = benchmark_manager.reset()
    promotion = _promotion(lexical, hybrid)
    soak = _soak(evaluation, index_path)
    output = {
        "schema_version": "1",
        "phase": "semantic soak",
        "evaluation_sha256": object_sha256(evaluation),
        "evaluation_query_count": len(evaluation["queries"]),
        "lexical": lexical,
        "vector": vector,
        "hybrid": hybrid,
        "promotion": promotion,
        "benchmark_worker": {
            "rss_bytes": {"before": rss_before, "after": rss_after, "growth": rss_after - rss_before},
            "load_count": benchmark_health["status"]["load_count"],
            "query_count": benchmark_health["status"]["query_count"],
            "reset": benchmark_reset,
        },
        "soak": soak,
    }
    if vector["citation_validity"] != 1.0 or hybrid["citation_validity"] != 1.0:
        raise RuntimeError("citation validity gate failed")
    if benchmark_health["status"]["load_count"] != 1 or soak["load_count"] != 1:
        raise RuntimeError("model load-count gate failed")
    if soak["errors"] or not soak["index_immutable"] or not soak["reset"]["reset"]["absent"]:
        raise RuntimeError("soak containment or immutability gate failed")
    if soak["burst"]["busy"] < 1:
        raise RuntimeError("concurrent burst did not exercise bounded busy rejection")
    _atomic_write(Path(args.output), output)
    print(json.dumps({
        "success": True,
        "promotion": promotion,
        "lexical": lexical["summary"],
        "vector": vector["summary"],
        "hybrid": hybrid["summary"],
        "hybrid_latency_seconds": hybrid["latency_seconds"],
        "soak": {key: soak[key] for key in (
            "sequential_queries", "latency_seconds", "response_bytes", "rss_bytes",
            "load_count", "query_count", "burst", "index_immutable",
        )},
        "artifact": str(Path(args.output)),
    }, ensure_ascii=False, allow_nan=False, indent=2))


if __name__ == "__main__":
    main()
