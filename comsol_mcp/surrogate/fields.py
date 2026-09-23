"""Closed solver-free feature, target, and transform contracts.

This module never imports COMSOL, Java, MPh, or network clients.  It freezes
field ordering, units, bounds, support coordinates, and training-only transform
fitting so that a surrogate target cannot be interpreted against an unverified
schema.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from comsol_mcp.durable.canonical import canonical_sha256_v1

SCHEMA_VERSION = "1.0.0"
MAX_FIELDS = 32
TRANSFORM_KINDS = ("identity", "standardize", "minmax")
_MISSING_POLICIES = ("refuse",)
_CLIP_POLICIES = ("refuse",)


def _require_str(name: str, value: Any, *, max_len: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise ValueError(f"{name} must be a non-empty string up to {max_len}")
    return value


def _require_finite(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _require_closed(mapping: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ValueError(f"{label} has unknown fields: {', '.join(unknown)}")


def _finalize(body: dict[str, Any]) -> dict[str, Any]:
    return {**body, "manifest_sha256": canonical_sha256_v1(body)}


def _validate_sealed(manifest: Mapping[str, Any], required: Sequence[str]) -> dict[str, Any]:
    if set(manifest) != set(required) | {"manifest_sha256"}:
        raise ValueError("manifest keys are closed")
    body = {key: manifest[key] for key in required}
    if manifest["manifest_sha256"] != canonical_sha256_v1(body):
        raise ValueError("manifest_sha256 mismatch")
    return dict(manifest)


def _normalize_field(name: str, raw: Any, *, is_target: bool) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{name} must be a mapping")
    _require_closed(
        raw,
        {"field_id", "unit", "lower", "upper", "missing_policy", "clip_policy", "support"},
        name,
    )
    field_id = _require_str(f"{name}.field_id", raw.get("field_id"))
    unit = _require_str(f"{name}.unit", raw.get("unit"), max_len=64)
    lower = _require_finite(f"{name}.lower", raw.get("lower"))
    upper = _require_finite(f"{name}.upper", raw.get("upper"))
    if not lower < upper:
        raise ValueError(f"{name} requires lower < upper")
    missing_policy = raw.get("missing_policy", "refuse")
    if missing_policy not in _MISSING_POLICIES:
        raise ValueError(f"{name}.missing_policy must be refuse")
    clip_policy = raw.get("clip_policy", "refuse")
    if clip_policy not in _CLIP_POLICIES:
        raise ValueError(f"{name}.clip_policy must be refuse")
    entry: dict[str, Any] = {
        "field_id": field_id,
        "unit": unit,
        "lower": lower,
        "upper": upper,
        "missing_policy": missing_policy,
        "clip_policy": clip_policy,
    }
    if is_target:
        support = raw.get("support")
        if not isinstance(support, Mapping):
            raise ValueError(f"{name}.support must be a mapping for a target")
        _require_closed(support, {"coordinates", "unit"}, f"{name}.support")
        coordinates = support.get("coordinates")
        if not isinstance(coordinates, Sequence) or isinstance(coordinates, (str, bytes)):
            raise ValueError(f"{name}.support.coordinates must be a sequence")
        if not coordinates:
            raise ValueError(f"{name}.support.coordinates must be non-empty")
        entry["support"] = {
            "coordinates": [
                _require_finite(f"{name}.support.coordinates", item) for item in coordinates
            ],
            "unit": _require_str(f"{name}.support.unit", support.get("unit"), max_len=64),
        }
    elif "support" in raw:
        raise ValueError(f"{name}.support is only valid for a target")
    return entry


def build_field_schema(
    *,
    schema_id: str,
    features: Sequence[Mapping[str, Any]],
    targets: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Freeze ordered feature and target declarations with units and bounds."""
    if not 2 <= len(features) <= 12:
        raise ValueError("features must contain 2-12 entries")
    if not 1 <= len(targets) <= 8:
        raise ValueError("targets must contain 1-8 entries")
    if len(features) > MAX_FIELDS:
        raise ValueError("features exceed the field limit")
    normalized_features = [
        _normalize_field(f"features[{index}]", raw, is_target=False)
        for index, raw in enumerate(features)
    ]
    normalized_targets = [
        _normalize_field(f"targets[{index}]", raw, is_target=True)
        for index, raw in enumerate(targets)
    ]
    feature_ids = [item["field_id"] for item in normalized_features]
    target_ids = [item["field_id"] for item in normalized_targets]
    if len(set(feature_ids)) != len(feature_ids):
        raise ValueError("feature field_id values must be unique")
    if len(set(target_ids)) != len(target_ids):
        raise ValueError("target field_id values must be unique")
    if set(feature_ids) & set(target_ids):
        raise ValueError("feature and target field_id values must not overlap")
    body = {
        "schema": "comsol_mcp.surrogate_field_schema",
        "schema_version": SCHEMA_VERSION,
        "schema_id": _require_str("schema_id", schema_id),
        "feature_order": feature_ids,
        "target_order": target_ids,
        "features": normalized_features,
        "targets": normalized_targets,
        "input_dim": len(normalized_features),
        "target_dim": len(normalized_targets),
        "continuous_only": True,
    }
    return _finalize(body)


def validate_field_schema(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("field schema must be a mapping")
    manifest = _validate_sealed(
        value,
        (
            "schema",
            "schema_version",
            "schema_id",
            "feature_order",
            "target_order",
            "features",
            "targets",
            "input_dim",
            "target_dim",
            "continuous_only",
        ),
    )
    if manifest["schema"] != "comsol_mcp.surrogate_field_schema":
        raise ValueError("unexpected schema name")
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported schema_version")
    if manifest["continuous_only"] is not True:
        raise ValueError("the first target lane requires continuous_only")
    rebuilt = build_field_schema(
        schema_id=manifest["schema_id"],
        features=manifest["features"],
        targets=manifest["targets"],
    )
    if rebuilt != manifest:
        raise ValueError("field schema content is not canonical")
    return manifest


def fit_transforms(
    *,
    transform_id: str,
    schema: Mapping[str, Any],
    train_rows: Sequence[Mapping[str, float]],
    kinds: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Fit transforms from the training split only, then freeze their parameters.

    Every transform is fitted exclusively on ``train_rows``; validation, test,
    and scientific-holdout values never influence the fitted parameters.
    """
    validated = validate_field_schema(schema)
    if not train_rows:
        raise ValueError("train_rows must be non-empty")
    kinds = dict(kinds or {})
    field_ids = list(validated["feature_order"]) + list(validated["target_order"])
    unknown = sorted(set(kinds) - set(field_ids))
    if unknown:
        raise ValueError(f"transform kinds reference unknown fields: {', '.join(unknown)}")

    fitted: list[dict[str, Any]] = []
    for field in [*validated["features"], *validated["targets"]]:
        field_id = field["field_id"]
        kind = kinds.get(field_id, "identity")
        if kind not in TRANSFORM_KINDS:
            raise ValueError(f"unsupported transform kind for {field_id}")
        values: list[float] = []
        for index, row in enumerate(train_rows):
            if not isinstance(row, Mapping) or field_id not in row:
                raise ValueError(f"train_rows[{index}] is missing field {field_id}")
            values.append(_require_finite(f"train_rows[{index}].{field_id}", row[field_id]))
        entry: dict[str, Any] = {"field_id": field_id, "kind": kind}
        if kind == "standardize":
            mean = sum(values) / len(values)
            variance = sum((item - mean) ** 2 for item in values) / len(values)
            scale = math.sqrt(variance)
            if scale == 0.0:
                raise ValueError(f"standardize transform for {field_id} has zero scale")
            entry |= {"mean": mean, "scale": scale}
        elif kind == "minmax":
            low, high = min(values), max(values)
            if low == high:
                raise ValueError(f"minmax transform for {field_id} has zero range")
            entry |= {"low": low, "high": high}
        fitted.append(entry)

    body = {
        "schema": "comsol_mcp.surrogate_fitted_transforms",
        "schema_version": SCHEMA_VERSION,
        "transform_id": _require_str("transform_id", transform_id),
        "field_schema_sha256": validated["manifest_sha256"],
        "fit_split": "train",
        "fit_row_count": len(train_rows),
        "transforms": fitted,
    }
    return _finalize(body)


def validate_fitted_transforms(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("fitted transforms must be a mapping")
    manifest = _validate_sealed(
        value,
        (
            "schema",
            "schema_version",
            "transform_id",
            "field_schema_sha256",
            "fit_split",
            "fit_row_count",
            "transforms",
        ),
    )
    if manifest["schema"] != "comsol_mcp.surrogate_fitted_transforms":
        raise ValueError("unexpected schema name")
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported schema_version")
    if manifest["fit_split"] != "train":
        raise ValueError("transforms may fit only on train")
    if not isinstance(manifest["fit_row_count"], int) or manifest["fit_row_count"] < 1:
        raise ValueError("fit_row_count must be a positive integer")
    seen: set[str] = set()
    for item in manifest["transforms"]:
        if not isinstance(item, Mapping):
            raise ValueError("each transform must be a mapping")
        field_id = _require_str("transform.field_id", item.get("field_id"))
        if field_id in seen:
            raise ValueError("transform field_id must be unique")
        seen.add(field_id)
        kind = item.get("kind")
        if kind not in TRANSFORM_KINDS:
            raise ValueError("unsupported transform kind")
        if kind == "standardize":
            if _require_finite("transform.scale", item.get("scale")) == 0.0:
                raise ValueError("standardize scale must be nonzero")
            _require_finite("transform.mean", item.get("mean"))
        elif kind == "minmax":
            low = _require_finite("transform.low", item.get("low"))
            high = _require_finite("transform.high", item.get("high"))
            if not low < high:
                raise ValueError("minmax requires low < high")
    return manifest


def apply_transforms(transforms: Mapping[str, Any], row: Mapping[str, float]) -> dict[str, float]:
    """Apply frozen transforms forward."""
    validated = validate_fitted_transforms(transforms)
    output: dict[str, float] = {}
    for item in validated["transforms"]:
        field_id = item["field_id"]
        if field_id not in row:
            raise ValueError(f"row is missing field {field_id}")
        value = _require_finite(f"row.{field_id}", row[field_id])
        if item["kind"] == "standardize":
            output[field_id] = (value - item["mean"]) / item["scale"]
        elif item["kind"] == "minmax":
            output[field_id] = (value - item["low"]) / (item["high"] - item["low"])
        else:
            output[field_id] = value
    return output


def invert_transforms(
    transforms: Mapping[str, Any], transformed: Mapping[str, float]
) -> dict[str, float]:
    """Apply frozen transforms in reverse."""
    validated = validate_fitted_transforms(transforms)
    output: dict[str, float] = {}
    for item in validated["transforms"]:
        field_id = item["field_id"]
        if field_id not in transformed:
            raise ValueError(f"transformed row is missing field {field_id}")
        value = _require_finite(f"transformed.{field_id}", transformed[field_id])
        if item["kind"] == "standardize":
            output[field_id] = value * item["scale"] + item["mean"]
        elif item["kind"] == "minmax":
            output[field_id] = value * (item["high"] - item["low"]) + item["low"]
        else:
            output[field_id] = value
    return output


def verify_transform_roundtrip(
    transforms: Mapping[str, Any],
    rows: Sequence[Mapping[str, float]],
    *,
    relative_tolerance: float = 1e-12,
    absolute_tolerance: float = 1e-12,
) -> dict[str, Any]:
    """Prove forward/inverse round-trip fidelity on the supplied rows."""
    if relative_tolerance <= 0 or absolute_tolerance <= 0:
        raise ValueError("round-trip tolerances must be positive")
    worst = 0.0
    worst_field: str | None = None
    for index, row in enumerate(rows):
        restored = invert_transforms(transforms, apply_transforms(transforms, row))
        for field_id, original in row.items():
            if field_id not in restored:
                continue
            delta = abs(restored[field_id] - float(original))
            allowed = absolute_tolerance + relative_tolerance * abs(float(original))
            if delta > allowed:
                raise ValueError(f"transform round-trip failed for {field_id} at row {index}")
            if delta > worst:
                worst, worst_field = delta, field_id
    return {
        "roundtrip_verified": True,
        "row_count": len(rows),
        "worst_absolute_error": worst,
        "worst_field": worst_field,
        "relative_tolerance": relative_tolerance,
        "absolute_tolerance": absolute_tolerance,
    }


__all__ = [
    "MAX_FIELDS",
    "SCHEMA_VERSION",
    "TRANSFORM_KINDS",
    "apply_transforms",
    "build_field_schema",
    "fit_transforms",
    "invert_transforms",
    "validate_field_schema",
    "validate_fitted_transforms",
    "verify_transform_roundtrip",
]
