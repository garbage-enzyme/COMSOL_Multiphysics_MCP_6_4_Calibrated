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
from comsol_mcp.path_policy import PathPolicy, ReadPinError, pin_validated_reads

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


def _check_count_limits(
    artifacts: list[dict[str, Any]],
    *,
    max_expressions: int,
    max_parameter_entries: int,
    max_time_values: int,
) -> list[str]:
    """B05: reject count overruns before any artifact I/O."""
    for index, artifact in enumerate(artifacts):
        columns = artifact.get("columns") or []
        if len(columns) > max_expressions:
            return [f"artifact {index} expressions exceed limit {max_expressions}"]
        parameters = artifact.get("parameter_values") or {}
        if isinstance(parameters, Mapping) and len(parameters) > max_parameter_entries:
            return [f"artifact {index} parameter_values exceed limit {max_parameter_entries}"]
        time_values = artifact.get("time_values")
        if isinstance(time_values, list) and len(time_values) > max_time_values:
            return [f"artifact {index} time_values exceed limit {max_time_values}"]
    return []


def validate_offline_export_manifest(
    manifest_source: Mapping[str, Any] | bytes | str | Path,
    base_directory: str | Path | None = None,
    *,
    expected_model_sha256: str | None = None,
    max_manifest_bytes: int = 33_554_432,
    max_artifacts: int = 512,
    max_artifact_bytes: int = 17_179_869_184,
    max_expressions: int = 256,
    max_parameter_entries: int = 512,
    max_time_values: int = 65_536,
    path_policy: PathPolicy | None = None,
) -> dict[str, Any]:
    """Validate one manifest plus its artifacts entirely offline.

    B04 containment: the manifest file, the export base directory, and every
    artifact must lie inside the configured owned artifact root. Each file is
    held with a validated read pin for the duration of hashing so a mid-
    validation replacement cannot swap the bytes that were checked.

    B05: caller limits are applied to structure/counts before artifact bytes
    are read. B12: the verdict binds the normalized manifest hash, expected
    model identity, effective limits, and per-artifact size/hash evidence.
    """
    warnings: list[str] = []
    failures: list[dict[str, Any]] = []
    policy = path_policy or PathPolicy.from_environment()
    path_evidence: dict[str, Any] = {
        "enforced": True,
        "validated_input_count": 0,
        "validated_kinds": [],
        "root_ids": [],
    }
    effective_limits = {
        "max_manifest_bytes": max_manifest_bytes,
        "max_artifacts": max_artifacts,
        "max_artifact_bytes": max_artifact_bytes,
        "max_expressions": max_expressions,
        "max_parameter_entries": max_parameter_entries,
        "max_time_values": max_time_values,
    }

    def _record_path(kind: str, root_id: str) -> None:
        path_evidence["validated_input_count"] += 1
        if kind not in path_evidence["validated_kinds"]:
            path_evidence["validated_kinds"].append(kind)
        if root_id not in path_evidence["root_ids"]:
            path_evidence["root_ids"].append(root_id)

    def _finish(
        valid: bool,
        checked_artifacts: int,
        failure_list: list[dict[str, Any]],
        warning_list: list[str],
        *,
        manifest_sha256: str | None = None,
        artifact_checks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        verdict: dict[str, Any] = {
            "schema_name": OFFLINE_EXPORT_MANIFEST_SCHEMA_NAME,
            "schema_version": OFFLINE_EXPORT_MANIFEST_SCHEMA_VERSION,
            "valid": valid,
            "checked_artifacts": checked_artifacts,
            "failures": failure_list,
            "warnings": sorted(set(warning_list)),
            "is_fem_validation": False,
            "path_evidence": {
                **path_evidence,
                "validated_kinds": sorted(path_evidence["validated_kinds"]),
                "root_ids": sorted(path_evidence["root_ids"]),
            },
            # B12: bind the verdict to the exact inputs that were checked.
            "input_binding": {
                "manifest_sha256": manifest_sha256,
                "expected_model_sha256": (
                    expected_model_sha256.lower() if expected_model_sha256 else None
                ),
                "effective_limits": effective_limits,
                "artifact_checks": artifact_checks or [],
            },
            "validation_sha256": "",
        }
        body = {key: value for key, value in verdict.items() if key != "validation_sha256"}
        verdict["validation_sha256"] = canonical_sha256_v1(body)
        return verdict

    manifest_pin = None
    if isinstance(manifest_source, (str, Path)):
        try:
            decision = policy.validate_artifact_read(str(manifest_source))
        except ValueError as exc:
            # A missing file under the owned root is unavailable, not an escape.
            try:
                root = policy.artifact_write_root.resolve(strict=False)
                candidate = Path(manifest_source).expanduser()
                if not candidate.is_absolute():
                    candidate = Path.cwd() / candidate
                resolved = candidate.resolve(strict=False)
                under_root = resolved == root or root in resolved.parents
            except (OSError, RuntimeError, ValueError):
                under_root = False
            code = "manifest_unavailable" if under_root else "manifest_outside_allowed_root"
            rejected = _invalid_verdict([code])
            rejected["failures"][0]["detail"] = str(exc)[:160]
            rejected["path_evidence"] = path_evidence
            return rejected
        manifest_pin = decision.read_pin
        _record_path(decision.kind, decision.root_id)
        path = decision.normalized_path
        try:
            if manifest_pin is not None:
                with pin_validated_reads((manifest_pin,)):
                    raw = path.read_bytes()
            else:
                raw = path.read_bytes()
        except (OSError, ReadPinError, RuntimeError, ValueError):
            rejected = _invalid_verdict(["manifest_unavailable"])
            rejected["path_evidence"] = path_evidence
            return rejected
        base_directory = base_directory or path.parent
        if len(raw) > max_manifest_bytes:
            rejected = _invalid_verdict(["manifest_too_large"])
            rejected["path_evidence"] = path_evidence
            return rejected
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            rejected = _invalid_verdict(["manifest_not_valid_json"])
            rejected["path_evidence"] = path_evidence
            return rejected
    elif isinstance(manifest_source, (bytes, bytearray)):
        raw = bytes(manifest_source)
        if len(raw) > max_manifest_bytes:
            rejected = _invalid_verdict(["manifest_too_large"])
            rejected["path_evidence"] = path_evidence
            return rejected
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            rejected = _invalid_verdict(["manifest_not_valid_json"])
            rejected["path_evidence"] = path_evidence
            return rejected
        if base_directory is None:
            rejected = _invalid_verdict(["base_directory_undeclared"])
            rejected["path_evidence"] = path_evidence
            return rejected
    else:
        payload = manifest_source
        raw = None
        if base_directory is None:
            rejected = _invalid_verdict(["base_directory_undeclared"])
            rejected["path_evidence"] = path_evidence
            return rejected

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
        rejected["path_evidence"] = path_evidence
        return rejected

    # B12: hash the exact bytes that were normalized.
    if isinstance(manifest_source, (str, Path, bytes, bytearray)):
        manifest_bytes = raw if raw is not None else json.dumps(payload, sort_keys=True).encode("utf-8")
        manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    else:
        manifest_digest = canonical_sha256_v1(manifest)

    # B05: count limits before any artifact filesystem work.
    if len(manifest["artifacts"]) > max_artifacts:
        failures.append({"artifact_id": None, "reason_codes": ["too_many_artifacts"]})
    count_errors = _check_count_limits(
        manifest["artifacts"],
        max_expressions=max_expressions,
        max_parameter_entries=max_parameter_entries,
        max_time_values=max_time_values,
    )
    for message in count_errors:
        failures.append({"artifact_id": None, "reason_codes": ["limit_exceeded"], "detail": message[:160]})
    if failures:
        return _finish(
            False,
            0,
            failures,
            warnings,
            manifest_sha256=manifest_digest,
            artifact_checks=[],
        )

    source_hash = manifest["source_identity"]["model_sha256"]
    if expected_model_sha256 is not None:
        if source_hash is None:
            failures.append({"artifact_id": None, "reason_codes": ["model_identity_undeclared"]})
        elif expected_model_sha256.lower() != source_hash:
            failures.append({"artifact_id": None, "reason_codes": ["model_identity_mismatch"]})
    elif source_hash is None:
        warnings.append("model_hash_undeclared")

    try:
        base_decision = policy.validate_artifact_read_root(str(base_directory))
    except ValueError as exc:
        rejected = _invalid_verdict(["base_directory_outside_allowed_root"])
        rejected["failures"][0]["detail"] = str(exc)[:160]
        rejected["path_evidence"] = path_evidence
        return rejected
    base = base_decision.normalized_path
    _record_path(base_decision.kind, base_decision.root_id)

    artifact_checks: list[dict[str, Any]] = []
    for artifact in manifest["artifacts"]:
        reasons: list[str] = []
        candidate = base / artifact["relative_path"]
        target = candidate
        pin = None
        try:
            target_decision = policy.validate_artifact_read(str(candidate))
            target = target_decision.normalized_path
            try:
                target.relative_to(base)
            except ValueError:
                reasons.append("path_escape")
            else:
                _record_path(target_decision.kind, target_decision.root_id)
                pin = target_decision.read_pin
        except ValueError as exc:
            # Missing files under the export base are missing, not escapes.
            try:
                resolved = candidate.resolve(strict=False)
                under_base = resolved == base or base in resolved.parents
            except (OSError, RuntimeError, ValueError):
                under_base = False
            if under_base and not candidate.exists():
                reasons.append("missing_file")
            else:
                reasons.append("path_escape")
            _ = exc
        check_record: dict[str, Any] = {
            "artifact_id": artifact["artifact_id"],
            "declared_byte_count": artifact["byte_count"],
            "declared_sha256": artifact["sha256"],
            "observed_byte_count": None,
            "observed_sha256": None,
        }
        if not reasons:
            if not target.is_file():
                reasons.append("missing_file")
            else:
                size = target.stat().st_size
                check_record["observed_byte_count"] = size
                if size > max_artifact_bytes:
                    reasons.append("artifact_over_declared_limit")
                else:
                    try:
                        if pin is not None:
                            with pin_validated_reads((pin,)):
                                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                                size_after = target.stat().st_size
                        else:
                            digest = hashlib.sha256(target.read_bytes()).hexdigest()
                            size_after = size
                    except (OSError, RuntimeError, ValueError):
                        reasons.append("artifact_unreadable")
                        failures.append(
                            {"artifact_id": artifact["artifact_id"], "reason_codes": reasons}
                        )
                        artifact_checks.append(check_record)
                        continue
                    check_record["observed_sha256"] = digest
                    if size_after != artifact["byte_count"] or size != artifact["byte_count"]:
                        reasons.append("byte_count_mismatch")
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
        artifact_checks.append(check_record)

    return _finish(
        not failures,
        len(manifest["artifacts"]),
        failures,
        warnings,
        manifest_sha256=manifest_digest,
        artifact_checks=artifact_checks,
    )


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
