"""Offline, versioned semantic-index construction and validation.

The module imports only the Python standard library at import time. NumPy and
SentenceTransformers are loaded only inside offline build/validation calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time
from typing import Any, Iterable, Iterator, Mapping, Protocol, Sequence
import unicodedata
import uuid

from comsol_mcp.durable import atomic_write_json, sha256_file_bounded

from .semantic_contracts import (
    INDEX_MANIFEST_SCHEMA_VERSION,
    MODEL_MANIFEST_SCHEMA_VERSION,
    canonical_json_bytes,
    object_sha256,
    validate_index_manifest,
    validate_model_manifest,
)


CHUNK_SCHEMA_VERSION = "1"
CURRENT_POINTER_SCHEMA_VERSION = "1"
DEFAULT_CHUNK_CHARACTERS = 1_200
DEFAULT_CHUNK_OVERLAP = 120
DEFAULT_BATCH_SIZE = 64
BUILD_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
MAX_SEMANTIC_ARTIFACT_BYTES = 8 * 1024 * 1024 * 1024


class Encoder(Protocol):
    dimension: int

    def encode(self, texts: Sequence[str]) -> Any:
        """Return a finite float32-compatible matrix with one row per text."""


def _require_ascii_absolute(path: str | Path, label: str) -> Path:
    value = Path(path).expanduser().resolve()
    if not value.is_absolute():
        raise ValueError(f"{label} must be absolute")
    try:
        str(value).encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must contain ASCII characters only") from exc
    return value


def _sha256_file(path: Path) -> str:
    return sha256_file_bounded(
        path,
        max_bytes=MAX_SEMANTIC_ARTIFACT_BYTES,
    )["sha256"]


def _file_record(path: Path, root: Path) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    stat = path.stat()
    return {"path": relative, "size": stat.st_size, "sha256": _sha256_file(path)}


def _file_set_sha256(records: Sequence[Mapping[str, Any]]) -> str:
    normalized = [
        {"path": str(item["path"]), "size": int(item["size"]), "sha256": str(item["sha256"])}
        for item in sorted(records, key=lambda item: str(item["path"]))
    ]
    return object_sha256(normalized)


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    atomic_write_json(
        path,
        dict(value),
        replace_fn=os.replace,
        compact_temporary=True,
    )


def _normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text.replace("\x00", " "))
    return " ".join(normalized.split())


def chunk_page(text: str, *, maximum_characters: int = DEFAULT_CHUNK_CHARACTERS, overlap: int = DEFAULT_CHUNK_OVERLAP) -> list[str]:
    """Split normalized text deterministically without crossing a page."""
    if maximum_characters < 200:
        raise ValueError("maximum_characters must be at least 200")
    if overlap < 0 or overlap >= maximum_characters // 2:
        raise ValueError("overlap must be non-negative and smaller than half a chunk")
    value = _normalize_text(text)
    if not value:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(value):
        hard_end = min(len(value), start + maximum_characters)
        end = hard_end
        if hard_end < len(value):
            minimum_break = start + maximum_characters // 2
            candidates = [value.rfind(marker, minimum_break, hard_end) for marker in (". ", "; ", ": ", " ")]
            best = max(candidates)
            if best >= minimum_break:
                end = best + 1
        chunk = value[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(value):
            break
        next_start = max(start + 1, end - overlap)
        while next_start < end and next_start > start and not value[next_start - 1].isspace():
            next_start += 1
        start = min(next_start, end)
    return chunks


def _chunk_id(corpus_fingerprint: str, source: str, page: int, ordinal: int, text_sha256: str) -> str:
    identity = f"{CHUNK_SCHEMA_VERSION}\0{corpus_fingerprint}\0{source}\0{page}\0{ordinal}\0{text_sha256}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _lexical_identity(index_path: Path) -> dict[str, Any]:
    uri = index_path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=0.25) as connection:
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        actual_count = int(connection.execute("SELECT COUNT(*) FROM pages").fetchone()[0])
    declared_count = int(metadata.get("page_count", "0"))
    if actual_count != declared_count:
        raise ValueError("lexical page count does not match its metadata")
    return {
        "path": str(index_path),
        "sha256": _sha256_file(index_path),
        "size": index_path.stat().st_size,
        "mtime_ns": index_path.stat().st_mtime_ns,
        "schema_version": metadata.get("schema_version"),
        "corpus_fingerprint": metadata.get("corpus_fingerprint"),
        "page_count": actual_count,
    }


def _iter_pages(index_path: Path) -> Iterator[tuple[str, str, int, str, str]]:
    uri = index_path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=0.25) as connection:
        for source, module, page, heading, text in connection.execute(
            "SELECT source, module, page, heading, text FROM pages ORDER BY source, page"
        ):
            yield str(source).replace("\\", "/"), str(module), int(page), str(heading), str(text)


def pin_model_snapshot(
    source_snapshot: str | Path,
    destination: str | Path,
    *,
    model_id: str,
    revision: str,
    dimension: int,
    license_name: str,
) -> dict[str, Any]:
    """Copy a local model snapshot into an immutable ASCII-only directory."""
    source = Path(source_snapshot).expanduser().resolve()
    target = _require_ascii_absolute(destination, "model destination")
    if not source.is_dir():
        raise ValueError("source model snapshot does not exist")
    if target.exists():
        raise FileExistsError(f"model destination already exists: {target}")
    staging = target.with_name(target.name + f".building-{uuid.uuid4().hex}")
    staging.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(source, staging, symlinks=False)
        files = [_file_record(path, staging) for path in sorted(staging.rglob("*")) if path.is_file()]
        if not files:
            raise ValueError("model snapshot is empty")
        model_sha256 = _file_set_sha256(files)
        manifest = {
            "schema_version": MODEL_MANIFEST_SCHEMA_VERSION,
            "model_id": model_id,
            "revision": revision,
            "model_path": str(target),
            "model_sha256": model_sha256,
            "dimension": int(dimension),
            "license": license_name,
            "files": files,
            "file_set_sha256": model_sha256,
            "pinned_at_epoch": time.time(),
        }
        validate_model_manifest(manifest)
        _atomic_write_json(staging / "model_manifest.json", manifest)
        os.replace(staging, target)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def validate_pinned_model(model_path: str | Path) -> dict[str, Any]:
    root = _require_ascii_absolute(model_path, "model_path")
    manifest_path = root / "model_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_model_manifest(manifest)
    if Path(manifest["model_path"]).resolve() != root:
        raise ValueError("model manifest path does not match its directory")
    expected = manifest.get("files")
    if not isinstance(expected, list) or not expected:
        raise ValueError("model manifest file list is missing")
    actual = []
    for record in expected:
        path = root / str(record["path"])
        if not path.is_file():
            raise ValueError(f"model file is missing: {record['path']}")
        actual.append(_file_record(path, root))
    if actual != expected or _file_set_sha256(actual) != manifest["model_sha256"]:
        raise ValueError("model file identity mismatch")
    return {**manifest, "manifest_sha256": _sha256_file(manifest_path)}


class SentenceTransformerEncoder:
    """Offline-only encoder; importing this module does not import ML packages."""

    def __init__(self, model_path: str | Path, *, dimension: int):
        path = _require_ascii_absolute(model_path, "model_path")
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        import sentence_transformers
        import torch
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(str(path), local_files_only=True, device="cpu")
        self.dimension = int(dimension)
        self.device = str(self._model.device)
        self.dependency_versions = {
            "sentence_transformers": sentence_transformers.__version__,
            "torch": torch.__version__,
        }

    def encode(self, texts: Sequence[str]) -> Any:
        return self._model.encode(
            list(texts),
            batch_size=len(texts),
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )


def _write_chunks(index_path: Path, output: Path, lexical: Mapping[str, Any], *, maximum_characters: int, overlap: int) -> int:
    count = 0
    seen: set[str] = set()
    with output.open("wb") as handle:
        for source, module, page, heading, text in _iter_pages(index_path):
            for ordinal, chunk in enumerate(chunk_page(text, maximum_characters=maximum_characters, overlap=overlap)):
                text_sha256 = hashlib.sha256(chunk.encode("utf-8")).hexdigest()
                chunk_id = _chunk_id(str(lexical["corpus_fingerprint"]), source, page, ordinal, text_sha256)
                if chunk_id in seen:
                    raise ValueError(f"duplicate chunk ID: {chunk_id}")
                seen.add(chunk_id)
                record = {
                    "schema_version": CHUNK_SCHEMA_VERSION,
                    "id": chunk_id,
                    "corpus_fingerprint": lexical["corpus_fingerprint"],
                    "source": source,
                    "module": module,
                    "page": page,
                    "heading": _normalize_text(heading)[:300],
                    "ordinal": ordinal,
                    "text_sha256": text_sha256,
                    "text": chunk,
                }
                handle.write(canonical_json_bytes(record) + b"\n")
                count += 1
        handle.flush()
        os.fsync(handle.fileno())
    if count < 1:
        raise ValueError("lexical corpus produced no chunks")
    return count


def _iter_chunk_batches(path: Path, batch_size: int) -> Iterator[list[str]]:
    batch: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            batch.append(str(record["text"]))
            if len(batch) >= batch_size:
                yield batch
                batch = []
    if batch:
        yield batch


def build_index(
    *,
    deployment_root: str | Path,
    lexical_index: str | Path,
    model_path: str | Path,
    encoder: Encoder,
    build_id: str,
    maximum_characters: int = DEFAULT_CHUNK_CHARACTERS,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
    batch_size: int = DEFAULT_BATCH_SIZE,
    expected_corpus_fingerprint: str | None = None,
    fault_injection: str | None = None,
    progress: bool = False,
) -> dict[str, Any]:
    """Build, validate, publish, and atomically activate one immutable index."""
    import numpy as np

    root = _require_ascii_absolute(deployment_root, "deployment_root")
    lexical_path = _require_ascii_absolute(lexical_index, "lexical_index")
    model_root = _require_ascii_absolute(model_path, "model_path")
    if not BUILD_ID_PATTERN.fullmatch(build_id):
        raise ValueError("build_id is invalid")
    if batch_size < 1 or batch_size > 1024:
        raise ValueError("batch_size must be between 1 and 1024")
    lexical_before = _lexical_identity(lexical_path)
    if expected_corpus_fingerprint and lexical_before["corpus_fingerprint"] != expected_corpus_fingerprint:
        raise ValueError("lexical corpus fingerprint does not match the requested corpus")
    model = validate_pinned_model(model_root)
    if int(model["dimension"]) != int(encoder.dimension):
        raise ValueError("encoder dimension does not match the pinned model")
    corpus = str(lexical_before["corpus_fingerprint"])
    model_fingerprint = str(model["model_sha256"])
    family = root / "indexes" / corpus / model_fingerprint
    final = family / build_id
    staging = family / f"{build_id}.building"
    if final.exists() or staging.exists():
        raise FileExistsError("build_id already exists")
    staging.mkdir(parents=True, exist_ok=False)
    chunks_path = staging / "chunks.jsonl"
    embeddings_path = staging / "embeddings.npy"
    try:
        chunk_count = _write_chunks(
            lexical_path, chunks_path, lexical_before,
            maximum_characters=maximum_characters, overlap=overlap,
        )
        if progress:
            print(json.dumps({"phase": "chunks_complete", "chunk_count": chunk_count}), file=sys.stderr, flush=True)
        if fault_injection == "after_chunks":
            raise RuntimeError("injected interruption after chunks")
        matrix = np.lib.format.open_memmap(
            embeddings_path, mode="w+", dtype=np.float32,
            shape=(chunk_count, int(encoder.dimension)),
        )
        offset = 0
        batch_number = 0
        encode_started = time.perf_counter()
        for texts in _iter_chunk_batches(chunks_path, batch_size):
            values = np.asarray(encoder.encode(texts), dtype=np.float32)
            expected_shape = (len(texts), int(encoder.dimension))
            if values.shape != expected_shape:
                raise ValueError(f"encoder returned {values.shape}, expected {expected_shape}")
            if not np.isfinite(values).all():
                raise ValueError("encoder returned non-finite vectors")
            matrix[offset:offset + len(texts)] = values
            offset += len(texts)
            batch_number += 1
            if progress and (batch_number == 1 or batch_number % 25 == 0 or offset == chunk_count):
                elapsed = time.perf_counter() - encode_started
                print(json.dumps({
                    "phase": "encoding",
                    "completed": offset,
                    "total": chunk_count,
                    "chunks_per_second": offset / elapsed if elapsed else None,
                }), file=sys.stderr, flush=True)
        matrix.flush()
        del matrix
        if offset != chunk_count:
            raise ValueError("vector count does not match chunk count")
        if fault_injection == "after_embeddings":
            raise RuntimeError("injected interruption after embeddings")
        files = [_file_record(chunks_path, staging), _file_record(embeddings_path, staging)]
        manifest = {
            "schema_version": INDEX_MANIFEST_SCHEMA_VERSION,
            "build_id": build_id,
            "index_path": str(final),
            "corpus_fingerprint": corpus,
            "lexical_index_sha256": lexical_before["sha256"],
            "lexical_index_page_count": lexical_before["page_count"],
            "lexical_index_schema_version": lexical_before["schema_version"],
            "model_manifest_sha256": model["manifest_sha256"],
            "model_fingerprint": model_fingerprint,
            "model_id": model["model_id"],
            "model_revision": model["revision"],
            "chunk_count": chunk_count,
            "vector_dimension": int(encoder.dimension),
            "distance_metric": "cosine",
            "chunking": {
                "schema_version": CHUNK_SCHEMA_VERSION,
                "maximum_characters": maximum_characters,
                "overlap": overlap,
                "normalization": "NFKC+collapsed_whitespace",
                "page_crossing": False,
            },
            "files": files,
            "file_set_sha256": _file_set_sha256(files),
            "dependencies": {"python": sys.version.split()[0], "numpy": np.__version__},
            "build_command": "python -m comsol_mcp.knowledge.semantic_index build <deployment-config>",
            "built_at_epoch": time.time(),
        }
        validate_index_manifest(manifest)
        _atomic_write_json(staging / "manifest.json", manifest)
        validate_index_directory(staging, expected_final_path=final)
        validate_index_against_lexical(staging, lexical_path, expected_final_path=final)
        if fault_injection == "before_publish":
            raise RuntimeError("injected interruption before publish")
        lexical_after = _lexical_identity(lexical_path)
        if lexical_after != lexical_before:
            raise RuntimeError("lexical index identity changed during semantic build")
        os.replace(staging, final)
        validated = validate_index_directory(final)
        switch_current(root, final)
        return {"success": True, "index": validated, "lexical_unchanged": True}
    except Exception:
        # Preserve .building evidence for interrupted/corrupt builds. It is never
        # referenced by current.json and can be removed by an offline operator.
        raise


def validate_index_directory(index_path: str | Path, *, expected_final_path: str | Path | None = None) -> dict[str, Any]:
    import numpy as np

    root = _require_ascii_absolute(index_path, "index_path")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_index_manifest(manifest)
    declared_path = Path(manifest["index_path"]).resolve()
    expected = Path(expected_final_path).resolve() if expected_final_path is not None else root
    if declared_path != expected:
        raise ValueError("index manifest path identity mismatch")
    files = manifest.get("files")
    if not isinstance(files, list) or {item.get("path") for item in files} != {"chunks.jsonl", "embeddings.npy"}:
        raise ValueError("index manifest file list is invalid")
    actual = [_file_record(root / str(item["path"]), root) for item in files]
    if actual != files or _file_set_sha256(actual) != manifest["file_set_sha256"]:
        raise ValueError("index file identity mismatch")
    seen: set[str] = set()
    seen_ordinals: set[tuple[str, int, int]] = set()
    count = 0
    with (root / "chunks.jsonl").open("r", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            if record.get("schema_version") != CHUNK_SCHEMA_VERSION:
                raise ValueError("unsupported chunk schema")
            chunk_id = record.get("id")
            if not isinstance(chunk_id, str) or chunk_id in seen:
                raise ValueError("duplicate or invalid chunk ID")
            seen.add(chunk_id)
            if record.get("corpus_fingerprint") != manifest["corpus_fingerprint"]:
                raise ValueError("chunk corpus identity mismatch")
            source = record.get("source")
            page = record.get("page")
            ordinal = record.get("ordinal")
            if not isinstance(source, str) or not source.endswith(".pdf"):
                raise ValueError("chunk source citation is invalid")
            if not isinstance(page, int) or isinstance(page, bool) or page < 1:
                raise ValueError("chunk page citation is invalid")
            if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
                raise ValueError("chunk ordinal is invalid")
            ordinal_key = (source, page, ordinal)
            if ordinal_key in seen_ordinals:
                raise ValueError("duplicate source/page/ordinal identity")
            seen_ordinals.add(ordinal_key)
            text_sha256 = hashlib.sha256(str(record.get("text", "")).encode("utf-8")).hexdigest()
            if text_sha256 != record.get("text_sha256"):
                raise ValueError("chunk text hash mismatch")
            expected_id = _chunk_id(str(manifest["corpus_fingerprint"]), source, page, ordinal, text_sha256)
            if chunk_id != expected_id:
                raise ValueError("chunk stable ID mismatch")
            count += 1
    matrix = np.load(root / "embeddings.npy", mmap_mode="r", allow_pickle=False)
    expected_shape = (int(manifest["chunk_count"]), int(manifest["vector_dimension"]))
    if matrix.shape != expected_shape or count != expected_shape[0]:
        raise ValueError("partial or mismatched chunk/vector counts")
    for start in range(0, matrix.shape[0], 4096):
        if not np.isfinite(matrix[start:start + 4096]).all():
            raise ValueError("index contains non-finite vectors")
    return {
        "path": str(root),
        "manifest": manifest,
        "manifest_sha256": _sha256_file(manifest_path),
        "validated": True,
    }


def validate_index_against_lexical(
    index_path: str | Path,
    lexical_index: str | Path,
    *,
    expected_final_path: str | Path | None = None,
) -> dict[str, Any]:
    """Prove every semantic citation belongs to the immutable lexical corpus."""
    validated = validate_index_directory(index_path, expected_final_path=expected_final_path)
    lexical_path = _require_ascii_absolute(lexical_index, "lexical_index")
    lexical = _lexical_identity(lexical_path)
    manifest = validated["manifest"]
    expected_identity = {
        "sha256": manifest["lexical_index_sha256"],
        "corpus_fingerprint": manifest["corpus_fingerprint"],
        "page_count": int(manifest["lexical_index_page_count"]),
        "schema_version": manifest["lexical_index_schema_version"],
    }
    for field, expected in expected_identity.items():
        if lexical[field] != expected:
            raise ValueError(f"lexical {field} does not match the semantic manifest")
    uri = lexical_path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=0.25) as connection:
        citations = {
            (str(source).replace("\\", "/"), int(page))
            for source, page in connection.execute("SELECT source, page FROM pages")
        }
    semantic_citations: set[tuple[str, int]] = set()
    with (Path(index_path) / "chunks.jsonl").open("r", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            citation = (str(record["source"]), int(record["page"]))
            if citation not in citations:
                raise ValueError(f"semantic citation is absent from lexical corpus: {citation}")
            semantic_citations.add(citation)
    return {
        "validated": True,
        "semantic_chunk_count": int(manifest["chunk_count"]),
        "semantic_citation_count": len(semantic_citations),
        "lexical_page_count": lexical["page_count"],
        "lexical_unchanged": True,
    }


def switch_current(deployment_root: str | Path, index_path: str | Path) -> dict[str, Any]:
    root = _require_ascii_absolute(deployment_root, "deployment_root")
    index = _require_ascii_absolute(index_path, "index_path")
    validated = validate_index_directory(index)
    indexes_root = (root / "indexes").resolve()
    try:
        index.relative_to(indexes_root)
    except ValueError as exc:
        raise ValueError("index path is outside the deployment index root") from exc
    if index.name.endswith(".building"):
        raise ValueError("a staging index cannot become current")
    pointer = {
        "schema_version": CURRENT_POINTER_SCHEMA_VERSION,
        "index_path": str(index),
        "manifest_sha256": validated["manifest_sha256"],
        "corpus_fingerprint": validated["manifest"]["corpus_fingerprint"],
        "model_fingerprint": validated["manifest"]["model_fingerprint"],
        "build_id": validated["manifest"]["build_id"],
        "updated_at_epoch": time.time(),
    }
    _atomic_write_json(root / "current.json", pointer)
    return pointer


def read_current(deployment_root: str | Path) -> dict[str, Any]:
    root = _require_ascii_absolute(deployment_root, "deployment_root")
    pointer_path = root / "current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    if pointer.get("schema_version") != CURRENT_POINTER_SCHEMA_VERSION:
        raise ValueError("unsupported current pointer schema")
    validated = validate_index_directory(pointer["index_path"])
    if validated["manifest_sha256"] != pointer.get("manifest_sha256"):
        raise ValueError("current pointer manifest identity mismatch")
    manifest = validated["manifest"]
    for field in ("corpus_fingerprint", "model_fingerprint", "build_id"):
        if pointer.get(field) != manifest.get(field):
            raise ValueError(f"current pointer {field} mismatch")
    return {"pointer": pointer, "index": validated}


def index_file_snapshot(index_path: str | Path) -> list[dict[str, Any]]:
    root = _require_ascii_absolute(index_path, "index_path")
    return [
        {**_file_record(path, root), "mtime_ns": path.stat().st_mtime_ns}
        for path in sorted(root.rglob("*")) if path.is_file()
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    pin = subparsers.add_parser("pin-model")
    pin.add_argument("--source", required=True)
    pin.add_argument("--destination", required=True)
    pin.add_argument("--model-id", required=True)
    pin.add_argument("--revision", required=True)
    pin.add_argument("--dimension", type=int, required=True)
    pin.add_argument("--license", required=True, dest="license_name")
    build = subparsers.add_parser("build")
    build.add_argument("--deployment-root", required=True)
    build.add_argument("--lexical-index", required=True)
    build.add_argument("--model-path", required=True)
    build.add_argument("--build-id", required=True)
    build.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--deployment-root", required=True)
    args = parser.parse_args()
    if args.command == "pin-model":
        result = pin_model_snapshot(args.source, args.destination, model_id=args.model_id, revision=args.revision, dimension=args.dimension, license_name=args.license_name)
    elif args.command == "build":
        model = validate_pinned_model(args.model_path)
        encoder = SentenceTransformerEncoder(args.model_path, dimension=int(model["dimension"]))
        result = build_index(deployment_root=args.deployment_root, lexical_index=args.lexical_index, model_path=args.model_path, encoder=encoder, build_id=args.build_id, batch_size=args.batch_size, progress=True)
    else:
        result = read_current(args.deployment_root)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))


if __name__ == "__main__":
    main()


__all__ = [
    "SentenceTransformerEncoder", "build_index", "chunk_page",
    "index_file_snapshot", "pin_model_snapshot", "read_current",
    "switch_current", "validate_index_directory", "validate_pinned_model",
    "validate_index_against_lexical",
]
