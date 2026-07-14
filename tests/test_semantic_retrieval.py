"""H4d deterministic vector, filter, fusion, and cache-identity gates."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
import shutil
import uuid

import numpy as np
import pytest

from src.knowledge.lexical_manual import build_index_from_records
from src.knowledge.semantic_index import (
    build_index,
    index_file_snapshot,
    pin_model_snapshot,
    read_current,
    switch_current,
)
from src.knowledge.semantic_retrieval import (
    HybridRetriever,
    RANKER_SHA256,
    fuse_candidates,
)


class ControlledEncoder:
    dimension = 4

    def encode(self, texts):
        rows = []
        for text in texts:
            lowered = text.casefold()
            if "copyface" in lowered or "copy face" in lowered:
                row = [1.0, 0.0, 0.0, 0.0]
            elif "periodic" in lowered or "floquet" in lowered:
                row = [0.0, 1.0, 0.0, 0.0]
            elif "thermal" in lowered:
                row = [0.0, 0.0, 1.0, 0.0]
            else:
                row = [0.0, 0.0, 0.0, 1.0]
            rows.append(row)
        return np.asarray(rows, dtype=np.float32)


class NonFiniteEncoder(ControlledEncoder):
    def encode(self, texts):
        return np.full((len(texts), self.dimension), np.nan, dtype=np.float32)


@pytest.fixture
def retrieval_assets():
    root = Path("D:/comsol_semantic_h4d_test") / uuid.uuid4().hex
    source_model = root / "source-model"
    source_model.mkdir(parents=True)
    (source_model / "config.json").write_text("{}\n", encoding="utf-8")
    (source_model / "model.bin").write_bytes(b"deterministic-test-model")
    deployment = root / "deployment"
    model = deployment / "models" / "controlled" / "r1"
    pin_model_snapshot(
        source_model, model, model_id="test/controlled", revision="r1",
        dimension=4, license_name="test-only",
    )
    lexical = root / "lexical" / "manuals.sqlite3"
    corpus = "7" * 64
    build_index_from_records(
        [
            {
                "source": "COMSOL_Multiphysics/ReferenceManual.pdf",
                "module": "COMSOL_Multiphysics",
                "page": 10,
                "heading": "CopyFace API",
                "text": "CopyFace maps a source boundary mesh onto the destination boundary. " * 8,
            },
            {
                "source": "Wave_Optics_Module/WaveOpticsUsersGuide.pdf",
                "module": "Wave_Optics_Module",
                "page": 20,
                "heading": "Periodic ports",
                "text": "A periodic port uses Floquet phase and requires a homogeneous adjacent medium.",
            },
            {
                "source": "Heat_Transfer_Module/HeatTransferUsersGuide.pdf",
                "module": "Heat_Transfer_Module",
                "page": 30,
                "heading": "Thermal contact",
                "text": "Thermal contact resistance couples heat flux across an interface.",
            },
        ],
        lexical,
        corpus_fingerprint=corpus,
    )
    build_index(
        deployment_root=deployment,
        lexical_index=lexical,
        model_path=model,
        encoder=ControlledEncoder(),
        build_id="controlled-001",
        maximum_characters=220,
        overlap=20,
        batch_size=2,
    )
    assets = {"root": root, "deployment": deployment, "model": model, "lexical": lexical}
    try:
        yield assets
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _retriever(assets, encoder=ControlledEncoder):
    return HybridRetriever(
        deployment_root=assets["deployment"],
        lexical_index=assets["lexical"],
        model_path=assets["model"],
        encoder_factory=lambda _path, _dimension: encoder(),
        lexical_timeout_seconds=5.0,
    )


def test_hybrid_ranking_is_deterministic_provenance_complete_and_loads_once(retrieval_assets):
    retriever = _retriever(retrieval_assets)
    before = index_file_snapshot(Path(read_current(retrieval_assets["deployment"])["pointer"]["index_path"]))
    first = retriever.query("CopyFace source destination", limit=3)
    second = retriever.query("CopyFace source destination", limit=3)
    after = index_file_snapshot(Path(read_current(retrieval_assets["deployment"])["pointer"]["index_path"]))

    assert first["results"] == second["results"]
    assert first["results"][0]["source"].endswith("ReferenceManual.pdf")
    assert first["results"][0]["page"] == 10
    assert first["results"][0]["citation_integrity"] == "validated"
    assert first["results"][0]["ranker_sha256"] == RANKER_SHA256
    assert first["results"][0]["lexical_rank"] == 1
    assert first["results"][0]["vector_rank"] == 1
    assert first["load_count"] == second["load_count"] == 1
    assert second["query_count"] == 2
    assert retriever.status()["load_count"] == 1
    assert before == after


def test_module_source_page_filters_apply_to_both_candidate_paths(retrieval_assets):
    retriever = _retriever(retrieval_assets)
    module = retriever.query(
        "periodic Floquet port", limit=5,
        filters={"module": "Wave_Optics_Module"},
    )
    source_page = retriever.query(
        "periodic Floquet port", limit=5,
        filters={
            "source": "Wave_Optics_Module/WaveOpticsUsersGuide.pdf",
            "page_start": 20,
            "page_end": 20,
        },
    )

    assert module["count"] == 1
    assert module["results"][0]["module"] == "Wave_Optics_Module"
    assert source_page["count"] == 1
    assert source_page["results"][0]["page"] == 20
    with pytest.raises(ValueError, match="page_start"):
        retriever.query("periodic", filters={"page_start": 21, "page_end": 20})


def test_exact_api_symbol_tier_outranks_loose_semantic_page():
    lexical = [{
        "source": "api.pdf", "module": "api", "page": 5,
        "heading": "getUpDown", "snippet": "Call getUpDown to inspect domains.",
        "rank": -1.0, "coverage": 1.0,
    }]
    vector = [
        {
            "rank": 1, "similarity": 0.95, "distance": 0.05,
            "chunk": {
                "source": "related.pdf", "module": "related", "page": 1,
                "heading": "Domain topology", "text": "Inspect adjacent domains.",
                "id": "a" * 64, "ordinal": 0,
            },
        },
        {
            "rank": 20, "similarity": 0.50, "distance": 0.50,
            "chunk": {
                "source": "api.pdf", "module": "api", "page": 5,
                "heading": "getUpDown", "text": "Call getUpDown to inspect domains.",
                "id": "b" * 64, "ordinal": 0,
            },
        },
    ]
    provenance = {
        "corpus_fingerprint": "c" * 64, "index_build_id": "test",
        "index_manifest_sha256": "d" * 64, "model_id": "test",
        "model_revision": "r1", "model_fingerprint": "e" * 64,
    }

    result = fuse_candidates(
        "getUpDown adjacent domains", lexical, vector, limit=2,
        retrieval_mode="hybrid", provenance=provenance,
    )

    assert result[0]["source"] == "api.pdf"
    assert result[0]["exact_match_tier"] == 2
    assert result[0]["matched_technical_tokens"] == ["getUpDown"]
    assert result[1]["exact_match_tier"] == 0


def test_page_dedup_abstention_and_nonfinite_scores_are_bounded():
    base_chunk = {
        "source": "one.pdf", "module": "one", "page": 1,
        "heading": "One", "text": "semantically weak", "ordinal": 0,
    }
    vector = [
        {"rank": 1, "similarity": 0.20, "distance": 0.80, "chunk": {**base_chunk, "id": "1" * 64}},
        {"rank": 2, "similarity": 0.19, "distance": 0.81, "chunk": {**base_chunk, "id": "2" * 64, "ordinal": 1}},
    ]
    provenance = {
        "corpus_fingerprint": "c" * 64, "index_build_id": "test",
        "index_manifest_sha256": "d" * 64, "model_id": "test",
        "model_revision": "r1", "model_fingerprint": "e" * 64,
    }

    assert fuse_candidates(
        "out of corpus", [], vector, limit=5,
        retrieval_mode="vector", provenance=provenance,
    ) == []
    vector[0]["similarity"] = float("nan")
    with pytest.raises(ValueError, match="non-finite"):
        fuse_candidates(
            "query", [], vector, limit=5,
            retrieval_mode="vector", provenance=provenance,
        )


def test_pointer_change_requires_restart_and_nonfinite_query_vector_is_rejected(retrieval_assets):
    retriever = _retriever(retrieval_assets)
    current = read_current(retrieval_assets["deployment"])
    switch_current(retrieval_assets["deployment"], current["pointer"]["index_path"])
    with pytest.raises(RuntimeError, match="restart worker"):
        retriever.query("CopyFace")

    bad = _retriever(retrieval_assets, encoder=NonFiniteEncoder)
    with pytest.raises(ValueError, match="non-finite"):
        bad.query("CopyFace", retrieval_mode="vector")


def test_model_manifest_identity_mismatch_refuses_cache(retrieval_assets):
    model_file = retrieval_assets["model"] / "model.bin"
    model_file.write_bytes(b"changed-model")
    with pytest.raises(ValueError, match="identity mismatch"):
        _retriever(retrieval_assets)
