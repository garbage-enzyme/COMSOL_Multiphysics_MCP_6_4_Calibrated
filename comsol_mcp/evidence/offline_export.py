"""Offline export-manifest construction, validation, and tamper detection.

``comsol_mcp.offline_export_manifest`` binds VTU/CSV/TXT artifacts to model
identity, dataset/solution provenance, ordered expressions with units,
parameter/time values, fidelity, producer identity, byte counts, and SHA-256
hashes. The validator works with COMSOL closed: it re-derives every checkable
fact from the artifact bytes and rejects missing files, duplicate IDs,
ordering or unit drift, stale manifest hashes, path escapes, unsupported
readers, and unbounded artifact counts. A validated export is integrity
evidence only and is never promoted to FEM validation.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from comsol_mcp.durable import canonical_sha256_v1

OFFLINE_EXPORT_MANIFEST_SCHEMA_NAME = "comsol_mcp.offline_export_manifest"
OFFLINE_EXPORT_MANIFEST_SCHEMA_VERSION = "1.0.0"

SUPPORTED_FORMATS = frozenset({"vtu", "csv", "txt"})
# Formats this package can lightly inspect offline; vtu stays hash-only.
OFFLINE_READABLE_FORMATS = frozenset({"csv", "txt"})

MAX_TEXT = 256
MAX_ID = 128


class OfflineExportError(ValueError):
    """Raised when a manifest violates its closed published contract."""


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise OfflineExportError(f"{label} must be a non-empty string")
    if len(value) > MAX_TEXT:
        raise OfflineExportError(f"{label} exceeds {MAX_TEXT} characters")
    return value


def _optional_text(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _hex64(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in value)
    ):
        raise OfflineExportError(f"{label} must be 64 hexadecimal characters or null")
    return value.lower()


def _bounded_number(value: Any, label: str) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OfflineExportError(f"{label} must be numeric or null")
    if not math.isfinite(float(value)):
        raise OfflineExportError(f"{label} must be finite")
    checked: float | int = value
    return checked


def _ordering_fingerprint(columns: list[dict[str, Any]]) -> str:
    sealed: list[dict[str, str]] = [
        {"expression": item["expression"], "unit": item["unit"]} for item in columns
    ]
    digest: str = canonical_sha256_v1(sealed)
    return digest


def _normalize_artifact(raw: Any, index: int) -> dict[str, Any]:
    label = f"artifact {index}"
    required = {
        "artifact_id",
        "relative_path",
        "format",
        "dataset",
        "solution",
        "columns",
        "ordering_sha256",
        "parameter_values",
        "time_values",
        "fidelity",
        "byte_count",
        "sha256",
        "reader_status",
    }
    if not isinstance(raw, Mapping):
        raise OfflineExportError(f"{label} must be an object")
    if set(raw) != required:
        missing = sorted(required - set(raw))
        unknown = sorted(set(raw) - required)
        raise OfflineExportError(f"{label} field mismatch (missing={missing}, extra={unknown})")

    artifact_id = _text(raw["artifact_id"], f"{label}.artifact_id")
    if len(artifact_id) > MAX_ID:
        raise OfflineExportError(f"{label}.artifact_id exceeds {MAX_ID} characters")

    relative = _text(raw["relative_path"], f"{label}.relative_path")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "\\" in relative or not pure.name:
        raise OfflineExportError(f"{label}.relative_path escapes the export directory")

    fmt = _text(raw["format"], f"{label}.format").lower()
    if fmt not in SUPPORTED_FORMATS:
        raise OfflineExportError(f"{label}.format must be one of {sorted(SUPPORTED_FORMATS)}")

    columns_raw = raw["columns"]
    if not isinstance(columns_raw, list) or not columns_raw or len(columns_raw) > 256:
        raise OfflineExportError(f"{label}.columns must be a non-empty list of at most 256 pairs")
    columns: list[dict[str, Any]] = []
    for position, pair in enumerate(columns_raw):
        if not isinstance(pair, Mapping) or set(pair) != {"expression", "unit"}:
            raise OfflineExportError(f"{label}.column {position} must bind expression and unit")
        columns.append(
            {
                "expression": _text(pair["expression"], f"{label}.column {position}.expression"),
                "unit": _text(pair["unit"], f"{label}.column {position}.unit"),
            }
        )
    ordering = _hex64(raw["ordering_sha256"], f"{label}.ordering_sha256")
    if ordering is None or ordering != _ordering_fingerprint(columns):
        raise OfflineExportError(f"{label} ordering fingerprint does not match its columns")

    parameters_raw = raw["parameter_values"] or {}
    if not isinstance(parameters_raw, Mapping):
        raise OfflineExportError(f"{label}.parameter_values must be an object or null")
    parameter_values = {
        _text(key, f"{label}.parameter_values key"): _bounded_number(
            value, f"{label}.parameter_values[{key}]"
        )
        for key, value in parameters_raw.items()
    }
    time_raw = raw["time_values"]
    if time_raw is None:
        time_values = None
    elif isinstance(time_raw, list):
        time_values = [
            _bounded_number(value, f"{label}.time_values[{position}]")
            for position, value in enumerate(time_raw)
        ]
    else:
        raise OfflineExportError(f"{label}.time_values must be a list or null")

    byte_count = raw["byte_count"]
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count <= 0:
        raise OfflineExportError(f"{label}.byte_count must be a positive integer")
    sha256 = _hex64(raw["sha256"], f"{label}.sha256")
    if sha256 is None:
        raise OfflineExportError(f"{label}.sha256 is required")

    reader_status = _text(raw["reader_status"], f"{label}.reader_status")
    if reader_status not in {"offline_reader_available", "hash_only_reader"}:
        raise OfflineExportError(
            f"{label}.reader_status must be offline_reader_available or hash_only_reader"
        )
    if fmt not in OFFLINE_READABLE_FORMATS and reader_status == "offline_reader_available":
        raise OfflineExportError(
            f"{label}.reader_status claims an offline reader for the unsupported {fmt} format"
        )

    fidelity = raw["fidelity"]
    if fidelity not in {"raw", "interpolated", "derived"}:
        raise OfflineExportError(f"{label}.fidelity must be raw, interpolated, or derived")

    return {
        "artifact_id": artifact_id,
        "relative_path": relative,
        "format": fmt,
        "dataset": _optional_text(raw["dataset"], f"{label}.dataset"),
        "solution": _optional_text(raw["solution"], f"{label}.solution"),
        "columns": columns,
        "ordering_sha256": ordering,
        "parameter_values": parameter_values,
        "time_values": time_values,
        "fidelity": fidelity,
        "byte_count": byte_count,
        "sha256": sha256,
        "reader_status": reader_status,
    }


def _normalize_structure(payload: Any) -> dict[str, Any]:
    """Validate the closed manifest shape without checking the top hash."""
    if not isinstance(payload, Mapping):
        raise OfflineExportError("manifest must be an object")
    top_required = {
        "schema_name",
        "schema_version",
        "producer",
        "source_identity",
        "artifacts",
    }
    if not set(top_required) <= set(payload):
        missing = sorted(top_required - set(payload))
        raise OfflineExportError(f"manifest is missing fields: {missing}")
    if payload["schema_name"] != OFFLINE_EXPORT_MANIFEST_SCHEMA_NAME:
        raise OfflineExportError("unsupported manifest schema_name")
    if payload["schema_version"] != OFFLINE_EXPORT_MANIFEST_SCHEMA_VERSION:
        raise OfflineExportError("unsupported manifest schema_version")

    producer = payload["producer"]
    if not isinstance(producer, Mapping) or set(producer) != {"tool", "version"}:
        raise OfflineExportError("producer must bind exactly tool and version")
    producer_normalized = {
        "tool": _text(producer["tool"], "producer.tool"),
        "version": _text(producer["version"], "producer.version"),
    }

    source = payload["source_identity"]
    if not isinstance(source, Mapping) or set(source) != {
        "model_path_redacted",
        "model_sha256",
        "dataset",
        "solution",
        "fidelity",
    }:
        raise OfflineExportError("source_identity fields are invalid")
    fidelity = source["fidelity"]
    if fidelity not in {"raw", "interpolated", "derived"}:
        raise OfflineExportError("source_identity.fidelity is invalid")
    source_normalized = {
        "model_path_redacted": _text(source["model_path_redacted"], "source_identity.model"),
        "model_sha256": _hex64(source["model_sha256"], "source_identity.model_sha256"),
        "dataset": _optional_text(source["dataset"], "source_identity.dataset"),
        "solution": _optional_text(source["solution"], "source_identity.solution"),
        "fidelity": fidelity,
    }

    artifacts_raw = payload["artifacts"]
    if not isinstance(artifacts_raw, list):
        raise OfflineExportError("artifacts must be a list")
    artifacts = []
    seen: set[str] = set()
    for index, item in enumerate(artifacts_raw):
        artifact = _normalize_artifact(item, index)
        if artifact["artifact_id"] in seen:
            raise OfflineExportError(f"duplicate artifact_id {artifact['artifact_id']!r}")
        seen.add(artifact["artifact_id"])
        artifacts.append(artifact)

    return {
        "schema_name": OFFLINE_EXPORT_MANIFEST_SCHEMA_NAME,
        "schema_version": OFFLINE_EXPORT_MANIFEST_SCHEMA_VERSION,
        "producer": producer_normalized,
        "source_identity": source_normalized,
        "artifacts": artifacts,
    }


def normalize_offline_export_manifest(payload: Any) -> dict[str, Any]:
    """Validate one loaded manifest against the closed published contract."""
    manifest = _normalize_structure(payload)
    declared = _hex64(payload.get("manifest_sha256"), "manifest_sha256")
    recomputed = canonical_sha256_v1(manifest)
    if declared != recomputed:
        raise OfflineExportError("stale_manifest_hash")
    manifest["manifest_sha256"] = recomputed
    return manifest


def build_offline_export_manifest(
    *,
    producer_tool: str,
    producer_version: str,
    model_path_redacted: str,
    model_sha256: str | None,
    dataset: str | None = None,
    solution: str | None = None,
    fidelity: str = "raw",
    artifacts: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build one canonical export manifest from caller-friendly inputs.

    Each artifact mapping binds ``artifact_id``, ``relative_path``,
    ``format``, optional ``dataset``/``solution``, ordered ``expressions``
    with aligned ``units``, optional ``parameter_values``/``time_values``, a
    ``fidelity`` override, plus ``byte_count`` and ``sha256`` over the exact
    file bytes. Ordering integrity is sealed by a per-artifact fingerprint.
    """
    normalized_artifacts: list[dict[str, Any]] = []
    for index, raw in enumerate(artifacts):
        if not isinstance(raw, Mapping):
            raise OfflineExportError(f"artifact {index} must be an object")
        expected_inputs = {
            "artifact_id",
            "relative_path",
            "format",
            "expressions",
            "units",
            "parameter_values",
            "time_values",
            "byte_count",
            "sha256",
            "reader_status",
        }
        if not expected_inputs <= set(raw):
            missing = sorted(expected_inputs - set(raw))
            raise OfflineExportError(f"artifact {index} is missing inputs: {missing}")
        columns = [
            {"expression": expression, "unit": unit}
            for expression, unit in zip(raw["expressions"], raw["units"], strict=True)
        ]
        full: dict[str, Any] = {
            "artifact_id": raw["artifact_id"],
            "relative_path": raw["relative_path"],
            "format": raw["format"],
            "dataset": raw.get("dataset"),
            "solution": raw.get("solution"),
            "columns": columns,
            "ordering_sha256": _ordering_fingerprint(columns),
            "parameter_values": raw["parameter_values"],
            "time_values": raw["time_values"],
            "fidelity": raw.get("fidelity", fidelity),
            "byte_count": raw["byte_count"],
            "sha256": raw["sha256"],
            "reader_status": raw["reader_status"],
        }
        normalized_artifacts.append(full)

    body: dict[str, Any] = {
        "schema_name": OFFLINE_EXPORT_MANIFEST_SCHEMA_NAME,
        "schema_version": OFFLINE_EXPORT_MANIFEST_SCHEMA_VERSION,
        "producer": {
            "tool": _text(producer_tool, "producer.tool"),
            "version": _text(producer_version, "producer.version"),
        },
        "source_identity": {
            "model_path_redacted": _text(model_path_redacted, "model_path_redacted"),
            "model_sha256": _hex64(model_sha256, "model_sha256"),
            "dataset": _optional_text(dataset, "dataset"),
            "solution": _optional_text(solution, "solution"),
            "fidelity": fidelity,
        },
        "artifacts": normalized_artifacts,
    }
    manifest = _normalize_structure(body)
    manifest["manifest_sha256"] = canonical_sha256_v1(manifest)
    return manifest


def validate_offline_export_manifest(
    manifest_source: Mapping[str, Any] | bytes | str | Path,
    base_directory: str | Path | None = None,
    *,
    expected_model_sha256: str | None = None,
    max_manifest_bytes: int = 33_554_432,
    max_artifacts: int = 512,
    max_artifact_bytes: int = 17_179_869_184,
) -> dict[str, Any]:
    """Validate one manifest plus its artifacts entirely offline."""
    warnings: list[str] = []
    failures: list[dict[str, Any]] = []

    if isinstance(manifest_source, (str, Path)):
        path = Path(manifest_source)
        try:
            raw = path.read_bytes()
        except OSError:
            return _invalid_verdict(["manifest_unavailable"])
        base_directory = base_directory or path.parent
        if len(raw) > max_manifest_bytes:
            return _invalid_verdict(["manifest_too_large"])
        try:
            payload = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError, json.JSONDecodeError:
            return _invalid_verdict(["manifest_not_valid_json"])
    elif isinstance(manifest_source, (bytes, bytearray)):
        if len(manifest_source) > max_manifest_bytes:
            return _invalid_verdict(["manifest_too_large"])
        try:
            payload = json.loads(bytes(manifest_source).decode("utf-8"))
        except UnicodeDecodeError, json.JSONDecodeError:
            return _invalid_verdict(["manifest_not_valid_json"])
        if base_directory is None:
            return _invalid_verdict(["base_directory_undeclared"])
    else:
        payload = manifest_source
        if base_directory is None:
            return _invalid_verdict(["base_directory_undeclared"])

    try:
        manifest = normalize_offline_export_manifest(payload)
    except OfflineExportError as exc:
        detail = str(exc)
        if detail == "stale_manifest_hash":
            return _invalid_verdict(["stale_manifest_hash"])
        if "ordering fingerprint" in detail:
            code = "ordering_drift"
        elif "escapes the export directory" in detail:
            code = "path_escape"
        elif "duplicate artifact_id" in detail:
            code = "duplicate_artifact_id"
        else:
            code = "manifest_invalid"
        rejected = _invalid_verdict([code])
        rejected["failures"][0]["detail"] = detail[:160]
        return rejected

    if len(manifest["artifacts"]) > max_artifacts:
        failures.append({"artifact_id": None, "reason_codes": ["too_many_artifacts"]})

    source_hash = manifest["source_identity"]["model_sha256"]
    if expected_model_sha256 is not None:
        if source_hash is None:
            failures.append({"artifact_id": None, "reason_codes": ["model_identity_undeclared"]})
        elif expected_model_sha256.lower() != source_hash:
            failures.append({"artifact_id": None, "reason_codes": ["model_identity_mismatch"]})
    elif source_hash is None:
        warnings.append("model_hash_undeclared")

    base = Path(base_directory).resolve()
    for artifact in manifest["artifacts"]:
        reasons: list[str] = []
        target = (base / artifact["relative_path"]).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            reasons.append("path_escape")
        if not reasons:
            if not target.is_file():
                reasons.append("missing_file")
            else:
                size = target.stat().st_size
                if size > max_artifact_bytes:
                    reasons.append("artifact_over_declared_limit")
                elif size != artifact["byte_count"]:
                    reasons.append("byte_count_mismatch")
                    digest = hashlib.sha256(target.read_bytes()).hexdigest()
                    if digest != artifact["sha256"]:
                        reasons.append("artifact_hash_mismatch")
                else:
                    digest = hashlib.sha256(target.read_bytes()).hexdigest()
                    if digest != artifact["sha256"]:
                        reasons.append("artifact_hash_mismatch")
                    elif artifact["format"] in OFFLINE_READABLE_FORMATS:
                        try:
                            text = target.read_text(encoding="utf-8")
                            if not text.strip():
                                reasons.append("empty_text_export")
                        except UnicodeDecodeError:
                            reasons.append("text_export_not_utf8")
        if reasons:
            failures.append({"artifact_id": artifact["artifact_id"], "reason_codes": reasons})

    valid = not failures
    verdict: dict[str, Any] = {
        "schema_name": OFFLINE_EXPORT_MANIFEST_SCHEMA_NAME,
        "schema_version": OFFLINE_EXPORT_MANIFEST_SCHEMA_VERSION,
        "valid": valid,
        "checked_artifacts": len(manifest["artifacts"]),
        "failures": failures,
        "warnings": sorted(set(warnings)),
        "is_fem_validation": False,
        "validation_sha256": "",
    }
    body = {key: value for key, value in verdict.items() if key != "validation_sha256"}
    verdict["validation_sha256"] = canonical_sha256_v1(body)
    return verdict


def _invalid_verdict(reasons: list[str]) -> dict[str, Any]:
    verdict: dict[str, Any] = {
        "schema_name": OFFLINE_EXPORT_MANIFEST_SCHEMA_NAME,
        "schema_version": OFFLINE_EXPORT_MANIFEST_SCHEMA_VERSION,
        "valid": False,
        "checked_artifacts": 0,
        "failures": [{"artifact_id": None, "reason_codes": reasons}],
        "warnings": [],
        "is_fem_validation": False,
        "validation_sha256": "",
    }
    body = {key: value for key, value in verdict.items() if key != "validation_sha256"}
    verdict["validation_sha256"] = canonical_sha256_v1(body)
    return verdict


__all__ = [
    "OFFLINE_EXPORT_MANIFEST_SCHEMA_NAME",
    "OFFLINE_EXPORT_MANIFEST_SCHEMA_VERSION",
    "OFFLINE_READABLE_FORMATS",
    "SUPPORTED_FORMATS",
    "OfflineExportError",
    "build_offline_export_manifest",
    "normalize_offline_export_manifest",
    "validate_offline_export_manifest",
]
