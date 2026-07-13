"""H4d read-only vector retrieval and deterministic BM25 fusion."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping, Sequence

from .lexical_manual import run_bounded
from .semantic_contracts import PUBLIC_LIMITS, object_sha256
from .semantic_index import (
    SentenceTransformerEncoder,
    index_file_snapshot,
    read_current,
    validate_pinned_model,
)


RANKER_VERSION = "1"
RANKER_CONFIG = {
    "version": RANKER_VERSION,
    "fusion": "reciprocal_rank_fusion",
    "rrf_constant": 60,
    "lexical_weight": 1.0,
    "vector_weight": 1.0,
    "exact_technical_token_bonus": 0.04,
    "maximum_technical_token_bonus": 0.20,
    "quoted_phrase_bonus": 0.10,
    "minimum_vector_similarity_without_lexical": 0.30,
    "lexical_candidate_count": 20,
    "vector_candidate_count": 50,
}
RANKER_SHA256 = object_sha256(RANKER_CONFIG)
TECHNICAL_TOKEN_PATTERN = re.compile(r"\b[A-Za-z_][A-Za-z0-9_.]*\b")
QUOTED_PHRASE_PATTERN = re.compile(r'["“”]([^"“”]{2,120})["“”]')


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _technical_tokens(query: str) -> list[str]:
    tokens = []
    for token in TECHNICAL_TOKEN_PATTERN.findall(query):
        if "_" in token or "." in token or any(char.isupper() for char in token[1:]) or any(char.isdigit() for char in token):
            if token not in tokens:
                tokens.append(token)
    return tokens


def _quoted_phrases(query: str) -> list[str]:
    return [match.strip() for match in QUOTED_PHRASE_PATTERN.findall(query) if match.strip()]


def _filters_match(record: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    if filters.get("module") and record.get("module") != filters["module"]:
        return False
    if filters.get("source") and record.get("source") != str(filters["source"]).replace("\\", "/"):
        return False
    page = int(record["page"])
    if filters.get("page_start") is not None and page < int(filters["page_start"]):
        return False
    if filters.get("page_end") is not None and page > int(filters["page_end"]):
        return False
    return True


def _validate_filters(filters: Mapping[str, Any] | None) -> dict[str, Any]:
    value = dict(filters or {})
    unknown = sorted(set(value) - {"module", "source", "page_start", "page_end"})
    if unknown:
        raise ValueError(f"unsupported semantic filters: {unknown}")
    for field in ("module", "source"):
        if value.get(field) is not None and (not isinstance(value[field], str) or not value[field].strip()):
            raise ValueError(f"{field} filter must be a nonempty string")
        if isinstance(value.get(field), str):
            value[field] = value[field].strip()
    for field in ("page_start", "page_end"):
        if value.get(field) is not None and (
            not isinstance(value[field], int) or isinstance(value[field], bool) or value[field] < 1
        ):
            raise ValueError(f"{field} must be a positive integer")
    if value.get("page_start") and value.get("page_end") and value["page_start"] > value["page_end"]:
        raise ValueError("page_start cannot exceed page_end")
    return value


class HybridRetriever:
    """Load one immutable index/model and serve deterministic bounded queries."""

    def __init__(
        self,
        *,
        deployment_root: str | Path,
        lexical_index: str | Path,
        model_path: str | Path,
        encoder_factory: Callable[[str | Path, int], Any] | None = None,
    ):
        import numpy as np

        self.deployment_root = Path(deployment_root).resolve()
        self.lexical_index = Path(lexical_index).resolve()
        self.model_path = Path(model_path).resolve()
        current = read_current(self.deployment_root)
        self.pointer = current["pointer"]
        self.index = current["index"]
        self.index_path = Path(self.pointer["index_path"])
        self.manifest = self.index["manifest"]
        self._pointer_sha256 = _sha256_file(self.deployment_root / "current.json")
        model = validate_pinned_model(self.model_path)
        if model["model_sha256"] != self.manifest["model_fingerprint"]:
            raise ValueError("pinned model fingerprint does not match the active index")
        if model["manifest_sha256"] != self.manifest["model_manifest_sha256"]:
            raise ValueError("pinned model manifest does not match the active index")
        if self.manifest["lexical_index_sha256"] != _sha256_file(self.lexical_index):
            raise ValueError("lexical index does not match the active semantic index")
        self._immutable_snapshot = index_file_snapshot(self.index_path)
        self.chunks: list[dict[str, Any]] = []
        with (self.index_path / "chunks.jsonl").open("r", encoding="utf-8") as handle:
            for line in handle:
                self.chunks.append(json.loads(line))
        self.embeddings = np.load(self.index_path / "embeddings.npy", mmap_mode="r", allow_pickle=False)
        if len(self.chunks) != self.embeddings.shape[0]:
            raise ValueError("chunk/vector count mismatch at retriever load")
        factory = encoder_factory or (lambda path, dimension: SentenceTransformerEncoder(path, dimension=dimension))
        self.encoder = factory(self.model_path, int(self.manifest["vector_dimension"]))
        if int(self.encoder.dimension) != int(self.manifest["vector_dimension"]):
            raise ValueError("query encoder dimension mismatch")
        self.load_count = 1
        self.query_count = 0
        self.loaded_at_epoch = time.time()
        self.last_error: str | None = None

    def _check_identity(self) -> None:
        if _sha256_file(self.deployment_root / "current.json") != self._pointer_sha256:
            raise RuntimeError("active semantic index pointer changed; restart worker before querying")
        current_snapshot = [
            {**item, "mtime_ns": item["mtime_ns"]}
            for item in index_file_snapshot(self.index_path)
        ]
        if current_snapshot != self._immutable_snapshot:
            raise RuntimeError("active semantic index files changed after worker load")

    def status(self) -> dict[str, Any]:
        return {
            "backend": "hybrid",
            "load_count": self.load_count,
            "query_count": self.query_count,
            "loaded_at_epoch": self.loaded_at_epoch,
            "model_id": self.manifest["model_id"],
            "model_revision": self.manifest["model_revision"],
            "model_fingerprint": self.manifest["model_fingerprint"],
            "index_build_id": self.manifest["build_id"],
            "index_manifest_sha256": self.index["manifest_sha256"],
            "corpus_fingerprint": self.manifest["corpus_fingerprint"],
            "chunk_count": self.manifest["chunk_count"],
            "ranker_version": RANKER_VERSION,
            "ranker_sha256": RANKER_SHA256,
            "device": "cpu",
            "last_error": self.last_error,
        }

    def _vector_candidates(self, query: str, filters: Mapping[str, Any], count: int) -> list[dict[str, Any]]:
        import numpy as np

        encoded = np.asarray(self.encoder.encode([query]), dtype=np.float32)
        if encoded.shape != (1, int(self.manifest["vector_dimension"])) or not np.isfinite(encoded).all():
            raise ValueError("query encoder returned malformed or non-finite values")
        norm = float(np.linalg.norm(encoded[0]))
        if not math.isfinite(norm) or norm <= 0:
            raise ValueError("query encoder returned a zero or non-finite vector")
        vector = encoded[0] / norm
        scores = np.asarray(self.embeddings @ vector, dtype=np.float32)
        eligible = np.fromiter(
            (_filters_match(record, filters) for record in self.chunks),
            dtype=np.bool_, count=len(self.chunks),
        )
        indices = np.flatnonzero(eligible)
        if not len(indices):
            return []
        take = min(int(count), len(indices))
        eligible_scores = scores[indices]
        selected_local = np.argpartition(-eligible_scores, take - 1)[:take]
        selected = indices[selected_local]
        ordered = sorted(
            (int(index) for index in selected),
            key=lambda index: (-float(scores[index]), self.chunks[index]["id"]),
        )
        return [
            {
                "rank": rank,
                "similarity": float(scores[index]),
                "distance": float(1.0 - scores[index]),
                "chunk": self.chunks[index],
            }
            for rank, index in enumerate(ordered, 1)
        ]

    def _lexical_candidates(self, query: str, filters: Mapping[str, Any], count: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        arguments = {
            "query": query,
            "module": filters.get("module"),
            "source": filters.get("source"),
            "page_start": filters.get("page_start"),
            "page_end": filters.get("page_end"),
            "limit": count,
            "index_path": str(self.lexical_index),
            "mode": "auto",
        }
        result = run_bounded("search", arguments, timeout=2.0)
        if not result.get("success"):
            raise RuntimeError(f"bounded lexical retrieval failed: {result}")
        return list(result["results"]), {
            "strategy": result["strategy"],
            "relaxed": result["relaxed"],
            "fts_query": result["fts_query"],
        }

    def query(
        self,
        query: str,
        *,
        limit: int = 5,
        filters: Mapping[str, Any] | None = None,
        retrieval_mode: str = "hybrid",
    ) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip() or len(query) > PUBLIC_LIMITS["maximum_query_characters"]:
            raise ValueError("query violates public limits")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= PUBLIC_LIMITS["maximum_results"]:
            raise ValueError("limit violates public limits")
        if retrieval_mode not in {"hybrid", "vector", "lexical"}:
            raise ValueError("retrieval_mode must be hybrid, vector, or lexical")
        normalized_filters = _validate_filters(filters)
        self._check_identity()
        started = time.perf_counter()
        vector = [] if retrieval_mode == "lexical" else self._vector_candidates(
            query.strip(), normalized_filters, RANKER_CONFIG["vector_candidate_count"]
        )
        lexical: list[dict[str, Any]] = []
        lexical_info: dict[str, Any] | None = None
        if retrieval_mode != "vector":
            lexical, lexical_info = self._lexical_candidates(
                query.strip(), normalized_filters, RANKER_CONFIG["lexical_candidate_count"]
            )
        results = fuse_candidates(
            query.strip(), lexical, vector, limit=limit, retrieval_mode=retrieval_mode,
            provenance={
                "corpus_fingerprint": self.manifest["corpus_fingerprint"],
                "index_build_id": self.manifest["build_id"],
                "index_manifest_sha256": self.index["manifest_sha256"],
                "model_id": self.manifest["model_id"],
                "model_revision": self.manifest["model_revision"],
                "model_fingerprint": self.manifest["model_fingerprint"],
            },
        )
        self.query_count += 1
        return {
            "query": query.strip(),
            "retrieval_mode": retrieval_mode,
            "filters": normalized_filters,
            "count": len(results),
            "results": results,
            "lexical": lexical_info,
            "ranker": {"version": RANKER_VERSION, "sha256": RANKER_SHA256},
            "elapsed_seconds": time.perf_counter() - started,
            "load_count": self.load_count,
            "query_count": self.query_count,
        }


def fuse_candidates(
    query: str,
    lexical: Sequence[Mapping[str, Any]],
    vector: Sequence[Mapping[str, Any]],
    *,
    limit: int,
    retrieval_mode: str,
    provenance: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Fuse page candidates deterministically while protecting exact symbols."""
    pages: dict[tuple[str, int], dict[str, Any]] = {}
    k = int(RANKER_CONFIG["rrf_constant"])
    for rank, row in enumerate(lexical, 1):
        lexical_score = float(row["rank"])
        if not math.isfinite(lexical_score):
            raise ValueError("lexical candidate contains a non-finite score")
        key = (str(row["source"]), int(row["page"]))
        page = pages.setdefault(key, {"source": key[0], "page": key[1]})
        page.update({
            "module": row.get("module"),
            "heading": row.get("heading"),
            "snippet": str(row.get("snippet") or "")[: PUBLIC_LIMITS["maximum_snippet_characters"]],
            "lexical_rank": rank,
            "lexical_score": lexical_score,
            "lexical_coverage": float(row.get("coverage", 0.0)),
        })
    for item in vector:
        similarity = float(item["similarity"])
        distance = float(item["distance"])
        vector_rank_value = int(item["rank"])
        if vector_rank_value < 1 or not math.isfinite(similarity) or not math.isfinite(distance):
            raise ValueError("vector candidate contains an invalid rank or non-finite score")
        chunk = item["chunk"]
        key = (str(chunk["source"]), int(chunk["page"]))
        page = pages.setdefault(key, {
            "source": key[0], "page": key[1], "module": chunk.get("module"),
            "heading": chunk.get("heading"),
        })
        if "vector_rank" not in page:
            page.update({
                "vector_rank": vector_rank_value,
                "vector_similarity": similarity,
                "vector_distance": distance,
                "vector_chunk_id": chunk["id"],
                "vector_chunk_ordinal": int(chunk["ordinal"]),
            })
            if not page.get("snippet"):
                page["snippet"] = str(chunk["text"])[: PUBLIC_LIMITS["maximum_snippet_characters"]]

    technical = _technical_tokens(query)
    phrases = _quoted_phrases(query)
    output = []
    for page in pages.values():
        lexical_rank = page.get("lexical_rank")
        vector_rank = page.get("vector_rank")
        score = 0.0
        if lexical_rank is not None:
            score += float(RANKER_CONFIG["lexical_weight"]) / (k + int(lexical_rank))
        if vector_rank is not None:
            score += float(RANKER_CONFIG["vector_weight"]) / (k + int(vector_rank))
        haystack = "\n".join(str(page.get(field) or "") for field in ("heading", "snippet"))
        matched_tokens = [token for token in technical if token in haystack]
        token_bonus = min(
            len(matched_tokens) * float(RANKER_CONFIG["exact_technical_token_bonus"]),
            float(RANKER_CONFIG["maximum_technical_token_bonus"]),
        )
        matched_phrases = [phrase for phrase in phrases if phrase.casefold() in haystack.casefold()]
        phrase_bonus = float(RANKER_CONFIG["quoted_phrase_bonus"]) if matched_phrases else 0.0
        exact_tier = 2 if technical and len(matched_tokens) == len(technical) else (1 if matched_tokens else 0)
        page.update({
            "fused_score": score + token_bonus + phrase_bonus,
            "matched_technical_tokens": matched_tokens,
            "matched_quoted_phrases": matched_phrases,
            "exact_match_tier": exact_tier,
            "citation_integrity": "validated",
            **provenance,
            "ranker_version": RANKER_VERSION,
            "ranker_sha256": RANKER_SHA256,
        })
        output.append(page)

    if retrieval_mode in {"hybrid", "vector"} and not lexical and vector:
        maximum = max(float(item["similarity"]) for item in vector)
        if maximum < float(RANKER_CONFIG["minimum_vector_similarity_without_lexical"]):
            return []
    output.sort(key=lambda row: (
        -int(row["exact_match_tier"]),
        -float(row["fused_score"]),
        str(row["source"]),
        int(row["page"]),
    ))
    return output[:limit]


__all__ = [
    "HybridRetriever", "RANKER_CONFIG", "RANKER_SHA256", "RANKER_VERSION",
    "fuse_candidates",
]
