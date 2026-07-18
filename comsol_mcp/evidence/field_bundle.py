"""Solver-free contracts for bounded field-evidence extraction requests."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
from typing import Any, Mapping


FIELD_EVIDENCE_REQUEST_SCHEMA = "comsol_mcp.field_evidence_request"
FIELD_EVIDENCE_SCHEMA_VERSION = "1.1.0"

MAX_FIELD_EXPRESSIONS = 8
MAX_FIELD_VIEWS = 2
MAX_RAW_FIELD_POINTS = 1_000_000
MAX_GRID_FIELD_POINTS = 1_048_576
MAX_FIELD_ARTIFACT_BYTES = 256 * 1024 * 1024
MAX_INLINE_FIELD_SAMPLES = 32
MAX_FIELD_REQUEST_BYTES = 256 * 1024
MAX_TEXT = 4096

_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_TAG = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


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
        raise ValueError("field-evidence request must contain only finite JSON values") from exc


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise ValueError(f"{label} must be an object with string keys")
    return dict(value)


def _exact_fields(
    value: Mapping[str, Any],
    allowed: set[str],
    required: set[str],
    label: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    missing = sorted(required - set(value))
    if unknown or missing:
        raise ValueError(
            f"{label} has unsupported fields {unknown} or missing fields {missing}"
        )


def _text(value: object, label: str, *, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    text = value.strip()
    if len(text) > MAX_TEXT:
        raise ValueError(f"{label} exceeds {MAX_TEXT} characters")
    if identifier and not _IDENTIFIER.fullmatch(text):
        raise ValueError(f"{label} must be a bounded portable identifier")
    return text


def _tag(value: object, label: str) -> str:
    text = _text(value, label)
    if not _TAG.fullmatch(text):
        raise ValueError(f"{label} must be one exact COMSOL tag")
    return text


def _sha256(value: object, label: str) -> str:
    text = _text(value, label).lower()
    if not _HEX64.fullmatch(text):
        raise ValueError(f"{label} must be exactly 64 hexadecimal characters")
    return text


def _finite(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        qualifier = "positive and finite" if positive else "finite"
        raise ValueError(f"{label} must be {qualifier}")
    return number


def _positive_integer(value: object, label: str, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > maximum
    ):
        raise ValueError(f"{label} must be an integer between 1 and {maximum}")
    return value


def _range(value: object, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{label} must contain exactly two finite values")
    result = [_finite(item, f"{label}[{index}]") for index, item in enumerate(value)]
    if result[0] >= result[1]:
        raise ValueError(f"{label} must be strictly increasing")
    return result


def _normalize_source(value: object, label: str) -> dict[str, Any]:
    raw = _mapping(value, label)
    kind = raw.get("kind")
    if kind == "existing_dataset":
        allowed = {
            "kind",
            "source_model_sha256",
            "component_tag",
            "dataset_name",
            "dataset_tag",
            "solution_tag",
            "solution_number",
        }
        _exact_fields(
            raw,
            allowed,
            {
                "kind",
                "source_model_sha256",
                "component_tag",
                "dataset_name",
                "dataset_tag",
                "solution_tag",
            },
            label,
        )
        solution_number = raw.get("solution_number")
        if solution_number is not None:
            solution_number = _positive_integer(
                solution_number, f"{label}.solution_number", 1_000_000
            )
        result = {
            "kind": kind,
            "source_model_sha256": _sha256(
                raw["source_model_sha256"], f"{label}.source_model_sha256"
            ),
            "component_tag": _tag(raw["component_tag"], f"{label}.component_tag"),
            "dataset_name": _text(raw["dataset_name"], f"{label}.dataset_name"),
            "dataset_tag": _tag(raw["dataset_tag"], f"{label}.dataset_tag"),
            "solution_tag": _tag(raw["solution_tag"], f"{label}.solution_tag"),
            "solution_number": solution_number,
        }
    elif kind == "validation_matrix_point":
        allowed = {
            "kind",
            "source_model_sha256",
            "job_id",
            "point_id",
            "point_fingerprint",
            "artifact_id",
            "component_tag",
            "dataset_name",
            "dataset_tag",
            "solution_tag",
        }
        _exact_fields(raw, allowed, allowed, label)
        result = {
            "kind": kind,
            "source_model_sha256": _sha256(
                raw["source_model_sha256"], f"{label}.source_model_sha256"
            ),
            "job_id": _text(raw["job_id"], f"{label}.job_id", identifier=True),
            "point_id": _text(raw["point_id"], f"{label}.point_id", identifier=True),
            "point_fingerprint": _sha256(
                raw["point_fingerprint"], f"{label}.point_fingerprint"
            ),
            "artifact_id": _text(
                raw["artifact_id"], f"{label}.artifact_id", identifier=True
            ),
            "component_tag": _tag(raw["component_tag"], f"{label}.component_tag"),
            "dataset_name": _text(raw["dataset_name"], f"{label}.dataset_name"),
            "dataset_tag": _tag(raw["dataset_tag"], f"{label}.dataset_tag"),
            "solution_tag": _tag(raw["solution_tag"], f"{label}.solution_tag"),
        }
    else:
        raise ValueError(
            f"{label}.kind must be existing_dataset or validation_matrix_point"
        )
    result["source_fingerprint"] = _fingerprint(result)
    return result


def _normalize_outputs(value: object, label: str, render_png: bool) -> dict[str, Any]:
    raw = _mapping(value, label)
    required = {"array_artifact_id", "manifest_artifact_id"}
    allowed = required | {"png_artifact_id"}
    _exact_fields(raw, allowed, required, label)
    if render_png != ("png_artifact_id" in raw):
        raise ValueError(
            f"{label}.png_artifact_id must be present exactly when PNG rendering is enabled"
        )
    result = {
        "array_artifact_id": _text(
            raw["array_artifact_id"],
            f"{label}.array_artifact_id",
            identifier=True,
        ),
        "manifest_artifact_id": _text(
            raw["manifest_artifact_id"],
            f"{label}.manifest_artifact_id",
            identifier=True,
        ),
        "png_artifact_id": (
            _text(
                raw["png_artifact_id"],
                f"{label}.png_artifact_id",
                identifier=True,
            )
            if render_png
            else None
        ),
    }
    ids = [item for item in result.values() if item is not None]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{label} artifact IDs must be unique")
    return result


def _normalize_view(value: object, index: int, render_png: bool) -> dict[str, Any]:
    label = f"views[{index}]"
    raw = _mapping(value, label)
    allowed = {"view_id", "wavelength_m", "source", "outputs"}
    _exact_fields(raw, allowed, allowed, label)
    result = {
        "view_id": _text(raw["view_id"], f"{label}.view_id", identifier=True),
        "wavelength_m": _finite(
            raw["wavelength_m"], f"{label}.wavelength_m", positive=True
        ),
        "source": _normalize_source(raw["source"], f"{label}.source"),
        "outputs": _normalize_outputs(raw["outputs"], f"{label}.outputs", render_png),
    }
    result["view_fingerprint"] = _fingerprint(result)
    return result


def normalize_field_evidence_request(value: object) -> dict[str, Any]:
    """Normalize one bounded extraction request without importing COMSOL or NumPy."""
    raw = _mapping(value, "field_evidence_request")
    allowed = {
        "request_id",
        "configuration_sha256",
        "expressions",
        "views",
        "slice",
        "coordinate_bounds",
        "grid",
        "render",
        "limits",
    }
    _exact_fields(raw, allowed, allowed, "field_evidence_request")

    expressions = raw["expressions"]
    if not isinstance(expressions, list) or not 1 <= len(expressions) <= MAX_FIELD_EXPRESSIONS:
        raise ValueError(
            f"expressions must contain 1..{MAX_FIELD_EXPRESSIONS} entries"
        )
    normalized_expressions: list[dict[str, str]] = []
    for index, value_item in enumerate(expressions):
        label = f"expressions[{index}]"
        item = _mapping(value_item, label)
        _exact_fields(item, {"name", "expression", "unit"}, {"name", "expression", "unit"}, label)
        normalized_expressions.append(
            {
                "name": _text(item["name"], f"{label}.name", identifier=True),
                "expression": _text(item["expression"], f"{label}.expression"),
                "unit": _text(item["unit"], f"{label}.unit"),
            }
        )
    expression_names = [item["name"] for item in normalized_expressions]
    if len(expression_names) != len(set(expression_names)):
        raise ValueError("expressions must have unique names")

    render_raw = _mapping(raw["render"], "render")
    _exact_fields(
        render_raw,
        {"png", "color_scale", "shared_color_limits"},
        {"png", "color_scale", "shared_color_limits"},
        "render",
    )
    if not isinstance(render_raw["png"], bool) or not isinstance(
        render_raw["shared_color_limits"], bool
    ):
        raise ValueError("render.png and render.shared_color_limits must be boolean")
    if render_raw["color_scale"] not in {"linear", "log"}:
        raise ValueError("render.color_scale must be linear or log")
    render = {
        "png": render_raw["png"],
        "color_scale": render_raw["color_scale"],
        "shared_color_limits": render_raw["shared_color_limits"],
    }

    views_raw = raw["views"]
    if not isinstance(views_raw, list) or not 1 <= len(views_raw) <= MAX_FIELD_VIEWS:
        raise ValueError(f"views must contain 1..{MAX_FIELD_VIEWS} entries")
    views = [
        _normalize_view(item, index, render["png"])
        for index, item in enumerate(views_raw)
    ]
    view_ids = [item["view_id"] for item in views]
    if len(view_ids) != len(set(view_ids)):
        raise ValueError("views must have unique view_id values")
    source_fingerprints = [item["source"]["source_fingerprint"] for item in views]
    if len(source_fingerprints) != len(set(source_fingerprints)):
        raise ValueError("views must have unique exact source identities")
    artifact_ids = [
        artifact_id
        for view in views
        for artifact_id in view["outputs"].values()
        if artifact_id is not None
    ]
    if len(artifact_ids) != len(set(artifact_ids)):
        raise ValueError("artifact IDs must be unique across all views")
    if len(views) == 2 and render["png"] and not render["shared_color_limits"]:
        raise ValueError("paired PNG views require shared_color_limits=true")
    if len(views) == 1 and render["shared_color_limits"]:
        raise ValueError("shared_color_limits requires exactly two views")

    slice_raw = _mapping(raw["slice"], "slice")
    _exact_fields(
        slice_raw,
        {"axis", "value", "tolerance", "unit"},
        {"axis", "value", "tolerance", "unit"},
        "slice",
    )
    if slice_raw["axis"] not in {"x", "y", "z"}:
        raise ValueError("slice.axis must be x, y, or z")
    slice_spec = {
        "axis": slice_raw["axis"],
        "value": _finite(slice_raw["value"], "slice.value"),
        "tolerance": _finite(
            slice_raw["tolerance"], "slice.tolerance", positive=True
        ),
        "unit": _text(slice_raw["unit"], "slice.unit"),
    }

    bounds_raw = _mapping(raw["coordinate_bounds"], "coordinate_bounds")
    _exact_fields(
        bounds_raw,
        {"x", "y", "z", "unit"},
        {"x", "y", "z", "unit"},
        "coordinate_bounds",
    )
    coordinate_bounds = {
        "x": _range(bounds_raw["x"], "coordinate_bounds.x"),
        "y": _range(bounds_raw["y"], "coordinate_bounds.y"),
        "z": _range(bounds_raw["z"], "coordinate_bounds.z"),
        "unit": _text(bounds_raw["unit"], "coordinate_bounds.unit"),
    }
    if coordinate_bounds["unit"] != slice_spec["unit"]:
        raise ValueError("slice and coordinate bounds must use the same unit")
    slice_range = coordinate_bounds[slice_spec["axis"]]
    if not slice_range[0] <= slice_spec["value"] <= slice_range[1]:
        raise ValueError("slice.value must lie inside the matching coordinate bound")

    grid_raw = _mapping(raw["grid"], "grid")
    _exact_fields(
        grid_raw,
        {"shape", "interpolation"},
        {"shape", "interpolation"},
        "grid",
    )
    shape = grid_raw["shape"]
    if not isinstance(shape, list) or len(shape) != 2:
        raise ValueError("grid.shape must contain exactly two positive integers")
    normalized_shape = [
        _positive_integer(item, f"grid.shape[{index}]", 8192)
        for index, item in enumerate(shape)
    ]
    if grid_raw["interpolation"] not in {"linear", "nearest"}:
        raise ValueError("grid.interpolation must be linear or nearest")
    grid = {"shape": normalized_shape, "interpolation": grid_raw["interpolation"]}

    limits_raw = _mapping(raw["limits"], "limits")
    limit_fields = {
        "max_raw_points",
        "max_grid_points",
        "max_artifact_bytes",
        "max_inline_samples",
    }
    _exact_fields(limits_raw, limit_fields, limit_fields, "limits")
    limits = {
        "max_raw_points": _positive_integer(
            limits_raw["max_raw_points"], "limits.max_raw_points", MAX_RAW_FIELD_POINTS
        ),
        "max_grid_points": _positive_integer(
            limits_raw["max_grid_points"], "limits.max_grid_points", MAX_GRID_FIELD_POINTS
        ),
        "max_artifact_bytes": _positive_integer(
            limits_raw["max_artifact_bytes"],
            "limits.max_artifact_bytes",
            MAX_FIELD_ARTIFACT_BYTES,
        ),
        "max_inline_samples": _positive_integer(
            limits_raw["max_inline_samples"],
            "limits.max_inline_samples",
            MAX_INLINE_FIELD_SAMPLES,
        ),
    }
    grid_points = normalized_shape[0] * normalized_shape[1]
    if grid_points > limits["max_grid_points"]:
        raise ValueError("grid.shape exceeds the caller-declared max_grid_points")

    result = {
        "schema_name": FIELD_EVIDENCE_REQUEST_SCHEMA,
        "schema_version": FIELD_EVIDENCE_SCHEMA_VERSION,
        "request_id": _text(raw["request_id"], "request_id", identifier=True),
        "configuration_sha256": _sha256(
            raw["configuration_sha256"], "configuration_sha256"
        ),
        "expressions": normalized_expressions,
        "views": views,
        "slice": slice_spec,
        "coordinate_bounds": coordinate_bounds,
        "grid": grid,
        "render": render,
        "limits": limits,
        "grid_point_count": grid_points,
        "visual_review_state": "visual_review_required",
    }
    if len(_canonical_bytes(result)) > MAX_FIELD_REQUEST_BYTES:
        raise ValueError(
            f"field-evidence request exceeds {MAX_FIELD_REQUEST_BYTES} bytes"
        )
    result["request_fingerprint"] = _fingerprint(result)
    return deepcopy(result)


def validate_field_evidence_request(value: object) -> dict[str, Any]:
    """Validate a normalized request after JSON or worker-process transport."""
    item = _mapping(value, "field_evidence_request")
    normalized_fields = {
        "schema_name",
        "schema_version",
        "request_id",
        "configuration_sha256",
        "expressions",
        "views",
        "slice",
        "coordinate_bounds",
        "grid",
        "render",
        "limits",
        "grid_point_count",
        "visual_review_state",
        "request_fingerprint",
    }
    _exact_fields(item, normalized_fields, normalized_fields, "field_evidence_request")
    if (
        item["schema_name"] != FIELD_EVIDENCE_REQUEST_SCHEMA
        or item["schema_version"] != FIELD_EVIDENCE_SCHEMA_VERSION
    ):
        raise ValueError("field_evidence_request schema is unsupported")
    if item["visual_review_state"] != "visual_review_required":
        raise ValueError("field_evidence_request must remain visual_review_required")

    views = item["views"]
    if not isinstance(views, list):
        raise ValueError("field_evidence_request.views must be a list")
    raw_views: list[dict[str, Any]] = []
    for index, value_item in enumerate(views):
        view = _mapping(value_item, f"field_evidence_request.views[{index}]")
        source = _mapping(
            view.get("source"), f"field_evidence_request.views[{index}].source"
        )
        outputs = _mapping(
            view.get("outputs"), f"field_evidence_request.views[{index}].outputs"
        )
        raw_source = dict(source)
        raw_source.pop("source_fingerprint", None)
        raw_outputs = dict(outputs)
        if raw_outputs.get("png_artifact_id") is None:
            raw_outputs.pop("png_artifact_id", None)
        raw_views.append(
            {
                "view_id": view.get("view_id"),
                "wavelength_m": view.get("wavelength_m"),
                "source": raw_source,
                "outputs": raw_outputs,
            }
        )

    normalized = normalize_field_evidence_request(
        {
            "request_id": item["request_id"],
            "configuration_sha256": item["configuration_sha256"],
            "expressions": item["expressions"],
            "views": raw_views,
            "slice": item["slice"],
            "coordinate_bounds": item["coordinate_bounds"],
            "grid": item["grid"],
            "render": item["render"],
            "limits": item["limits"],
        }
    )
    if normalized != item:
        raise ValueError("field_evidence_request is not canonical or was modified")
    return deepcopy(normalized)


__all__ = [
    "FIELD_EVIDENCE_REQUEST_SCHEMA",
    "FIELD_EVIDENCE_SCHEMA_VERSION",
    "MAX_FIELD_ARTIFACT_BYTES",
    "MAX_FIELD_EXPRESSIONS",
    "MAX_FIELD_VIEWS",
    "MAX_GRID_FIELD_POINTS",
    "MAX_INLINE_FIELD_SAMPLES",
    "MAX_RAW_FIELD_POINTS",
    "normalize_field_evidence_request",
    "validate_field_evidence_request",
]
