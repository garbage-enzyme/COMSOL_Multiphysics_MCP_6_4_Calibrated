"""Durable bounded serialization for already-gridded scalar field evidence."""

from __future__ import annotations

import math
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Mapping

from comsol_mcp.durable.io import (
    atomic_write_bytes_exclusive,
    atomic_write_json_exclusive,
    fsync_directory,
    publish_file_exclusive,
    read_file_bytes_bounded,
    snapshot_file_bounded,
)

from .field_bundle import validate_field_evidence_request
from .field_manifest import build_field_evidence_manifest

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _resolve_root(value: object) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError("artifact_root must be a filesystem path")
    root = Path(value).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise ValueError("artifact_root must be a directory")
    return root


def _descriptor(
    path: Path,
    *,
    root: Path,
    artifact_id: str,
    media_type: str,
    max_bytes: int,
    required_prefix: bytes | None = None,
    relative_path: str | None = None,
) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("artifact path escapes artifact_root") from exc
    snapshot = snapshot_file_bounded(
        resolved,
        max_bytes=max_bytes,
        prefix_bytes=len(required_prefix) if required_prefix is not None else 0,
    )
    if snapshot["byte_count"] <= 0:
        raise ValueError("field artifact must not be empty")
    if required_prefix is not None and snapshot["prefix"] != required_prefix:
        raise ValueError("png_path does not contain a PNG signature")
    return {
        "artifact_id": artifact_id,
        "relative_path": relative_path or relative,
        "media_type": media_type,
        "sha256": snapshot["sha256"],
        "byte_count": snapshot["byte_count"],
    }


def _png_descriptor(
    value: object,
    *,
    root: Path,
    artifact_id: str | None,
    max_bytes: int,
    destination: Path,
    relative_path: str,
) -> dict[str, Any] | None:
    if artifact_id is None:
        if value is not None:
            raise ValueError("png_path was provided but PNG rendering was not requested")
        return None
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError("png_path is required when PNG rendering was requested")
    path = Path(value).expanduser().resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("png_path must remain inside artifact_root") from exc
    try:
        payload = read_file_bytes_bounded(path, max_bytes=max_bytes)
        if not payload.startswith(_PNG_SIGNATURE):
            raise ValueError("png_path does not contain a PNG signature")
        atomic_write_bytes_exclusive(destination, payload)
        return _descriptor(
            destination,
            root=root,
            artifact_id=artifact_id,
            media_type="image/png",
            max_bytes=max_bytes,
            required_prefix=_PNG_SIGNATURE,
            relative_path=relative_path,
        )
    except FileNotFoundError as exc:
        raise ValueError("png_path must name an existing PNG file") from exc


def write_field_evidence_artifacts(
    *,
    request: object,
    view_id: str,
    artifact_root: object,
    axis_coordinates: object,
    quantity_grids: object,
    raw_point_count: int,
    selected_point_count: int,
    unique_point_count: int | None = None,
    collapsed_duplicate_point_count: int | None = None,
    png_path: object | None = None,
) -> dict[str, Any]:
    """Write one immutable NPZ plus manifest from bounded real scalar grids.

    Interpolation and COMSOL evaluation are intentionally outside this function.
    The first non-slice Cartesian axis is stored as the column axis and the
    second as the row axis; quantity arrays therefore have shape ``(rows, cols)``.
    """
    request_value = validate_field_evidence_request(request)
    matches = [view for view in request_value["views"] if view["view_id"] == view_id]
    if len(matches) != 1:
        raise ValueError("view_id must identify exactly one requested view")
    view = matches[0]
    root = _resolve_root(artifact_root)

    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - the runtime package already uses NumPy
        raise RuntimeError("NumPy is required to serialize field artifacts") from exc

    if not isinstance(axis_coordinates, Mapping):
        raise ValueError("axis_coordinates must be an object")
    slice_axis = request_value["slice"]["axis"]
    plane_axes = [axis for axis in ("x", "y", "z") if axis != slice_axis]
    if set(axis_coordinates) != set(plane_axes):
        raise ValueError("axis_coordinates must contain exactly the two non-slice axes")
    column_axis, row_axis = plane_axes
    rows, columns = request_value["grid"]["shape"]
    coordinate_arrays: dict[str, Any] = {}
    for axis, expected_length in ((column_axis, columns), (row_axis, rows)):
        values = np.asarray(axis_coordinates[axis])
        if values.ndim != 1 or values.shape[0] != expected_length:
            raise ValueError(f"axis_coordinates.{axis} must have length {expected_length}")
        if values.dtype.kind not in "fiu" or not np.all(np.isfinite(values)):
            raise ValueError(f"axis_coordinates.{axis} must be finite numeric values")
        values = values.astype(np.float64, copy=False)
        if np.any(np.diff(values) <= 0):
            raise ValueError(f"axis_coordinates.{axis} must be strictly increasing")
        bounds = request_value["coordinate_bounds"][axis]
        if float(values[0]) < bounds[0] or float(values[-1]) > bounds[1]:
            raise ValueError(f"axis_coordinates.{axis} escapes the requested bounds")
        coordinate_arrays[axis] = values

    if not isinstance(quantity_grids, Mapping):
        raise ValueError("quantity_grids must be an object")
    expected_expressions = {item["name"]: item for item in request_value["expressions"]}
    if set(quantity_grids) != set(expected_expressions):
        raise ValueError("quantity_grids must contain exactly the requested expressions")
    normalized_grids: dict[str, Any] = {}
    missing_mask = None
    raw_array_bytes = sum(array.nbytes for array in coordinate_arrays.values())
    summaries: list[dict[str, Any]] = []
    for expression in request_value["expressions"]:
        name = expression["name"]
        values = np.asarray(quantity_grids[name])
        if values.shape != (rows, columns):
            raise ValueError(f"quantity_grids.{name} must have shape {(rows, columns)}")
        if values.dtype.kind not in "fiu" or np.any(np.isinf(values)):
            raise ValueError(f"quantity_grids.{name} must be real numeric values without infinity")
        values = values.astype(np.float64, copy=False)
        current_missing = np.isnan(values)
        if missing_mask is None:
            missing_mask = current_missing
        elif not np.array_equal(missing_mask, current_missing):
            raise ValueError("all quantity grids must use the same missing-cell mask")
        finite = values[~current_missing]
        if finite.size == 0:
            raise ValueError(f"quantity_grids.{name} must contain at least one finite value")
        raw_array_bytes += values.nbytes
        normalized_grids[name] = values
        scale = float(np.max(np.abs(finite)))
        rms = 0.0 if scale == 0.0 else scale * math.sqrt(float(np.mean(np.square(finite / scale))))
        summaries.append(
            {
                "name": name,
                "unit": expression["unit"],
                "minimum": float(np.min(finite)),
                "maximum": float(np.max(finite)),
                "rms": rms,
                "finite_count": int(finite.size),
                "missing_count": int(current_missing.sum()),
            }
        )
    if raw_array_bytes > request_value["limits"]["max_artifact_bytes"]:
        raise ValueError("uncompressed field arrays exceed the caller-declared byte limit")

    if missing_mask is None:
        raise RuntimeError("validated field request produced no expression mask")
    missing_count = int(missing_mask.sum())
    covered_count = request_value["grid_point_count"] - missing_count
    slice_value = request_value["slice"]["value"]
    coordinate_ranges = {
        axis: (
            [slice_value, slice_value]
            if axis == slice_axis
            else [
                float(coordinate_arrays[axis][0]),
                float(coordinate_arrays[axis][-1]),
            ]
        )
        for axis in ("x", "y", "z")
    }
    coordinate_ranges["unit"] = request_value["coordinate_bounds"]["unit"]

    view_directory = root / view["view_fingerprint"]
    if view_directory.exists():
        raise FileExistsError("field evidence artifacts already exist for this view")
    staging_directory = root / f".fb-{uuid.uuid4().hex[:8]}"
    staging_directory.mkdir(parents=False, exist_ok=False)
    array_path = staging_directory / "field_arrays.npz"
    manifest_path = staging_directory / "field_manifest.json"
    png_destination = staging_directory / "field_render.png"
    temporary = staging_directory / ".a.tmp"
    published = False
    final_prefix = view["view_fingerprint"]
    try:
        with temporary.open("xb") as handle:
            np.savez_compressed(
                handle,
                **{f"coordinate_{axis}": coordinate_arrays[axis] for axis in plane_axes},
                **{f"quantity_{name}": normalized_grids[name] for name in expected_expressions},
            )
            handle.flush()
            os.fsync(handle.fileno())
        publish_file_exclusive(temporary, array_path)
        array_descriptor = _descriptor(
            array_path,
            root=root,
            artifact_id=view["outputs"]["array_artifact_id"],
            media_type="application/x-npz",
            max_bytes=request_value["limits"]["max_artifact_bytes"],
            relative_path=f"{final_prefix}/field_arrays.npz",
        )
        png_descriptor = _png_descriptor(
            png_path,
            root=root,
            artifact_id=view["outputs"]["png_artifact_id"],
            max_bytes=request_value["limits"]["max_artifact_bytes"],
            destination=png_destination,
            relative_path=f"{final_prefix}/field_render.png",
        )
        manifest = build_field_evidence_manifest(
            request=request_value,
            view_id=view_id,
            raw_point_count=raw_point_count,
            selected_point_count=selected_point_count,
            unique_point_count=unique_point_count,
            collapsed_duplicate_point_count=collapsed_duplicate_point_count,
            covered_grid_point_count=covered_count,
            missing_grid_point_count=missing_count,
            coordinate_ranges=coordinate_ranges,
            quantity_summaries=summaries,
            array_artifact=array_descriptor,
            png_artifact=png_descriptor,
        )
        atomic_write_json_exclusive(manifest_path, manifest)
        manifest_descriptor = _descriptor(
            manifest_path,
            root=root,
            artifact_id=view["outputs"]["manifest_artifact_id"],
            media_type="application/json",
            max_bytes=request_value["limits"]["max_artifact_bytes"],
            relative_path=f"{final_prefix}/field_manifest.json",
        )
        total_bytes = (
            array_descriptor["byte_count"]
            + manifest_descriptor["byte_count"]
            + (png_descriptor["byte_count"] if png_descriptor else 0)
        )
        if total_bytes > request_value["limits"]["max_artifact_bytes"]:
            raise ValueError("complete field bundle exceeds the caller-declared byte limit")
        try:
            os.rename(staging_directory, view_directory)
        except FileExistsError as exc:
            raise FileExistsError("field evidence artifacts already exist for this view") from exc
        fsync_directory(root)
        published = True
        return {
            "request_id": request_value["request_id"],
            "request_fingerprint": request_value["request_fingerprint"],
            "view_id": view_id,
            "view_fingerprint": view["view_fingerprint"],
            "array_artifact": array_descriptor,
            "manifest_artifact": manifest_descriptor,
            "png_artifact": png_descriptor,
            "raw_point_count": manifest["raw_point_count"],
            "selected_point_count": manifest["selected_point_count"],
            "grid_point_count": request_value["grid_point_count"],
            "unique_point_count": manifest["unique_point_count"],
            "collapsed_duplicate_point_count": manifest["collapsed_duplicate_point_count"],
            "covered_grid_point_count": covered_count,
            "missing_grid_point_count": missing_count,
            "quantity_summaries": summaries,
            "visual_review_state": "visual_review_required",
            "semantic_mode_label": "not_assigned",
        }
    finally:
        temporary.unlink(missing_ok=True)
        if not published:
            shutil.rmtree(staging_directory, ignore_errors=True)


__all__ = ["write_field_evidence_artifacts"]
