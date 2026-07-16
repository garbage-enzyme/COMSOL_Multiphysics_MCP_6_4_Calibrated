"""Dependency-free contracts for the optional semantic-retrieval service."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping


CONTRACT_VERSION = "1"
EVALUATION_SCHEMA_VERSION = "1"
INDEX_MANIFEST_SCHEMA_VERSION = "1"
MODEL_MANIFEST_SCHEMA_VERSION = "1"
WORKER_PROTOCOL_SCHEMA_VERSION = "1"
DEFAULT_DEPLOYMENT_ROOT = Path("D:/comsol_semantic")

ALLOWED_CATEGORIES = frozenset({
    "exact_clientapi",
    "wave_optics",
    "conventional_fem",
    "troubleshooting",
    "negative",
})
ALLOWED_STYLES = frozenset({
    "exact",
    "paraphrase",
    "multi_concept",
    "zh_cross_language",
})

PUBLIC_LIMITS = {
    "status_deadline_seconds": 1.0,
    "query_deadline_seconds": 5.0,
    "maximum_query_characters": 2_000,
    "maximum_results": 10,
    "maximum_snippet_characters": 1_200,
    "maximum_response_bytes": 65_536,
    "maximum_queue_depth": 8,
}

SEMANTIC_CONTINUATION_GATE = {
    "target_styles": ["paraphrase", "multi_concept", "zh_cross_language"],
    "maximum_lexical_recall_at_5": 0.80,
    "minimum_target_queries": 20,
    "minimum_target_misses_at_5": 10,
}

SEMANTIC_PROMOTION_GATE = {
    "citation_validity": 1.0,
    "maximum_exact_symbol_recall_at_5_regression": 0.03,
    "minimum_target_recall_at_5_absolute_gain": 0.10,
    "minimum_target_recall_at_5_relative_gain": 0.15,
    "maximum_warm_p95_seconds": 3.0,
    "hard_query_deadline_seconds": 5.0,
}

THREAT_MODEL = {
    "protected_parent": "COMSOL MCP control process",
    "untrusted_failures": [
        "embedding_model_load_hang",
        "vector_query_hang",
        "worker_crash",
        "invalid_or_oversized_json",
        "stale_pid_or_port",
        "corrupt_or_mismatched_index",
        "unexpected_network_download",
    ],
    "containment": [
        "heavy_dependencies_import_only_in_worker",
        "localhost_random_port_and_session_token",
        "hard_socket_deadlines",
        "exact_recorded_worker_tree_termination",
        "no_automatic_query_retry",
        "lexical_fallback_remains_independent",
        "no_solver_lease_or_COMSOL_start",
    ],
}


def canonical_json_bytes(value: Any) -> bytes:
    """Encode a contract object deterministically for hashing."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def object_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _require_ascii_absolute_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    if not value.isascii():
        raise ValueError(f"{label} must contain ASCII characters only")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    return str(path)


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{label} must be a lowercase SHA-256 hex string")
    if value != value.lower() or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 hex string")
    return value


def validate_evaluation_set(payload: Mapping[str, Any], *, minimum_queries: int = 60) -> dict[str, Any]:
    """Validate and normalize the frozen semantic-retrieval evaluation set."""
    if not isinstance(payload, Mapping):
        raise ValueError("evaluation set must be an object")
    if payload.get("schema_version") != EVALUATION_SCHEMA_VERSION:
        raise ValueError("unsupported evaluation schema_version")
    corpus_fingerprint = _require_sha256(
        payload.get("corpus_fingerprint"), "corpus_fingerprint"
    )
    queries = payload.get("queries")
    if not isinstance(queries, list) or len(queries) < minimum_queries:
        raise ValueError(f"evaluation set must contain at least {minimum_queries} queries")

    normalized_queries = []
    seen_ids: set[str] = set()
    for index, item in enumerate(queries):
        if not isinstance(item, Mapping):
            raise ValueError(f"queries[{index}] must be an object")
        qid = item.get("id")
        if not isinstance(qid, str) or not qid or len(qid) > 80:
            raise ValueError(f"queries[{index}].id is invalid")
        if qid in seen_ids:
            raise ValueError(f"duplicate query id: {qid}")
        seen_ids.add(qid)
        query = item.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(f"queries[{index}].query is invalid")
        if len(query) > PUBLIC_LIMITS["maximum_query_characters"]:
            raise ValueError(f"queries[{index}].query exceeds the public limit")
        category = item.get("category")
        style = item.get("style")
        if category not in ALLOWED_CATEGORIES:
            raise ValueError(f"queries[{index}].category is invalid")
        if style not in ALLOWED_STYLES:
            raise ValueError(f"queries[{index}].style is invalid")
        relevant = item.get("relevant")
        expected_no_relevant = item.get("expected_no_relevant", False)
        if not isinstance(expected_no_relevant, bool):
            raise ValueError(f"queries[{index}].expected_no_relevant must be boolean")
        if not isinstance(relevant, list):
            raise ValueError(f"queries[{index}].relevant must be a list")
        if expected_no_relevant != (len(relevant) == 0):
            raise ValueError(
                f"queries[{index}] must declare expected_no_relevant exactly when relevant is empty"
            )
        if expected_no_relevant and category != "negative":
            raise ValueError(f"queries[{index}] empty relevance is restricted to the negative category")
        if not expected_no_relevant and category == "negative":
            raise ValueError(f"queries[{index}] negative category must have empty relevance")
        normalized_relevant = []
        seen_citations = set()
        for citation in relevant:
            if not isinstance(citation, Mapping):
                raise ValueError(f"queries[{index}] has an invalid citation")
            source = citation.get("source")
            page = citation.get("page")
            if not isinstance(source, str) or not source.endswith(".pdf"):
                raise ValueError(f"queries[{index}] citation source is invalid")
            source = source.replace("\\", "/")
            if not isinstance(page, int) or isinstance(page, bool) or page < 1:
                raise ValueError(f"queries[{index}] citation page is invalid")
            key = (source, page)
            if key not in seen_citations:
                seen_citations.add(key)
                normalized_relevant.append({"source": source, "page": page})
        judge_note = item.get("judge_note")
        if not isinstance(judge_note, str) or not judge_note.strip():
            raise ValueError(f"queries[{index}].judge_note is required")
        normalized_queries.append({
            "id": qid,
            "query": query.strip(),
            "category": category,
            "style": style,
            "relevant": normalized_relevant,
            "expected_no_relevant": expected_no_relevant,
            "judge_note": judge_note.strip(),
        })

    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "name": str(payload.get("name") or "semantic-retrieval-evaluation"),
        "frozen_at": str(payload.get("frozen_at") or ""),
        "corpus_fingerprint": corpus_fingerprint,
        "queries": normalized_queries,
    }


def validate_model_manifest(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the immutable offline embedding-model identity contract."""
    required = {"schema_version", "model_id", "revision", "model_path", "model_sha256", "dimension"}
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError(f"model manifest missing fields: {missing}")
    if payload["schema_version"] != MODEL_MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported model manifest schema_version")
    dimension = payload["dimension"]
    if not isinstance(dimension, int) or isinstance(dimension, bool) or dimension < 1:
        raise ValueError("dimension must be a positive integer")
    return {
        "schema_version": MODEL_MANIFEST_SCHEMA_VERSION,
        "model_id": str(payload["model_id"]),
        "revision": str(payload["revision"]),
        "model_path": _require_ascii_absolute_path(payload["model_path"], "model_path"),
        "model_sha256": _require_sha256(payload["model_sha256"], "model_sha256"),
        "dimension": dimension,
        "license": str(payload.get("license") or "unknown"),
    }


def validate_index_manifest(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the versioned read-only semantic-index identity contract."""
    required = {
        "schema_version", "build_id", "index_path", "corpus_fingerprint",
        "lexical_index_sha256", "model_manifest_sha256", "chunk_count",
        "vector_dimension", "distance_metric", "file_set_sha256",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError(f"index manifest missing fields: {missing}")
    if payload["schema_version"] != INDEX_MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported index manifest schema_version")
    for field in ("chunk_count", "vector_dimension"):
        value = payload[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"{field} must be a positive integer")
    metric = payload["distance_metric"]
    if metric not in {"cosine", "l2", "inner_product"}:
        raise ValueError("distance_metric is unsupported")
    for field in (
        "corpus_fingerprint", "lexical_index_sha256", "model_manifest_sha256",
        "file_set_sha256",
    ):
        _require_sha256(payload[field], field)
    normalized = dict(payload)
    normalized["index_path"] = _require_ascii_absolute_path(payload["index_path"], "index_path")
    canonical_json_bytes(normalized)
    return normalized


def evaluate_semantic_continuation(baseline: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the predeclared lexical-gap gate without semantic results."""
    target = baseline.get("target_styles")
    if not isinstance(target, Mapping):
        raise ValueError("baseline target_styles metrics are missing")
    query_count = int(target.get("query_count", 0))
    recall_at_5 = float(target.get("recall_at_5", math.nan))
    misses_at_5 = int(target.get("misses_at_5", 0))
    finite = math.isfinite(recall_at_5)
    passed = (
        finite
        and query_count >= SEMANTIC_CONTINUATION_GATE["minimum_target_queries"]
        and recall_at_5 <= SEMANTIC_CONTINUATION_GATE["maximum_lexical_recall_at_5"]
        and misses_at_5 >= SEMANTIC_CONTINUATION_GATE["minimum_target_misses_at_5"]
    )
    return {
        "continue_to_semantic_worker": passed,
        "measured": {
            "query_count": query_count,
            "recall_at_5": recall_at_5,
            "misses_at_5": misses_at_5,
        },
        "thresholds": SEMANTIC_CONTINUATION_GATE,
        "reason": (
            "material_lexical_gap_demonstrated" if passed
            else "material_lexical_gap_not_demonstrated"
        ),
    }


__all__ = [
    "CONTRACT_VERSION",
    "DEFAULT_DEPLOYMENT_ROOT",
    "SEMANTIC_CONTINUATION_GATE",
    "SEMANTIC_PROMOTION_GATE",
    "INDEX_MANIFEST_SCHEMA_VERSION",
    "MODEL_MANIFEST_SCHEMA_VERSION",
    "PUBLIC_LIMITS",
    "THREAT_MODEL",
    "WORKER_PROTOCOL_SCHEMA_VERSION",
    "canonical_json_bytes",
    "evaluate_semantic_continuation",
    "object_sha256",
    "validate_evaluation_set",
    "validate_index_manifest",
    "validate_model_manifest",
]
