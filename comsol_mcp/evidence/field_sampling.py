"""Solver-free selection of bounded raw field samples for one requested slice."""

from __future__ import annotations

from typing import Any, Mapping

from .field_bundle import validate_field_evidence_request


def select_field_slice_samples(
    *,
    request: object,
    view_id: str,
    coordinates: object,
    quantities: object,
) -> dict[str, Any]:
    """Select finite in-bounds raw samples without importing COMSOL or SciPy."""
    request_value = validate_field_evidence_request(request)
    if len([view for view in request_value["views"] if view["view_id"] == view_id]) != 1:
        raise ValueError("view_id must identify exactly one requested view")

    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - runtime field tools require NumPy
        raise RuntimeError("NumPy is required to select field samples") from exc

    if not isinstance(coordinates, Mapping) or set(coordinates) != {"x", "y", "z"}:
        raise ValueError("coordinates must contain exactly x, y, and z")
    coordinate_arrays: dict[str, Any] = {}
    raw_count: int | None = None
    point_limit = request_value["limits"]["max_raw_points"]
    for axis in ("x", "y", "z"):
        raw_values = coordinates[axis]
        try:
            declared_count = len(raw_values)
        except TypeError:
            declared_count = None
        if declared_count is not None and declared_count > point_limit:
            raise ValueError("raw field samples exceed the caller-declared point limit")
        values = np.asarray(raw_values)
        if values.ndim != 1 or values.dtype.kind not in "fiu":
            raise ValueError(f"coordinates.{axis} must be a one-dimensional numeric array")
        if values.size > point_limit:
            raise ValueError("raw field samples exceed the caller-declared point limit")
        if raw_count is not None and values.size != raw_count:
            raise ValueError("all coordinate arrays must have the same length")
        values = values.astype(np.float64, copy=False)
        if not np.all(np.isfinite(values)):
            raise ValueError(f"coordinates.{axis} must contain only finite values")
        if raw_count is None:
            raw_count = int(values.size)
            if raw_count <= 0:
                raise ValueError("raw field samples must not be empty")
        coordinate_arrays[axis] = values
    if raw_count is None:
        raise RuntimeError("coordinate normalization produced no sample count")

    if not isinstance(quantities, Mapping):
        raise ValueError("quantities must be an object")
    expression_names = [item["name"] for item in request_value["expressions"]]
    if set(quantities) != set(expression_names):
        raise ValueError("quantities must contain exactly the requested expressions")
    quantity_arrays: dict[str, Any] = {}
    for name in expression_names:
        raw_values = quantities[name]
        try:
            declared_count = len(raw_values)
        except TypeError:
            declared_count = None
        if declared_count is not None and declared_count != raw_count:
            raise ValueError(
                f"quantities.{name} must be a one-dimensional numeric array matching coordinates"
            )
        values = np.asarray(raw_values)
        if values.ndim != 1 or values.size != raw_count or values.dtype.kind not in "fiu":
            raise ValueError(
                f"quantities.{name} must be a one-dimensional numeric array matching coordinates"
            )
        values = values.astype(np.float64, copy=False)
        if not np.all(np.isfinite(values)):
            raise ValueError(f"quantities.{name} must contain only finite values")
        quantity_arrays[name] = values

    mask = np.ones(raw_count, dtype=bool)
    for axis in ("x", "y", "z"):
        lower, upper = request_value["coordinate_bounds"][axis]
        inclusive_lower = np.nextafter(float(lower), -np.inf)
        inclusive_upper = np.nextafter(float(upper), np.inf)
        mask &= coordinate_arrays[axis] >= inclusive_lower
        mask &= coordinate_arrays[axis] <= inclusive_upper
    slice_spec = request_value["slice"]
    slice_lower = np.nextafter(float(slice_spec["value"] - slice_spec["tolerance"]), -np.inf)
    slice_upper = np.nextafter(float(slice_spec["value"] + slice_spec["tolerance"]), np.inf)
    slice_coordinates = coordinate_arrays[slice_spec["axis"]]
    mask &= slice_coordinates >= slice_lower
    mask &= slice_coordinates <= slice_upper
    selected_count = int(mask.sum())
    if selected_count == 0:
        raise ValueError("slice selection contains no field samples")

    plane_axes = [axis for axis in ("x", "y", "z") if axis != slice_spec["axis"]]
    selected_coordinates = {axis: values[mask] for axis, values in coordinate_arrays.items()}
    selected_quantities = {name: values[mask] for name, values in quantity_arrays.items()}
    if request_value["grid"]["interpolation"] == "linear":
        if selected_count < 3:
            raise ValueError("linear interpolation requires at least three selected samples")
        plane = np.column_stack(
            (selected_coordinates[plane_axes[0]], selected_coordinates[plane_axes[1]])
        )
        if len(np.unique(plane[:, 0])) < 2 or len(np.unique(plane[:, 1])) < 2:
            raise ValueError("linear interpolation requires variation on both in-plane axes")
        if np.linalg.matrix_rank(plane - np.mean(plane, axis=0)) < 2:
            raise ValueError("linear interpolation samples must not be collinear")

    ranges = {
        axis: [
            float(np.min(selected_coordinates[axis])),
            float(np.max(selected_coordinates[axis])),
        ]
        for axis in ("x", "y", "z")
    }
    ranges["unit"] = request_value["coordinate_bounds"]["unit"]
    return {
        "request_id": request_value["request_id"],
        "request_fingerprint": request_value["request_fingerprint"],
        "view_id": view_id,
        "raw_point_count": raw_count,
        "selected_point_count": selected_count,
        "rejected_point_count": raw_count - selected_count,
        "slice": dict(slice_spec),
        "coordinate_ranges": ranges,
        "plane_axes": plane_axes,
        "coordinates": selected_coordinates,
        "quantities": selected_quantities,
    }


__all__ = ["select_field_slice_samples"]
