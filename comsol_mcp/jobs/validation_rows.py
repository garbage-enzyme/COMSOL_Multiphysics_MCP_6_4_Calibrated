"""Append-only durable rows for bounded validation-matrix jobs."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import time
from typing import Any, Mapping


VALIDATION_ROW_SCHEMA_VERSION = "1.0.0"
MAX_VALIDATION_ROWS = 256
MAX_VALIDATION_ROW_BYTES = 128 * 1024
_COMPLETE_AUDIT_STATES = frozenset({"measurement_complete", "policy_evaluated"})


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object with string keys")
    return dict(value)


def _hex_digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value.lower())
    ):
        raise ValueError(f"{name} must be exactly 64 hexadecimal characters")
    return value.lower()


def _point_map(spec: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    if spec.get("job_type") != "validation_matrix":
        raise ValueError("validation rows require a validation_matrix specification")
    points = spec.get("points")
    if not isinstance(points, list) or not points:
        raise ValueError("validation_matrix points are unavailable")
    mapped: dict[str, dict[str, Any]] = {}
    for point in points:
        item = _mapping(point, "validation_matrix point")
        point_id = item.get("point_id")
        if not isinstance(point_id, str) or not point_id:
            raise ValueError("validation_matrix point_id is invalid")
        if point_id in mapped:
            raise ValueError("validation_matrix point_id values must be unique")
        mapped[point_id] = item
    return mapped


def _normalize_manifest_path(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError(f"{name} must be one portable relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{name} must be one portable relative path")
    if len(value) > 512:
        raise ValueError(f"{name} exceeds 512 characters")
    return path.as_posix()


def _normalize_collector_summary(
    value: object,
    *,
    point: Mapping[str, Any],
    index: int,
) -> dict[str, Any]:
    name = f"collector_summaries[{index}]"
    raw = _mapping(value, name)
    fields = {
        "collector",
        "artifact_id",
        "audit_status",
        "manifest_relative_path",
        "manifest_sha256",
        "manifest_size_bytes",
    }
    if set(raw) != fields:
        raise ValueError(f"{name} has missing or unsupported fields")
    collectors = point.get("collectors")
    artifacts = point.get("expected_artifact_ids")
    if not isinstance(collectors, list) or not isinstance(artifacts, list):
        raise ValueError("point collector/artifact declarations are invalid")
    if index >= len(collectors) or index >= len(artifacts):
        raise ValueError(f"{name} exceeds the point declaration")
    declared_collector = _mapping(collectors[index], f"point.collectors[{index}]").get("name")
    if raw.get("collector") != declared_collector:
        raise ValueError(f"{name}.collector differs from the immutable point declaration")
    if raw.get("artifact_id") != artifacts[index]:
        raise ValueError(f"{name}.artifact_id differs from the immutable point declaration")
    audit_status = raw.get("audit_status")
    if audit_status not in _COMPLETE_AUDIT_STATES:
        raise ValueError(f"{name}.audit_status is not complete")
    size = raw.get("manifest_size_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError(f"{name}.manifest_size_bytes must be a positive integer")
    return {
        "collector": declared_collector,
        "artifact_id": artifacts[index],
        "audit_status": audit_status,
        "manifest_relative_path": _normalize_manifest_path(
            raw.get("manifest_relative_path"), f"{name}.manifest_relative_path"
        ),
        "manifest_sha256": _hex_digest(raw.get("manifest_sha256"), f"{name}.manifest_sha256"),
        "manifest_size_bytes": size,
    }


def _normalize_error(value: object) -> dict[str, str]:
    raw = _mapping(value, "error")
    if set(raw) != {"type", "message"}:
        raise ValueError("error requires exactly type and message")
    normalized: dict[str, str] = {}
    for field, limit in (("type", 128), ("message", 2000)):
        item = raw.get(field)
        if not isinstance(item, str) or not item or len(item) > limit:
            raise ValueError(f"error.{field} must be nonempty and at most {limit} characters")
        normalized[field] = item
    return normalized


def _normalize_row(
    value: object,
    *,
    spec: Mapping[str, Any],
    sequence: int,
    previous_row_sha256: str | None,
) -> dict[str, Any]:
    raw = _mapping(value, f"validation row {sequence}")
    fields = {
        "schema_version",
        "sequence",
        "attempt",
        "created_at_epoch",
        "spec_fingerprint",
        "source_model_sha256",
        "point_id",
        "point_fingerprint",
        "configuration_sha256",
        "status",
        "collector_summaries",
        "error",
        "previous_row_sha256",
        "row_sha256",
    }
    if set(raw) != fields:
        raise ValueError(f"validation row {sequence} has missing or unsupported fields")
    if raw.get("schema_version") != VALIDATION_ROW_SCHEMA_VERSION:
        raise ValueError("unsupported validation row schema_version")
    if raw.get("sequence") != sequence:
        raise ValueError("validation row sequence is not contiguous")
    attempt = raw.get("attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0:
        raise ValueError("validation row attempt must be a positive integer")
    created = raw.get("created_at_epoch")
    if isinstance(created, bool) or not isinstance(created, (int, float)) or not math.isfinite(float(created)):
        raise ValueError("validation row timestamp must be finite")
    if raw.get("previous_row_sha256") != previous_row_sha256:
        raise ValueError("validation row hash chain is discontinuous")
    if raw.get("spec_fingerprint") != spec.get("spec_fingerprint"):
        raise ValueError("validation row spec_fingerprint differs from the immutable job")
    if raw.get("source_model_sha256") != spec.get("source_model_sha256"):
        raise ValueError("validation row source hash differs from the immutable job")
    points = _point_map(spec)
    point_id = raw.get("point_id")
    if point_id not in points:
        raise ValueError("validation row point_id is not declared by the immutable job")
    point = points[point_id]
    if raw.get("point_fingerprint") != point.get("point_fingerprint"):
        raise ValueError("validation row point fingerprint differs from the immutable job")
    if raw.get("configuration_sha256") != point.get("configuration_sha256"):
        raise ValueError("validation row configuration hash differs from the immutable job")
    status = raw.get("status")
    if status not in {"ok", "error"}:
        raise ValueError("validation row status must be ok or error")
    summaries_raw = raw.get("collector_summaries")
    if not isinstance(summaries_raw, list):
        raise ValueError("collector_summaries must be a list")
    summaries = [
        _normalize_collector_summary(item, point=point, index=index)
        for index, item in enumerate(summaries_raw)
    ]
    if status == "ok":
        if len(summaries) != len(point["collectors"]) or len(summaries) != len(
            point["expected_artifact_ids"]
        ):
            raise ValueError("ok validation rows require every declared collector artifact")
        if raw.get("error") is not None:
            raise ValueError("ok validation rows must not contain error evidence")
        error = None
    else:
        if len(summaries) > len(point["collectors"]):
            raise ValueError("error validation row has too many collector summaries")
        error = _normalize_error(raw.get("error"))
    normalized = {
        **raw,
        "created_at_epoch": float(created),
        "collector_summaries": summaries,
        "error": error,
        "row_sha256": _hex_digest(raw.get("row_sha256"), "row_sha256"),
    }
    expected_hash = _fingerprint(
        {key: item for key, item in normalized.items() if key != "row_sha256"}
    )
    if normalized["row_sha256"] != expected_hash:
        raise ValueError("validation row_sha256 does not match its canonical content")
    return normalized


def read_validation_rows(path: str | Path, spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read and validate the bounded row journal and its exact identity chain."""
    journal = Path(path)
    if not journal.exists():
        return []
    rows: list[dict[str, Any]] = []
    previous: str | None = None
    with journal.open("r", encoding="utf-8") as handle:
        for sequence, line in enumerate(handle, 1):
            if not line.strip():
                raise ValueError("validation row journal contains a blank record")
            if sequence > MAX_VALIDATION_ROWS:
                raise ValueError("validation row journal exceeds its entry limit")
            if len(line.encode("utf-8")) > MAX_VALIDATION_ROW_BYTES:
                raise ValueError("validation row exceeds its byte limit")
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("validation row journal contains malformed JSON") from exc
            row = _normalize_row(
                value,
                spec=spec,
                sequence=sequence,
                previous_row_sha256=previous,
            )
            rows.append(row)
            previous = row["row_sha256"]
    return rows


def completed_point_fingerprints(
    path: str | Path, spec: Mapping[str, Any]
) -> set[str]:
    """Return only exact valid point identities with one complete durable row."""
    return {
        row["point_fingerprint"]
        for row in read_validation_rows(path, spec)
        if row["status"] == "ok"
    }


def append_validation_row(
    path: str | Path,
    spec: Mapping[str, Any],
    *,
    attempt: int,
    point_id: str,
    status: str,
    collector_summaries: list[dict[str, Any]] | None = None,
    error: dict[str, str] | None = None,
    created_at_epoch: float | None = None,
) -> dict[str, Any]:
    """Append one hashed row, flushing and fsyncing before it becomes resumable."""
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0:
        raise ValueError("attempt must be a positive integer")
    rows = read_validation_rows(path, spec)
    points = _point_map(spec)
    if point_id not in points:
        raise ValueError("point_id is not declared by the immutable job")
    point = points[point_id]
    if status == "ok" and point["point_fingerprint"] in {
        row["point_fingerprint"] for row in rows if row["status"] == "ok"
    }:
        raise ValueError("an exact complete validation row already exists")
    row = {
        "schema_version": VALIDATION_ROW_SCHEMA_VERSION,
        "sequence": len(rows) + 1,
        "attempt": attempt,
        "created_at_epoch": float(created_at_epoch if created_at_epoch is not None else time.time()),
        "spec_fingerprint": spec["spec_fingerprint"],
        "source_model_sha256": spec["source_model_sha256"],
        "point_id": point_id,
        "point_fingerprint": point["point_fingerprint"],
        "configuration_sha256": point["configuration_sha256"],
        "status": status,
        "collector_summaries": list(collector_summaries or []),
        "error": error,
        "previous_row_sha256": rows[-1]["row_sha256"] if rows else None,
    }
    row["row_sha256"] = _fingerprint(row)
    normalized = _normalize_row(
        row,
        spec=spec,
        sequence=row["sequence"],
        previous_row_sha256=row["previous_row_sha256"],
    )
    payload = _canonical_bytes(normalized) + b"\n"
    if len(payload) > MAX_VALIDATION_ROW_BYTES:
        raise ValueError("validation row exceeds its byte limit")
    if len(rows) >= MAX_VALIDATION_ROWS:
        raise ValueError("validation row journal exceeds its entry limit")
    journal = Path(path)
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("ab") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return normalized


__all__ = [
    "MAX_VALIDATION_ROWS",
    "VALIDATION_ROW_SCHEMA_VERSION",
    "append_validation_row",
    "completed_point_fingerprints",
    "read_validation_rows",
]
