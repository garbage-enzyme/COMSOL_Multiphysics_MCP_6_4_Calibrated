"""Solver-free surrogate export, consistency, and registry integration.

This module never imports COMSOL, Java, MPh, or network clients.  It hashes an
exported artifact produced elsewhere, binds that hash into a model card, proves
a re-imported prediction agrees with the in-model prediction within a declared
tolerance, and routes the result through the OOD and registry contracts.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from comsol_mcp.durable.canonical import canonical_sha256_v1

SCHEMA_VERSION = "1.0.0"

# Export formats this project can bind.  COMSOL's DNN `export(path)` writes ONNX
# protobuf regardless of the filename extension: exporting to "model.txt" and
# "model.onnx" produces byte-identical ONNX payloads.  There is therefore no
# separate internal text format, and ONNX is the required export path rather
# than an optional extra.
EXPORT_FORMATS = ("onnx",)
REQUIRED_EXPORT_FORMATS = ("onnx",)

CONSISTENCY_STATES = ("consistent", "inconsistent", "unavailable")

MAX_EXPORT_BYTES = 2 * 1024 * 1024 * 1024
MAX_ROWS = 4096


def _require_str(name: str, value: Any, *, max_len: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise ValueError(f"{name} must be a non-empty string up to {max_len}")
    return value


def _require_hex64(name: str, value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{name} must be a 64-character hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{name} must be a 64-character hex digest") from exc
    return value.lower()


def _require_finite(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _matrix(
    rows: Sequence[Sequence[float]], *, name: str, width: int | None = None
) -> list[list[float]]:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or not rows:
        raise ValueError(f"{name} must be a non-empty sequence")
    if len(rows) > MAX_ROWS:
        raise ValueError(f"{name} exceeds the {MAX_ROWS} row limit")
    normalized: list[list[float]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
            raise ValueError(f"{name}[{index}] must be a sequence")
        values = [_require_finite(f"{name}[{index}]", item) for item in row]
        if not values:
            raise ValueError(f"{name}[{index}] must be non-empty")
        normalized.append(values)
    width = width if width is not None else len(normalized[0])
    if any(len(row) != width for row in normalized):
        raise ValueError(f"{name} rows must share width {width}")
    return normalized


def hash_export_artifact(
    *, path: str | Path, export_format: str, chunk_bytes: int = 1024 * 1024
) -> dict[str, Any]:
    """Hash an exported artifact by streaming it.

    ``exportfilename`` is not readable from COMSOL, so the produced file itself
    is the only trustworthy export evidence; its content hash is what a model
    card binds.
    """
    if export_format not in EXPORT_FORMATS:
        raise ValueError(f"export_format must be one of: {', '.join(EXPORT_FORMATS)}")
    if not 4096 <= chunk_bytes <= 64 * 1024 * 1024:
        raise ValueError("chunk_bytes must be in 4096..67108864")
    resolved = Path(path)
    if not resolved.is_file():
        raise ValueError(f"export artifact not found: {resolved}")
    size = resolved.stat().st_size
    if size <= 0:
        raise ValueError("export artifact is empty")
    if size > MAX_EXPORT_BYTES:
        raise ValueError(f"export artifact exceeds {MAX_EXPORT_BYTES} bytes")
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk_bytes), b""):
            digest.update(block)
    return {
        "export_format": export_format,
        "artifact_name": resolved.name,
        "size_bytes": size,
        "sha256": digest.hexdigest(),
        "hashed_by": "streaming_content_hash",
        "trusted_property_filename": False,
    }


def build_export_manifest(
    *,
    export_id: str,
    trained_chksum: str,
    architecture_sha256: str,
    artifacts: Sequence[Mapping[str, Any]],
    required_formats: Sequence[str] = REQUIRED_EXPORT_FORMATS,
) -> dict[str, Any]:
    """Bind the exported artifacts and declare which formats are unavailable.

    A missing required format fails the manifest.  A missing *optional* format
    (ONNX) is recorded as unavailable rather than being silently omitted or
    reported as present.
    """
    if not isinstance(artifacts, Sequence) or isinstance(artifacts, (str, bytes)):
        raise ValueError("artifacts must be a sequence")
    if not artifacts:
        raise ValueError("artifacts must be non-empty")
    for required in required_formats:
        if required not in EXPORT_FORMATS:
            raise ValueError(f"required format is not supported: {required}")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, Mapping):
            raise ValueError(f"artifacts[{index}] must be a mapping")
        # Annotated so the numeric comparison below is checked as an int rather
        # than against an inferred ``object``.
        record: dict[str, Any] = {
            "export_format": _require_str(
                f"artifacts[{index}].export_format", artifact.get("export_format"), max_len=32
            ),
            "artifact_name": _require_str(
                f"artifacts[{index}].artifact_name", artifact.get("artifact_name")
            ),
            "size_bytes": int(artifact.get("size_bytes", 0)),
            "sha256": _require_hex64(f"artifacts[{index}].sha256", artifact.get("sha256")),
        }
        if record["export_format"] not in EXPORT_FORMATS:
            raise ValueError(f"artifacts[{index}] has an unsupported format")
        if record["size_bytes"] <= 0:
            raise ValueError(f"artifacts[{index}] must be non-empty")
        if record["export_format"] in seen:
            raise ValueError(f"duplicate export format: {record['export_format']}")
        seen.add(record["export_format"])
        normalized.append(record)
    normalized.sort(key=lambda item: item["export_format"])

    present = {item["export_format"] for item in normalized}
    unavailable = sorted(set(EXPORT_FORMATS) - present)
    missing_required = sorted(set(required_formats) - present)
    if missing_required:
        raise ValueError(f"missing required export formats: {', '.join(missing_required)}")

    body = {
        "schema": "comsol_mcp.surrogate_export_manifest",
        "schema_version": SCHEMA_VERSION,
        "export_id": _require_str("export_id", export_id),
        "trained_chksum": _require_str("trained_chksum", trained_chksum, max_len=64),
        "architecture_sha256": _require_hex64("architecture_sha256", architecture_sha256),
        "artifacts": normalized,
        "required_formats": sorted(required_formats),
        "unavailable_formats": unavailable,
        "onnx_available": "onnx" in present,
        "unavailable_is_failure": False,
        "is_surrogate_prediction": False,
        "upgrades_fem_evidence": False,
    }
    return {**body, "manifest_sha256": canonical_sha256_v1(body)}


def validate_export_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("export manifest must be a mapping")
    required = (
        "schema",
        "schema_version",
        "export_id",
        "trained_chksum",
        "architecture_sha256",
        "artifacts",
        "required_formats",
        "unavailable_formats",
        "onnx_available",
        "unavailable_is_failure",
        "is_surrogate_prediction",
        "upgrades_fem_evidence",
    )
    if set(value) != set(required) | {"manifest_sha256"}:
        raise ValueError("export manifest keys are closed")
    if value["schema"] != "comsol_mcp.surrogate_export_manifest":
        raise ValueError("unexpected schema name")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported schema_version")
    body = {key: value[key] for key in required}
    if value["manifest_sha256"] != canonical_sha256_v1(body):
        raise ValueError("manifest_sha256 mismatch")
    if value["is_surrogate_prediction"] is not False:
        raise ValueError("an export manifest is not a prediction")
    if value["upgrades_fem_evidence"] is not False:
        raise ValueError("an export manifest never upgrades FEM evidence")
    present = {item["export_format"] for item in value["artifacts"]}
    if value["onnx_available"] != ("onnx" in present):
        raise ValueError("onnx_available disagrees with the artifact list")
    if value["unavailable_formats"] != sorted(set(EXPORT_FORMATS) - present):
        raise ValueError("unavailable_formats disagrees with the artifact list")
    missing_required = set(value["required_formats"]) - present
    if missing_required:
        raise ValueError(f"missing required export formats: {sorted(missing_required)}")
    return dict(value)


def check_prediction_consistency(
    *,
    in_model_predictions: Sequence[Sequence[float]],
    reimported_predictions: Sequence[Sequence[float]],
    absolute_tolerance: float,
    relative_tolerance: float = 0.0,
) -> dict[str, Any]:
    """Compare an in-model prediction with a re-imported artifact prediction.

    A re-imported artifact that disagrees with the model means the exported
    model is not the trained model, so the result is `inconsistent` and the
    export must not be registered.
    """
    absolute_tolerance = _require_finite("absolute_tolerance", absolute_tolerance)
    relative_tolerance = _require_finite("relative_tolerance", relative_tolerance)
    if absolute_tolerance < 0.0 or relative_tolerance < 0.0:
        raise ValueError("tolerances must be non-negative")

    in_model = _matrix(in_model_predictions, name="in_model_predictions")
    reimported = _matrix(
        reimported_predictions, name="reimported_predictions", width=len(in_model[0])
    )
    if len(in_model) != len(reimported):
        raise ValueError("prediction sets must have the same row count")

    worst_absolute = 0.0
    worst_relative: float | None = 0.0
    violating: list[int] = []
    for index in range(len(in_model)):
        for dimension in range(len(in_model[0])):
            expected = in_model[index][dimension]
            observed = reimported[index][dimension]
            difference = abs(expected - observed)
            worst_absolute = max(worst_absolute, difference)
            if expected != 0.0:
                ratio = difference / abs(expected)
                worst_relative = max(worst_relative or 0.0, ratio)
            else:
                worst_relative = None
            within_absolute = difference <= absolute_tolerance
            within_relative = expected != 0.0 and difference <= relative_tolerance * abs(expected)
            if not (within_absolute or within_relative):
                violating.append(index)
                break

    consistent = not violating
    return {
        "state": "consistent" if consistent else "inconsistent",
        "row_count": len(in_model),
        "worst_absolute_difference": worst_absolute,
        "worst_relative_difference": worst_relative,
        "absolute_tolerance": absolute_tolerance,
        "relative_tolerance": relative_tolerance,
        "violating_rows": violating,
        "consistent": consistent,
        "registration_allowed": consistent,
        "is_surrogate_prediction": True,
        "upgrades_fem_evidence": False,
    }


def evaluate_export_availability(*, onnx_export_error: str | None) -> dict[str, Any]:
    """Record ONNX export availability from a real attempt.

    ONNX is the only format COMSOL's DNN ``export(path)`` produces, so an
    unavailable ONNX export is a genuine failure: it blocks registration rather
    than being reported as an acceptable omission.
    """
    if onnx_export_error is None:
        return {
            "format": "onnx",
            "state": "available",
            "error": None,
            "required": True,
            "blocks_registration": False,
        }
    return {
        "format": "onnx",
        "state": "unavailable",
        "error": _require_str("onnx_export_error", onnx_export_error, max_len=1024),
        "required": True,
        "blocks_registration": True,
        "recorded_as_unavailable_not_passed": True,
    }


def integrate_export_into_registry(
    *,
    export_manifest: Mapping[str, Any],
    consistency: Mapping[str, Any],
    ood_state: str,
    escalation_required: bool,
) -> dict[str, Any]:
    """Route an export through the OOD and registry decision.

    Registration requires a consistent artifact.  An inconsistent export is
    refused, and an out-of-domain state still demands fresh FEM escalation.
    """
    manifest = validate_export_manifest(export_manifest)
    if consistency.get("state") not in CONSISTENCY_STATES:
        raise ValueError(f"consistency state must be one of: {', '.join(CONSISTENCY_STATES)}")
    if ood_state not in ("in_domain", "edge", "out_of_domain", "uncalibrated"):
        raise ValueError("unknown ood_state")
    if consistency.get("state") == "inconsistent":
        return {
            "registered": False,
            "reason_code": "export_inconsistent_with_model",
            "escalation_required": True,
            "export_manifest_sha256": manifest["manifest_sha256"],
            "ood_state": ood_state,
            "is_surrogate_prediction": True,
            "upgrades_fem_evidence": False,
        }
    if consistency.get("state") == "unavailable":
        return {
            "registered": False,
            "reason_code": "consistency_unavailable",
            "escalation_required": True,
            "export_manifest_sha256": manifest["manifest_sha256"],
            "ood_state": ood_state,
            "is_surrogate_prediction": True,
            "upgrades_fem_evidence": False,
        }
    return {
        "registered": True,
        "reason_code": "export_consistent_and_in_domain"
        if ood_state == "in_domain"
        else "export_consistent_outside_domain",
        "escalation_required": bool(escalation_required) or ood_state != "in_domain",
        "export_manifest_sha256": manifest["manifest_sha256"],
        "ood_state": ood_state,
        "onnx_available": manifest["onnx_available"],
        "is_surrogate_prediction": True,
        "upgrades_fem_evidence": False,
    }


__all__ = [
    "CONSISTENCY_STATES",
    "EXPORT_FORMATS",
    "MAX_EXPORT_BYTES",
    "REQUIRED_EXPORT_FORMATS",
    "SCHEMA_VERSION",
    "build_export_manifest",
    "check_prediction_consistency",
    "evaluate_export_availability",
    "hash_export_artifact",
    "integrate_export_into_registry",
    "validate_export_manifest",
]
