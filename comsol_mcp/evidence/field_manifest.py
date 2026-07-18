"""Versioned, solver-free manifests for bounded field-evidence artifacts."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import PurePosixPath
import re
from typing import Any, Mapping

from .field_bundle import validate_field_evidence_request


FIELD_EVIDENCE_MANIFEST_SCHEMA = "comsol_mcp.field_evidence_manifest"
FIELD_EVIDENCE_MANIFEST_VERSION = "1.0.0"
MAX_FIELD_MANIFEST_BYTES = 256 * 1024
MAX_ARTIFACT_PATH = 512

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("field manifest must contain only finite JSON values") from exc


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise ValueError(f"{label} must be an object with string keys")
    return dict(value)


def _exact(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    unknown = sorted(set(value) - fields)
    missing = sorted(fields - set(value))
    if unknown or missing:
        raise ValueError(
            f"{label} has unsupported fields {unknown} or missing fields {missing}"
        )


def _finite(value: object, label: str, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number) or (nonnegative and number < 0):
        qualifier = "finite and nonnegative" if nonnegative else "finite"
        raise ValueError(f"{label} must be {qualifier}")
    return number


def _count(value: object, label: str, *, allow_zero: bool = True) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer greater than or equal to {minimum}")
    return value


def _hash(value: object, label: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value.lower()):
        raise ValueError(f"{label} must be exactly 64 hexadecimal characters")
    return value.lower()


def _relative_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_ARTIFACT_PATH:
        raise ValueError(f"{label} must be a bounded nonempty relative path")
    text = value.strip().replace("\\", "/")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or _WINDOWS_DRIVE.match(text):
        raise ValueError(f"{label} must be relative and traversal-free")
    return text


def _artifact(
    value: object,
    label: str,
    *,
    expected_id: str,
    expected_media_type: str,
) -> dict[str, Any]:
    raw = _mapping(value, label)
    fields = {"artifact_id", "relative_path", "media_type", "sha256", "byte_count"}
    _exact(raw, fields, label)
    if raw["artifact_id"] != expected_id:
        raise ValueError(f"{label}.artifact_id does not match the field request")
    if raw["media_type"] != expected_media_type:
        raise ValueError(f"{label}.media_type must be {expected_media_type}")
    return {
        "artifact_id": expected_id,
        "relative_path": _relative_path(raw["relative_path"], f"{label}.relative_path"),
        "media_type": expected_media_type,
        "sha256": _hash(raw["sha256"], f"{label}.sha256"),
        "byte_count": _count(raw["byte_count"], f"{label}.byte_count", allow_zero=False),
    }


def _coordinate_ranges(value: object, request: Mapping[str, Any]) -> dict[str, Any]:
    raw = _mapping(value, "coordinate_ranges")
    fields = {"x", "y", "z", "unit"}
    _exact(raw, fields, "coordinate_ranges")
    if raw["unit"] != request["coordinate_bounds"]["unit"]:
        raise ValueError("coordinate_ranges.unit must match the request")
    result: dict[str, Any] = {"unit": raw["unit"]}
    for axis in ("x", "y", "z"):
        pair = raw[axis]
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError(f"coordinate_ranges.{axis} must contain two finite values")
        normalized = [
            _finite(item, f"coordinate_ranges.{axis}[{index}]")
            for index, item in enumerate(pair)
        ]
        if normalized[0] > normalized[1]:
            raise ValueError(f"coordinate_ranges.{axis} must be ordered")
        requested = request["coordinate_bounds"][axis]
        if normalized[0] < requested[0] or normalized[1] > requested[1]:
            raise ValueError(f"coordinate_ranges.{axis} escapes the requested bounds")
        result[axis] = normalized
    return result


def _quantity_summaries(
    value: object,
    request: Mapping[str, Any],
    grid_points: int,
    global_missing: int,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("quantity_summaries must be a list")
    expected = {item["name"]: item for item in request["expressions"]}
    if len(value) != len(expected):
        raise ValueError("quantity_summaries must describe every requested expression")
    fields = {"name", "unit", "minimum", "maximum", "rms", "finite_count", "missing_count"}
    by_name: dict[str, dict[str, Any]] = {}
    for index, value_item in enumerate(value):
        label = f"quantity_summaries[{index}]"
        raw = _mapping(value_item, label)
        _exact(raw, fields, label)
        name = raw["name"]
        if name not in expected or name in by_name:
            raise ValueError("quantity_summaries contain duplicate or unrequested names")
        if raw["unit"] != expected[name]["unit"]:
            raise ValueError(f"{label}.unit does not match the requested expression")
        minimum = _finite(raw["minimum"], f"{label}.minimum")
        maximum = _finite(raw["maximum"], f"{label}.maximum")
        if minimum > maximum:
            raise ValueError(f"{label}.minimum must not exceed maximum")
        finite_count = _count(raw["finite_count"], f"{label}.finite_count")
        missing_count = _count(raw["missing_count"], f"{label}.missing_count")
        if finite_count + missing_count != grid_points:
            raise ValueError(f"{label} counts must equal the exact grid size")
        if missing_count != global_missing:
            raise ValueError(f"{label}.missing_count must match the manifest coverage")
        by_name[name] = {
            "name": name,
            "unit": raw["unit"],
            "minimum": minimum,
            "maximum": maximum,
            "rms": _finite(raw["rms"], f"{label}.rms", nonnegative=True),
            "finite_count": finite_count,
            "missing_count": missing_count,
        }
    if set(by_name) != set(expected):
        raise ValueError("quantity_summaries must describe every requested expression")
    return [by_name[item["name"]] for item in request["expressions"]]


def _assemble_manifest(
    *,
    request: Mapping[str, Any],
    view: Mapping[str, Any],
    raw_point_count: object,
    selected_point_count: object,
    unique_point_count: object | None,
    collapsed_duplicate_point_count: object | None,
    covered_grid_point_count: object,
    missing_grid_point_count: object,
    coordinate_ranges: object,
    quantity_summaries: object,
    array_artifact: object,
    png_artifact: object | None,
) -> dict[str, Any]:
    limits = request["limits"]
    raw_count = _count(raw_point_count, "raw_point_count", allow_zero=False)
    if raw_count > limits["max_raw_points"]:
        raise ValueError("raw_point_count exceeds the caller-declared limit")
    selected_count = _count(selected_point_count, "selected_point_count", allow_zero=False)
    if selected_count > raw_count:
        raise ValueError("selected_point_count must not exceed raw_point_count")
    unique_count = (
        selected_count
        if unique_point_count is None
        else _count(unique_point_count, "unique_point_count", allow_zero=False)
    )
    collapsed_count = (
        0
        if collapsed_duplicate_point_count is None
        else _count(
            collapsed_duplicate_point_count,
            "collapsed_duplicate_point_count",
        )
    )
    if unique_count + collapsed_count != selected_count:
        raise ValueError(
            "unique and collapsed duplicate point counts must equal selected_point_count"
        )
    covered_count = _count(covered_grid_point_count, "covered_grid_point_count")
    missing_count = _count(missing_grid_point_count, "missing_grid_point_count")
    grid_count = request["grid_point_count"]
    if covered_count + missing_count != grid_count:
        raise ValueError("covered and missing grid counts must equal the exact grid size")

    outputs = view["outputs"]
    array = _artifact(
        array_artifact,
        "array_artifact",
        expected_id=outputs["array_artifact_id"],
        expected_media_type="application/x-npz",
    )
    png_expected = outputs["png_artifact_id"]
    if (png_artifact is None) != (png_expected is None):
        raise ValueError("png_artifact presence must match the field request")
    png = (
        _artifact(
            png_artifact,
            "png_artifact",
            expected_id=png_expected,
            expected_media_type="image/png",
        )
        if png_expected is not None
        else None
    )
    total_artifact_bytes = array["byte_count"] + (png["byte_count"] if png else 0)
    if total_artifact_bytes > limits["max_artifact_bytes"]:
        raise ValueError("field artifacts exceed the caller-declared byte limit")

    manifest = {
        "schema_name": FIELD_EVIDENCE_MANIFEST_SCHEMA,
        "schema_version": FIELD_EVIDENCE_MANIFEST_VERSION,
        "manifest_artifact_id": outputs["manifest_artifact_id"],
        "request_id": request["request_id"],
        "request_fingerprint": request["request_fingerprint"],
        "configuration_sha256": request["configuration_sha256"],
        "view_id": view["view_id"],
        "view_fingerprint": view["view_fingerprint"],
        "source": deepcopy(view["source"]),
        "wavelength_m": view["wavelength_m"],
        "expressions": deepcopy(request["expressions"]),
        "slice": deepcopy(request["slice"]),
        "coordinate_ranges": _coordinate_ranges(coordinate_ranges, request),
        "grid": deepcopy(request["grid"]),
        "raw_point_count": raw_count,
        "selected_point_count": selected_count,
        "unique_point_count": unique_count,
        "collapsed_duplicate_point_count": collapsed_count,
        "grid_point_count": grid_count,
        "covered_grid_point_count": covered_count,
        "missing_grid_point_count": missing_count,
        "coverage_fraction": covered_count / grid_count,
        "quantity_summaries": _quantity_summaries(
            quantity_summaries, request, grid_count, missing_count
        ),
        "artifacts": {"array": array, "png": png},
        "artifact_byte_count": total_artifact_bytes,
        "visual_review_state": "visual_review_required",
        "semantic_mode_label": "not_assigned",
        "measurement_status": "measurement_complete" if missing_count == 0 else "partial",
    }
    if len(_canonical_bytes(manifest)) > MAX_FIELD_MANIFEST_BYTES:
        raise ValueError(f"field manifest exceeds {MAX_FIELD_MANIFEST_BYTES} bytes")
    manifest["manifest_sha256"] = _fingerprint(manifest)
    return manifest


def build_field_evidence_manifest(
    *,
    request: object,
    view_id: str,
    raw_point_count: int,
    selected_point_count: int,
    unique_point_count: int | None = None,
    collapsed_duplicate_point_count: int | None = None,
    covered_grid_point_count: int,
    missing_grid_point_count: int,
    coordinate_ranges: object,
    quantity_summaries: object,
    array_artifact: object,
    png_artifact: object | None = None,
) -> dict[str, Any]:
    """Build a compact manifest that never embeds field arrays."""
    normalized_request = validate_field_evidence_request(request)
    matches = [view for view in normalized_request["views"] if view["view_id"] == view_id]
    if len(matches) != 1:
        raise ValueError("view_id must identify exactly one requested view")
    return _assemble_manifest(
        request=normalized_request,
        view=matches[0],
        raw_point_count=raw_point_count,
        selected_point_count=selected_point_count,
        unique_point_count=unique_point_count,
        collapsed_duplicate_point_count=collapsed_duplicate_point_count,
        covered_grid_point_count=covered_grid_point_count,
        missing_grid_point_count=missing_grid_point_count,
        coordinate_ranges=coordinate_ranges,
        quantity_summaries=quantity_summaries,
        array_artifact=array_artifact,
        png_artifact=png_artifact,
    )


def validate_field_evidence_manifest(value: object, *, request: object) -> dict[str, Any]:
    """Validate a transported manifest against its immutable request."""
    item = _mapping(value, "field_evidence_manifest")
    fields = {
        "schema_name", "schema_version", "manifest_artifact_id", "request_id",
        "request_fingerprint", "configuration_sha256", "view_id",
        "view_fingerprint", "source", "wavelength_m", "expressions", "slice",
        "coordinate_ranges", "grid", "raw_point_count", "selected_point_count",
        "unique_point_count", "collapsed_duplicate_point_count", "grid_point_count",
        "covered_grid_point_count", "missing_grid_point_count",
        "coverage_fraction", "quantity_summaries", "artifacts",
        "artifact_byte_count", "visual_review_state", "semantic_mode_label",
        "measurement_status", "manifest_sha256",
    }
    _exact(item, fields, "field_evidence_manifest")
    if item["schema_name"] != FIELD_EVIDENCE_MANIFEST_SCHEMA or item[
        "schema_version"
    ] != FIELD_EVIDENCE_MANIFEST_VERSION:
        raise ValueError("field_evidence_manifest schema is unsupported")
    supplied = _hash(item["manifest_sha256"], "manifest_sha256")
    unhashed = dict(item)
    unhashed.pop("manifest_sha256")
    if supplied != _fingerprint(unhashed):
        raise ValueError("field_evidence_manifest hash does not match")

    normalized_request = validate_field_evidence_request(request)
    if item["request_fingerprint"] != normalized_request["request_fingerprint"]:
        raise ValueError("field_evidence_manifest request fingerprint does not match")
    matches = [
        view for view in normalized_request["views"] if view["view_id"] == item["view_id"]
    ]
    if len(matches) != 1:
        raise ValueError("field_evidence_manifest view is not present in the request")
    artifacts = _mapping(item["artifacts"], "artifacts")
    _exact(artifacts, {"array", "png"}, "artifacts")
    rebuilt = _assemble_manifest(
        request=normalized_request,
        view=matches[0],
        raw_point_count=item["raw_point_count"],
        selected_point_count=item["selected_point_count"],
        unique_point_count=item["unique_point_count"],
        collapsed_duplicate_point_count=item["collapsed_duplicate_point_count"],
        covered_grid_point_count=item["covered_grid_point_count"],
        missing_grid_point_count=item["missing_grid_point_count"],
        coordinate_ranges=item["coordinate_ranges"],
        quantity_summaries=item["quantity_summaries"],
        array_artifact=artifacts["array"],
        png_artifact=artifacts["png"],
    )
    if rebuilt != item:
        raise ValueError("field_evidence_manifest is not canonical or was modified")
    return deepcopy(rebuilt)


__all__ = [
    "FIELD_EVIDENCE_MANIFEST_SCHEMA",
    "FIELD_EVIDENCE_MANIFEST_VERSION",
    "MAX_FIELD_MANIFEST_BYTES",
    "build_field_evidence_manifest",
    "validate_field_evidence_manifest",
]
