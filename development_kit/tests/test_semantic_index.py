"""semantic index gates for immutable offline model and semantic-index publication."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import numpy as np
import pytest

from src.knowledge.lexical_manual import build_index_from_records
from src.knowledge import semantic_index as index_module
from src.knowledge.semantic_index import (
    build_index,
    chunk_page,
    index_file_snapshot,
    pin_model_snapshot,
    read_current,
    switch_current,
    validate_index_against_lexical,
    validate_index_directory,
    validate_pinned_model,
)


class FakeEncoder:
    dimension = 4

    def encode(self, texts):
        rows = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            row = np.asarray([digest[index] + 1 for index in range(self.dimension)], dtype=np.float32)
            rows.append(row / np.linalg.norm(row))
        return np.stack(rows)


@pytest.fixture
def semantic_index_root():
    root = Path("D:/comsol_semantic_index_test") / uuid.uuid4().hex
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def semantic_index_assets(semantic_index_root):
    source = semantic_index_root / "source-model"
    source.mkdir()
    (source / "config.json").write_text('{"dimension":4}\n', encoding="utf-8")
    (source / "model.bin").write_bytes(bytes(range(64)))
    model = semantic_index_root / "deployment" / "models" / "fake" / "revision-1"
    pin_model_snapshot(
        source,
        model,
        model_id="fake/offline-model",
        revision="revision-1",
        dimension=4,
        license_name="test-only",
    )
    lexical = semantic_index_root / "lexical" / "manuals.sqlite3"
    corpus = "8" * 64
    build_index_from_records(
        [
            {
                "source": "COMSOL_Multiphysics/ReferenceManual.pdf",
                "module": "COMSOL_Multiphysics",
                "page": 1,
                "heading": "Copy Face",
                "text": "CopyFace maps source meshes to destination faces. " * 40,
            },
            {
                "source": "Wave_Optics_Module/WaveOpticsUsersGuide.pdf",
                "module": "Wave_Optics_Module",
                "page": 2,
                "heading": "Periodic ports",
                "text": "Periodic ports require homogeneous adjacent media and Floquet phase settings.",
            },
        ],
        lexical,
        corpus_fingerprint=corpus,
    )
    return {
        "root": semantic_index_root / "deployment",
        "model": model,
        "lexical": lexical,
        "corpus": corpus,
    }


def _build(assets, build_id):
    return build_index(
        deployment_root=assets["root"],
        lexical_index=assets["lexical"],
        model_path=assets["model"],
        encoder=FakeEncoder(),
        build_id=build_id,
        maximum_characters=240,
        overlap=20,
        batch_size=3,
        expected_corpus_fingerprint=assets["corpus"],
    )


def _rewrite_data_identity(index: Path, manifest: dict) -> None:
    files = [
        index_module._file_record(index / name, index)
        for name in ("chunks.jsonl", "embeddings.npy")
    ]
    manifest["files"] = files
    manifest["file_set_sha256"] = index_module._file_set_sha256(files)
    index_module._atomic_write_json(index / "manifest.json", manifest)


def test_chunking_is_deterministic_bounded_and_page_local():
    text = "First sentence. " * 100
    first = chunk_page(text, maximum_characters=240, overlap=20)
    second = chunk_page(text, maximum_characters=240, overlap=20)

    assert first == second
    assert len(first) > 1
    assert all(1 <= len(item) <= 240 for item in first)
    assert all("\n" not in item for item in first)


def test_model_pin_is_ascii_immutable_and_revision_bound(semantic_index_assets):
    model = validate_pinned_model(semantic_index_assets["model"])
    assert model["revision"] == "revision-1"
    assert model["dimension"] == 4
    assert model["license"] == "test-only"
    assert Path(model["model_path"]).is_absolute()
    assert model["model_path"].isascii()

    (semantic_index_assets["model"] / "model.bin").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="identity mismatch"):
        validate_pinned_model(semantic_index_assets["model"])


def test_build_validates_then_atomically_publishes_current(semantic_index_assets):
    result = _build(semantic_index_assets, "build-001")
    current = read_current(semantic_index_assets["root"])
    index = Path(current["pointer"]["index_path"])
    before = index_file_snapshot(index)
    repeated = read_current(semantic_index_assets["root"])
    after = index_file_snapshot(index)

    assert result["success"] is True
    assert result["lexical_unchanged"] is True
    assert current["pointer"]["build_id"] == "build-001"
    assert current["index"]["manifest"]["chunk_count"] > 2
    assert current["index"]["manifest"]["vector_dimension"] == 4
    assert current["index"]["manifest"]["chunking"]["page_crossing"] is False
    assert repeated["index"]["manifest_sha256"] == current["index"]["manifest_sha256"]
    citations = validate_index_against_lexical(index, semantic_index_assets["lexical"])
    assert citations["semantic_citation_count"] == 2
    assert before == after
    assert not list(semantic_index_assets["root"].rglob("*.tmp"))


@pytest.mark.parametrize("fault", ["after_chunks", "after_embeddings", "before_publish"])
def test_interrupted_build_never_changes_active_pointer(semantic_index_assets, fault):
    _build(semantic_index_assets, "accepted")
    pointer = semantic_index_assets["root"] / "current.json"
    before = pointer.read_bytes()

    with pytest.raises(RuntimeError, match="injected interruption"):
        build_index(
            deployment_root=semantic_index_assets["root"],
            lexical_index=semantic_index_assets["lexical"],
            model_path=semantic_index_assets["model"],
            encoder=FakeEncoder(),
            build_id=f"failed-{fault.replace('_', '-')}",
            maximum_characters=240,
            overlap=20,
            batch_size=3,
            fault_injection=fault,
        )

    assert pointer.read_bytes() == before
    assert read_current(semantic_index_assets["root"])["pointer"]["build_id"] == "accepted"
    assert list(semantic_index_assets["root"].rglob("*.building"))


def test_validation_rejects_corrupt_manifest_duplicate_ids_and_partial_vectors(semantic_index_assets):
    _build(semantic_index_assets, "good")
    good = Path(read_current(semantic_index_assets["root"])["pointer"]["index_path"])

    corrupt = good.parent / "corrupt"
    shutil.copytree(good, corrupt)
    (corrupt / "manifest.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        validate_index_directory(corrupt)

    duplicate = good.parent / "duplicate"
    shutil.copytree(good, duplicate)
    manifest = json.loads((duplicate / "manifest.json").read_text(encoding="utf-8"))
    manifest["index_path"] = str(duplicate)
    lines = (duplicate / "chunks.jsonl").read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    second["id"] = first["id"]
    lines[1] = json.dumps(second, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    (duplicate / "chunks.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _rewrite_data_identity(duplicate, manifest)
    with pytest.raises(ValueError, match="duplicate"):
        validate_index_directory(duplicate)

    partial = good.parent / "partial"
    shutil.copytree(good, partial)
    manifest = json.loads((partial / "manifest.json").read_text(encoding="utf-8"))
    manifest["index_path"] = str(partial)
    matrix = np.load(partial / "embeddings.npy", allow_pickle=False)
    np.save(partial / "embeddings.npy", matrix[:-1], allow_pickle=False)
    _rewrite_data_identity(partial, manifest)
    with pytest.raises(ValueError, match="partial or mismatched"):
        validate_index_directory(partial)

    missing_citation = good.parent / "missing-citation"
    shutil.copytree(good, missing_citation)
    manifest = json.loads((missing_citation / "manifest.json").read_text(encoding="utf-8"))
    manifest["index_path"] = str(missing_citation)
    lines = (missing_citation / "chunks.jsonl").read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["source"] = "missing/NotInCorpus.pdf"
    record["id"] = index_module._chunk_id(
        record["corpus_fingerprint"], record["source"], record["page"],
        record["ordinal"], record["text_sha256"],
    )
    lines[0] = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    (missing_citation / "chunks.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _rewrite_data_identity(missing_citation, manifest)
    assert validate_index_directory(missing_citation)["validated"] is True
    with pytest.raises(ValueError, match="absent from lexical corpus"):
        validate_index_against_lexical(missing_citation, semantic_index_assets["lexical"])


def test_mismatch_non_ascii_and_pointer_rollback_gates(semantic_index_assets):
    with pytest.raises(ValueError, match="corpus fingerprint"):
        build_index(
            deployment_root=semantic_index_assets["root"], lexical_index=semantic_index_assets["lexical"],
            model_path=semantic_index_assets["model"], encoder=FakeEncoder(), build_id="wrong-corpus",
            expected_corpus_fingerprint="0" * 64,
        )

    wrong_encoder = FakeEncoder()
    wrong_encoder.dimension = 5
    with pytest.raises(ValueError, match="dimension"):
        build_index(
            deployment_root=semantic_index_assets["root"], lexical_index=semantic_index_assets["lexical"],
            model_path=semantic_index_assets["model"], encoder=wrong_encoder, build_id="wrong-model",
        )

    with pytest.raises(ValueError, match="ASCII"):
        build_index(
            deployment_root="C:/Users/陆星/semantic", lexical_index=semantic_index_assets["lexical"],
            model_path=semantic_index_assets["model"], encoder=FakeEncoder(), build_id="unicode-root",
        )

    _build(semantic_index_assets, "rollback-a")
    first = Path(read_current(semantic_index_assets["root"])["pointer"]["index_path"])
    _build(semantic_index_assets, "rollback-b")
    assert read_current(semantic_index_assets["root"])["pointer"]["build_id"] == "rollback-b"
    switch_current(semantic_index_assets["root"], first)
    assert read_current(semantic_index_assets["root"])["pointer"]["build_id"] == "rollback-a"


def test_semantic_index_import_does_not_load_ml_or_spawn(semantic_index_root):
    code = """
import json, sys
import src.knowledge.semantic_index
for name in ('numpy', 'chromadb', 'torch', 'sentence_transformers', 'mph', 'psutil'):
    assert name not in sys.modules, name
print(json.dumps({'ok': True}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["ok"] is True
