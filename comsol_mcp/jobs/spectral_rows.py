"""Hash-chained raw point rows for durable spectral-characterization jobs."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import time
from typing import Any, Mapping

from comsol_mcp.evidence.contracts import validate_physical_evidence


SPECTRAL_ROW_SCHEMA_NAME = "comsol_mcp.durable_spectral_point"
SPECTRAL_ROW_SCHEMA_VERSION = "1.0.0"
MAX_SPECTRAL_ROW_BYTES = 128 * 1024
MAX_SPECTRAL_ROWS = 1024
SPECTRAL_STAGE_KINDS = frozenset({"initial_locator", "window_expansion", "refinement"})


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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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


def _finite(value: object, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0.0):
        qualifier = "positive and finite" if positive else "finite"
        raise ValueError(f"{name} must be {qualifier}")
    return number


def _optional_nonnegative_integer(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer or null")
    return value


def _portable_relative_path(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError(f"{name} must be one portable relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{name} must be one portable relative path")
    if len(value) > 512:
        raise ValueError(f"{name} exceeds 512 characters")
    return path.as_posix()


def spectral_collector_identity(spec: Mapping[str, Any]) -> str:
    """Return the collector identity bound by a normalized spectral job spec."""
    if spec.get("job_type") != "spectral_characterization":
        raise ValueError("spectral rows require a spectral_characterization job")
    return _fingerprint(_mapping(spec.get("collector"), "collector"))


def normalize_spectral_wavelength_m(value: object) -> float:
    """Canonicalize a metre wavelength before deduplication and persistence."""
    wavelength = _finite(value, "wavelength_m", positive=True)
    normalized = float(format(wavelength, ".15g"))
    if not math.isfinite(normalized) or normalized <= 0.0:
        raise ValueError("normalized wavelength_m must remain positive and finite")
    return normalized


def spectral_point_identity(spec: Mapping[str, Any], wavelength_m: object) -> dict[str, Any]:
    """Create the exact deduplication identity for one normalized wavelength."""
    wavelength = normalize_spectral_wavelength_m(wavelength_m)
    body = {
        "source_model_sha256": _hex_digest(
            spec.get("source_model_sha256"), "source_model_sha256"
        ),
        "configuration_sha256": _hex_digest(
            spec.get("configuration_sha256"), "configuration_sha256"
        ),
        "collector_identity_sha256": spectral_collector_identity(spec),
        "wavelength_parameter": spec.get("wavelength_parameter"),
        "requested_wavelength_m": wavelength,
    }
    fingerprint = _fingerprint(body)
    return {
        **body,
        "point_id": f"wl-{fingerprint[:20]}",
        "point_fingerprint": fingerprint,
    }


def _normalize_artifact(value: object) -> dict[str, Any]:
    raw = _mapping(value, "audit_artifact")
    fields = {
        "wrapper_relative_path",
        "wrapper_sha256",
        "wrapper_size_bytes",
        "inner_relative_path",
        "inner_sha256",
        "inner_size_bytes",
        "physical_evidence_sha256",
        "audit_status",
    }
    if set(raw) != fields:
        raise ValueError("audit_artifact has missing or unsupported fields")
    if raw["audit_status"] not in {"measurement_complete", "policy_evaluated"}:
        raise ValueError("audit_artifact.audit_status is not complete")
    normalized = {
        "wrapper_relative_path": _portable_relative_path(
            raw["wrapper_relative_path"], "audit_artifact.wrapper_relative_path"
        ),
        "wrapper_sha256": _hex_digest(raw["wrapper_sha256"], "audit_artifact.wrapper_sha256"),
        "inner_relative_path": _portable_relative_path(
            raw["inner_relative_path"], "audit_artifact.inner_relative_path"
        ),
        "inner_sha256": _hex_digest(raw["inner_sha256"], "audit_artifact.inner_sha256"),
        "physical_evidence_sha256": _hex_digest(
            raw["physical_evidence_sha256"],
            "audit_artifact.physical_evidence_sha256",
        ),
        "audit_status": raw["audit_status"],
    }
    for field in ("wrapper_size_bytes", "inner_size_bytes"):
        size = raw[field]
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ValueError(f"audit_artifact.{field} must be a positive integer")
        normalized[field] = size
    return normalized


def _verify_artifact_bytes(
    artifact: Mapping[str, Any],
    root: Path,
    *,
    spec: Mapping[str, Any],
    point_fingerprint: str,
) -> None:
    resolved_root = root.resolve()
    for prefix in ("wrapper", "inner"):
        path = (resolved_root / artifact[f"{prefix}_relative_path"]).resolve()
        try:
            path.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError(f"audit {prefix} artifact escapes the durable job directory") from exc
        if not path.is_file():
            raise ValueError(f"audit {prefix} artifact is missing")
        if path.stat().st_size != artifact[f"{prefix}_size_bytes"]:
            raise ValueError(f"audit {prefix} artifact size does not match")
        if _sha256_file(path) != artifact[f"{prefix}_sha256"]:
            raise ValueError(f"audit {prefix} artifact hash does not match")
    inner_path = resolved_root / artifact["inner_relative_path"]
    try:
        inner = json.loads(inner_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("audit inner artifact is not valid JSON") from exc
    physical = validate_physical_evidence(inner.get("physical_evidence"))
    if physical["contract_sha256"] != artifact["physical_evidence_sha256"]:
        raise ValueError("physical evidence hash differs from the durable row")
    if inner.get("audit_status") != artifact["audit_status"]:
        raise ValueError("audit status differs from the durable row")
    if physical["producer"]["tool"] != "wave_optics_point_audit":
        raise ValueError("physical evidence producer is not the declared point audit")
    if physical["identity"]["source_sha256"] != spec.get("source_model_sha256"):
        raise ValueError("physical evidence source hash differs from the immutable job")
    if physical["identity"]["config_id"] != point_fingerprint:
        raise ValueError("physical evidence point identity differs from the durable row")


def _normalize_row(
    value: object,
    *,
    spec: Mapping[str, Any],
    sequence: int,
    previous_row_sha256: str | None,
    artifact_root: Path | None,
) -> dict[str, Any]:
    raw = _mapping(value, f"spectral row {sequence}")
    fields = {
        "schema_name",
        "schema_version",
        "sequence",
        "attempt",
        "stage_index",
        "stage_kind",
        "created_at_epoch",
        "spec_fingerprint",
        "source_model_sha256",
        "configuration_sha256",
        "collector_identity_sha256",
        "point_id",
        "point_fingerprint",
        "requested_wavelength_m",
        "evaluated_wavelength_m",
        "frequency_wavelength_m",
        "R",
        "T",
        "A",
        "mesh_element_count",
        "mesh_vertex_count",
        "solve_seconds",
        "audit_artifact",
        "previous_row_sha256",
        "row_sha256",
    }
    if set(raw) != fields:
        raise ValueError(f"spectral row {sequence} has missing or unsupported fields")
    if raw["schema_name"] != SPECTRAL_ROW_SCHEMA_NAME or raw["schema_version"] != SPECTRAL_ROW_SCHEMA_VERSION:
        raise ValueError("unsupported spectral row schema")
    if raw["sequence"] != sequence:
        raise ValueError("spectral row sequence is not contiguous")
    if raw["previous_row_sha256"] != previous_row_sha256:
        raise ValueError("spectral row hash chain is discontinuous")
    attempt = raw["attempt"]
    stage_index = raw["stage_index"]
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0:
        raise ValueError("spectral row attempt must be positive")
    if isinstance(stage_index, bool) or not isinstance(stage_index, int) or stage_index < 0:
        raise ValueError("spectral row stage_index must be nonnegative")
    if raw["stage_kind"] not in SPECTRAL_STAGE_KINDS:
        raise ValueError("spectral row stage_kind is unsupported")
    created = _finite(raw["created_at_epoch"], "created_at_epoch")
    if raw["spec_fingerprint"] != spec.get("spec_fingerprint"):
        raise ValueError("spectral row spec fingerprint differs from the immutable job")
    if raw["source_model_sha256"] != spec.get("source_model_sha256"):
        raise ValueError("spectral row source hash differs from the immutable job")
    if raw["configuration_sha256"] != spec.get("configuration_sha256"):
        raise ValueError("spectral row configuration hash differs from the immutable job")
    if raw["collector_identity_sha256"] != spectral_collector_identity(spec):
        raise ValueError("spectral row collector identity differs from the immutable job")
    identity = spectral_point_identity(spec, raw["requested_wavelength_m"])
    if raw["point_id"] != identity["point_id"] or raw["point_fingerprint"] != identity["point_fingerprint"]:
        raise ValueError("spectral row point identity does not match its wavelength")
    artifact = _normalize_artifact(raw["audit_artifact"])
    normalized = {
        **raw,
        "created_at_epoch": created,
        "requested_wavelength_m": identity["requested_wavelength_m"],
        "evaluated_wavelength_m": _finite(raw["evaluated_wavelength_m"], "evaluated_wavelength_m", positive=True),
        "frequency_wavelength_m": _finite(raw["frequency_wavelength_m"], "frequency_wavelength_m", positive=True),
        "R": _finite(raw["R"], "R"),
        "T": _finite(raw["T"], "T"),
        "A": _finite(raw["A"], "A"),
        "mesh_element_count": _optional_nonnegative_integer(raw["mesh_element_count"], "mesh_element_count"),
        "mesh_vertex_count": _optional_nonnegative_integer(raw["mesh_vertex_count"], "mesh_vertex_count"),
        "solve_seconds": _finite(raw["solve_seconds"], "solve_seconds"),
        "audit_artifact": artifact,
        "row_sha256": _hex_digest(raw["row_sha256"], "row_sha256"),
    }
    if normalized["solve_seconds"] < 0.0:
        raise ValueError("solve_seconds must be nonnegative")
    expected = _fingerprint({key: item for key, item in normalized.items() if key != "row_sha256"})
    if normalized["row_sha256"] != expected:
        raise ValueError("spectral row hash does not match its canonical content")
    if artifact_root is not None:
        _verify_artifact_bytes(
            artifact,
            artifact_root,
            spec=spec,
            point_fingerprint=identity["point_fingerprint"],
        )
    return normalized


def read_spectral_rows(
    path: str | Path,
    spec: Mapping[str, Any],
    *,
    artifact_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Read and validate every durable row, hash link, identity, and artifact."""
    journal = Path(path)
    if not journal.exists():
        return []
    root = Path(artifact_root) if artifact_root is not None else None
    rows: list[dict[str, Any]] = []
    previous: str | None = None
    with journal.open("r", encoding="utf-8") as handle:
        for sequence, line in enumerate(handle, 1):
            if not line.strip():
                raise ValueError("spectral row journal contains a blank record")
            if sequence > MAX_SPECTRAL_ROWS:
                raise ValueError("spectral row journal exceeds its entry limit")
            if len(line.encode("utf-8")) > MAX_SPECTRAL_ROW_BYTES:
                raise ValueError("spectral row exceeds its byte limit")
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("spectral row journal contains malformed JSON") from exc
            row = _normalize_row(
                value,
                spec=spec,
                sequence=sequence,
                previous_row_sha256=previous,
                artifact_root=root,
            )
            rows.append(row)
            previous = row["row_sha256"]
    complete = [row["point_fingerprint"] for row in rows]
    if len(complete) != len(set(complete)):
        raise ValueError("spectral row journal contains a duplicate complete point")
    return rows


def completed_spectral_point_fingerprints(
    path: str | Path,
    spec: Mapping[str, Any],
    *,
    artifact_root: str | Path | None = None,
) -> set[str]:
    return {
        row["point_fingerprint"]
        for row in read_spectral_rows(path, spec, artifact_root=artifact_root)
    }


def append_spectral_row(
    path: str | Path,
    spec: Mapping[str, Any],
    *,
    attempt: int,
    stage_index: int,
    stage_kind: str,
    requested_wavelength_m: float,
    evaluated_wavelength_m: float,
    frequency_wavelength_m: float,
    R: float,
    T: float,
    A: float,
    mesh_element_count: int | None,
    mesh_vertex_count: int | None,
    solve_seconds: float,
    audit_artifact: Mapping[str, Any],
    artifact_root: str | Path,
    created_at_epoch: float | None = None,
) -> dict[str, Any]:
    """Append and fsync one complete raw point only after its artifact validates."""
    rows = read_spectral_rows(path, spec, artifact_root=artifact_root)
    identity = spectral_point_identity(spec, requested_wavelength_m)
    if identity["point_fingerprint"] in {row["point_fingerprint"] for row in rows}:
        raise ValueError("an exact complete spectral point already exists")
    row = {
        "schema_name": SPECTRAL_ROW_SCHEMA_NAME,
        "schema_version": SPECTRAL_ROW_SCHEMA_VERSION,
        "sequence": len(rows) + 1,
        "attempt": attempt,
        "stage_index": stage_index,
        "stage_kind": stage_kind,
        "created_at_epoch": float(created_at_epoch if created_at_epoch is not None else time.time()),
        "spec_fingerprint": spec["spec_fingerprint"],
        "source_model_sha256": spec["source_model_sha256"],
        "configuration_sha256": spec["configuration_sha256"],
        "collector_identity_sha256": spectral_collector_identity(spec),
        "point_id": identity["point_id"],
        "point_fingerprint": identity["point_fingerprint"],
        "requested_wavelength_m": identity["requested_wavelength_m"],
        "evaluated_wavelength_m": evaluated_wavelength_m,
        "frequency_wavelength_m": frequency_wavelength_m,
        "R": R,
        "T": T,
        "A": A,
        "mesh_element_count": mesh_element_count,
        "mesh_vertex_count": mesh_vertex_count,
        "solve_seconds": solve_seconds,
        "audit_artifact": dict(audit_artifact),
        "previous_row_sha256": rows[-1]["row_sha256"] if rows else None,
    }
    row["row_sha256"] = _fingerprint(row)
    normalized = _normalize_row(
        row,
        spec=spec,
        sequence=row["sequence"],
        previous_row_sha256=row["previous_row_sha256"],
        artifact_root=Path(artifact_root),
    )
    payload = _canonical_bytes(normalized) + b"\n"
    if len(payload) > MAX_SPECTRAL_ROW_BYTES:
        raise ValueError("spectral row exceeds its byte limit")
    if len(rows) >= min(int(spec["maximum_points"]), MAX_SPECTRAL_ROWS):
        raise ValueError("spectral row journal reached the declared point cap")
    journal = Path(path)
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("ab") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return normalized


__all__ = [
    "MAX_SPECTRAL_ROWS",
    "SPECTRAL_ROW_SCHEMA_NAME",
    "SPECTRAL_ROW_SCHEMA_VERSION",
    "SPECTRAL_STAGE_KINDS",
    "append_spectral_row",
    "completed_spectral_point_fingerprints",
    "read_spectral_rows",
    "normalize_spectral_wavelength_m",
    "spectral_collector_identity",
    "spectral_point_identity",
]
