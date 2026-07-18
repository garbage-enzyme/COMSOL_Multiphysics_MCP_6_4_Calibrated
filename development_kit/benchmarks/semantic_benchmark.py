"""Lexical baseline benchmark for the frozen semantic-retrieval evaluation set."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import statistics
import time
from typing import Any, Iterable, Mapping

from comsol_mcp.knowledge.lexical_manual import DEFAULT_INDEX_PATH, search_index
from comsol_mcp.knowledge.semantic_contracts import (
    SEMANTIC_CONTINUATION_GATE,
    SEMANTIC_PROMOTION_GATE,
    evaluate_semantic_continuation,
    object_sha256,
    validate_evaluation_set,
)


DEFAULT_EVALUATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "development_kit"
    / "tests"
    / "fixtures"
    / "semantic_retrieval_evaluation.json"
)
BASELINE_SCHEMA_VERSION = "1"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _dcg(relevance: Iterable[int]) -> float:
    return sum(value / math.log2(index + 2) for index, value in enumerate(relevance))


def _query_metrics(ranked: list[tuple[str, int]], relevant: set[tuple[str, int]]) -> dict[str, Any]:
    if not relevant:
        return {
            "recall_at_5": None,
            "recall_at_10": None,
            "reciprocal_rank_at_10": None,
            "ndcg_at_10": None,
            "first_relevant_rank": None,
            "miss_at_5": False,
            "negative_abstained": len(ranked) == 0,
        }
    hits = [1 if citation in relevant else 0 for citation in ranked[:10]]
    first_rank = next((index + 1 for index, hit in enumerate(hits) if hit), None)
    retrieved_5 = set(ranked[:5])
    retrieved_10 = set(ranked[:10])
    recall_5 = len(retrieved_5 & relevant) / len(relevant)
    recall_10 = len(retrieved_10 & relevant) / len(relevant)
    ideal = [1] * min(len(relevant), 10)
    ideal_dcg = _dcg(ideal)
    return {
        "recall_at_5": recall_5,
        "recall_at_10": recall_10,
        "reciprocal_rank_at_10": 0.0 if first_rank is None else 1.0 / first_rank,
        "ndcg_at_10": 0.0 if ideal_dcg == 0 else _dcg(hits) / ideal_dcg,
        "first_relevant_rank": first_rank,
        "miss_at_5": recall_5 == 0,
        "negative_abstained": None,
    }


def _aggregate(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "query_count": 0,
            "recall_at_5": math.nan,
            "recall_at_10": math.nan,
            "mrr_at_10": math.nan,
            "ndcg_at_10": math.nan,
            "zero_result_rate": math.nan,
            "misses_at_5": 0,
            "negative_query_count": 0,
            "negative_abstention_rate": None,
        }
    judged = [row for row in rows if row["relevant"]]
    negative = [row for row in rows if not row["relevant"]]
    def mean_metric(name: str) -> float | None:
        return statistics.fmean(row["metrics"][name] for row in judged) if judged else None
    return {
        "query_count": len(rows),
        "judged_query_count": len(judged),
        "recall_at_5": mean_metric("recall_at_5"),
        "recall_at_10": mean_metric("recall_at_10"),
        "mrr_at_10": mean_metric("reciprocal_rank_at_10"),
        "ndcg_at_10": mean_metric("ndcg_at_10"),
        "zero_result_rate": statistics.fmean(1.0 if row["result_count"] == 0 else 0.0 for row in rows),
        "misses_at_5": sum(1 for row in judged if row["metrics"]["miss_at_5"]),
        "negative_query_count": len(negative),
        "negative_abstention_rate": (
            statistics.fmean(1.0 if row["metrics"]["negative_abstained"] else 0.0 for row in negative)
            if negative else None
        ),
    }


def _index_identity(index_path: Path) -> dict[str, Any]:
    uri = index_path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=0.25) as connection:
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        citations = {
            (str(source), int(page))
            for source, page in connection.execute("SELECT source, page FROM pages")
        }
    return {
        "path": str(index_path.resolve()),
        "sha256": _sha256_file(index_path),
        "schema_version": metadata.get("schema_version"),
        "corpus_fingerprint": metadata.get("corpus_fingerprint"),
        "page_count": int(metadata.get("page_count", "0")),
        "citations": citations,
    }


def evaluate_lexical_baseline(
    evaluation: Mapping[str, Any],
    *,
    index_path: str | Path = DEFAULT_INDEX_PATH,
) -> dict[str, Any]:
    """Evaluate the current deterministic BM25 implementation at ranks 5 and 10."""
    frozen = validate_evaluation_set(evaluation)
    index = _index_identity(Path(index_path))
    if frozen["corpus_fingerprint"] != index["corpus_fingerprint"]:
        raise ValueError("evaluation corpus_fingerprint does not match the lexical index")

    missing_judgments = []
    for item in frozen["queries"]:
        for citation in item["relevant"]:
            key = (citation["source"], citation["page"])
            if key not in index["citations"]:
                missing_judgments.append({"query_id": item["id"], "source": key[0], "page": key[1]})
    if missing_judgments:
        raise ValueError(f"evaluation contains missing corpus citations: {missing_judgments[:5]}")

    rows = []
    for item in frozen["queries"]:
        started = time.perf_counter()
        result = search_index(item["query"], limit=10, index_path=index_path, mode="auto")
        elapsed = time.perf_counter() - started
        ranked = [(row["source"], int(row["page"])) for row in result["results"]]
        relevant = {(row["source"], int(row["page"])) for row in item["relevant"]}
        rows.append({
            "id": item["id"],
            "query": item["query"],
            "category": item["category"],
            "style": item["style"],
            "relevant": item["relevant"],
            "ranked_citations": [{"source": source, "page": page} for source, page in ranked],
            "result_count": len(ranked),
            "strategy": result["strategy"],
            "elapsed_seconds": elapsed,
            "metrics": _query_metrics(ranked, relevant),
        })

    by_category = {
        category: _aggregate([row for row in rows if row["category"] == category])
        for category in sorted({row["category"] for row in rows})
    }
    by_style = {
        style: _aggregate([row for row in rows if row["style"] == style])
        for style in sorted({row["style"] for row in rows})
    }
    target_style_names = set(SEMANTIC_CONTINUATION_GATE["target_styles"])
    target_rows = [
        row for row in rows
        if row["style"] in target_style_names and row["relevant"]
    ]
    latencies = [row["elapsed_seconds"] for row in rows]
    summary = {
        "overall": _aggregate(rows),
        "by_category": by_category,
        "by_style": by_style,
        "target_styles": _aggregate(target_rows),
        "latency_seconds": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "maximum": max(latencies),
        },
        "citation_validity": 1.0,
    }
    public_index = {key: value for key, value in index.items() if key != "citations"}
    output = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "created_at_epoch": time.time(),
        "evaluation_sha256": object_sha256(frozen),
        "evaluation_name": frozen["name"],
        "query_count": len(rows),
        "lexical_index": public_index,
        "summary": summary,
        "continuation_gate": evaluate_semantic_continuation(summary),
        "future_promotion_gate": SEMANTIC_PROMOTION_GATE,
        "queries": rows,
    }
    return output


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    temporary.write_bytes(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n")
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation", default=str(DEFAULT_EVALUATION_PATH))
    parser.add_argument("--index", default=str(DEFAULT_INDEX_PATH))
    parser.add_argument("--output")
    args = parser.parse_args()
    evaluation = json.loads(Path(args.evaluation).read_text(encoding="utf-8"))
    result = evaluate_lexical_baseline(evaluation, index_path=args.index)
    if args.output:
        output_path = Path(args.output)
        _atomic_write_json(output_path, result)
        displayed = {
            "success": True,
            "output": str(output_path),
            "query_count": result["query_count"],
            "summary": result["summary"],
            "continuation_gate": result["continuation_gate"],
        }
    else:
        displayed = result
    print(json.dumps(displayed, indent=2, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
