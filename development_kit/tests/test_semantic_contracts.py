"""semantic benchmark gates for benchmark, manifests, limits, and import safety."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import pytest

from src.knowledge.lexical_manual import build_index_from_records
from development_kit.benchmarks.semantic_benchmark import evaluate_lexical_baseline
from src.knowledge.semantic_contracts import (
    SEMANTIC_CONTINUATION_GATE,
    PUBLIC_LIMITS,
    THREAT_MODEL,
    canonical_json_bytes,
    evaluate_semantic_continuation,
    validate_evaluation_set,
    validate_index_manifest,
    validate_model_manifest,
)


ROOT = Path(__file__).parents[2]
EVALUATION_PATH = ROOT / "development_kit" / "tests" / "fixtures" / "semantic_retrieval_evaluation.json"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def test_frozen_evaluation_has_sixty_reviewed_queries_and_declared_slices():
    payload = json.loads(EVALUATION_PATH.read_text(encoding="utf-8"))
    result = validate_evaluation_set(payload)

    assert len(result["queries"]) == 66
    assert len({item["id"] for item in result["queries"]}) == 66
    assert {item["category"] for item in result["queries"]} == {
        "exact_clientapi", "wave_optics", "conventional_fem", "troubleshooting", "negative"
    }
    assert {item["style"] for item in result["queries"]} == {
        "exact", "paraphrase", "multi_concept", "zh_cross_language"
    }
    assert all(item["judge_note"] for item in result["queries"])
    assert sum(item["expected_no_relevant"] for item in result["queries"]) == 6


def test_model_and_index_manifests_require_ascii_absolute_identity_paths():
    model = validate_model_manifest({
        "schema_version": "1",
        "model_id": "all-MiniLM-L6-v2",
        "revision": "local-test",
        "model_path": "D:/comsol_semantic/models/minilm/local-test",
        "model_sha256": SHA_A,
        "dimension": 384,
        "license": "Apache-2.0",
    })
    index = validate_index_manifest({
        "schema_version": "1",
        "build_id": "fixture-v1",
        "index_path": "D:/comsol_semantic/indexes/corpus/model/fixture-v1",
        "corpus_fingerprint": SHA_A,
        "lexical_index_sha256": SHA_B,
        "model_manifest_sha256": SHA_C,
        "chunk_count": 100,
        "vector_dimension": 384,
        "distance_metric": "cosine",
        "file_set_sha256": SHA_A,
    })

    assert model["dimension"] == index["vector_dimension"] == 384
    with pytest.raises(ValueError, match="ASCII"):
        validate_model_manifest({**model, "model_path": "C:/Users/陆星/model"})
    with pytest.raises(ValueError, match="positive integer"):
        validate_index_manifest({**index, "chunk_count": 0})


def test_contract_json_rejects_nonfinite_values_and_limits_are_bounded():
    with pytest.raises(ValueError):
        canonical_json_bytes({"distance": float("nan")})

    assert PUBLIC_LIMITS["query_deadline_seconds"] == 5.0
    assert PUBLIC_LIMITS["maximum_results"] == 10
    assert PUBLIC_LIMITS["maximum_response_bytes"] == 65_536
    assert "no_solver_lease_or_COMSOL_start" in THREAT_MODEL["containment"]


def test_semantic_continuation_gate_requires_a_material_target_slice_gap():
    blocked = evaluate_semantic_continuation({
        "target_styles": {"query_count": 30, "recall_at_5": 0.9, "misses_at_5": 3}
    })
    continuing = evaluate_semantic_continuation({
        "target_styles": {
            "query_count": SEMANTIC_CONTINUATION_GATE["minimum_target_queries"],
            "recall_at_5": SEMANTIC_CONTINUATION_GATE["maximum_lexical_recall_at_5"],
            "misses_at_5": SEMANTIC_CONTINUATION_GATE["minimum_target_misses_at_5"],
        }
    })

    assert blocked["continue_to_semantic_worker"] is False
    assert continuing["continue_to_semantic_worker"] is True


def test_lexical_baseline_computes_rank_metrics_without_semantic_dependencies():
    root = Path("D:/comsol_semantic_contract_test") / uuid.uuid4().hex
    index = root / "manuals.sqlite3"
    corpus = "d" * 64
    source = "COMSOL_Multiphysics/COMSOL_ReferenceManual.pdf"
    try:
        build_index_from_records(
            [{
                "source": source,
                "module": "COMSOL_Multiphysics",
                "page": 10,
                "heading": "Copy Face",
                "text": "CopyFace copies a mesh from source to destination faces.",
            }],
            index,
            corpus_fingerprint=corpus,
        )
        evaluation = {
            "schema_version": "1",
            "name": "unit",
            "frozen_at": "2026-07-13",
            "corpus_fingerprint": corpus,
            "queries": [
                {
                    "id": f"q{number:02d}",
                    "query": "CopyFace source destination",
                    "category": "exact_clientapi",
                    "style": "exact" if number < 30 else "paraphrase",
                    "relevant": [{"source": source, "page": 10}],
                    "judge_note": "Synthetic exact citation for rank-metric testing.",
                }
                for number in range(60)
            ],
        }
        result = evaluate_lexical_baseline(evaluation, index_path=index)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    assert result["query_count"] == 60
    assert result["summary"]["overall"]["recall_at_5"] == 1.0
    assert result["summary"]["overall"]["mrr_at_10"] == 1.0
    assert result["continuation_gate"]["continue_to_semantic_worker"] is False


def test_semantic_contract_imports_do_not_load_heavy_semantic_or_comsol_modules():
    code = """
import json, sys
import src.knowledge.semantic_contracts
import development_kit.benchmarks.semantic_benchmark
import psutil
for name in ('chromadb', 'torch', 'sentence_transformers', 'mph'):
    assert name not in sys.modules, name
assert psutil.Process().children(recursive=True) == []
print(json.dumps({'ok': True, 'children': 0}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["ok"] is True
    assert json.loads(completed.stdout)["children"] == 0
