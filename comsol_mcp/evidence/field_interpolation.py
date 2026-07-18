"""Bounded interpolation of selected field samples onto a declared grid."""

from __future__ import annotations

from typing import Any, Mapping

from .field_bundle import validate_field_evidence_request


_SELECTION_FIELDS = {
    "request_id",
    "request_fingerprint",
    "view_id",
    "raw_point_count",
    "selected_point_count",
    "rejected_point_count",
    "slice",
    "coordinate_ranges",
    "plane_axes",
    "coordinates",
    "quantities",
}


def interpolate_field_slice(
    *,
    request: object,
    selection: object,
) -> dict[str, Any]:
    """Collapse duplicate sample locations and interpolate every quantity."""
    request_value = validate_field_evidence_request(request)
    if not isinstance(selection, Mapping) or set(selection) != _SELECTION_FIELDS:
        raise ValueError("selection is not a canonical field-slice selection")
    selection_value = dict(selection)
    if selection_value["request_id"] != request_value["request_id"] or selection_value[
        "request_fingerprint"
    ] != request_value["request_fingerprint"]:
        raise ValueError("selection request identity does not match")
    if len(
        [
            view
            for view in request_value["views"]
            if view["view_id"] == selection_value["view_id"]
        ]
    ) != 1:
        raise ValueError("selection view identity does not match the request")
    if selection_value["slice"] != request_value["slice"]:
        raise ValueError("selection slice does not match the request")

    try:
        import numpy as np
        from scipy.interpolate import griddata
        from scipy.spatial import QhullError
    except ImportError as exc:  # pragma: no cover - exercised by deployment probes
        raise RuntimeError("SciPy and NumPy are required for field interpolation") from exc

    raw_count = selection_value["raw_point_count"]
    selected_count = selection_value["selected_point_count"]
    rejected_count = selection_value["rejected_point_count"]
    if (
        isinstance(raw_count, bool)
        or isinstance(selected_count, bool)
        or isinstance(rejected_count, bool)
        or not all(isinstance(value, int) for value in (raw_count, selected_count, rejected_count))
        or selected_count <= 0
        or raw_count != selected_count + rejected_count
    ):
        raise ValueError("selection point counts are invalid")

    slice_axis = request_value["slice"]["axis"]
    plane_axes = [axis for axis in ("x", "y", "z") if axis != slice_axis]
    if selection_value["plane_axes"] != plane_axes:
        raise ValueError("selection plane axes do not match the request")
    coordinates = selection_value["coordinates"]
    if not isinstance(coordinates, Mapping) or set(coordinates) != {"x", "y", "z"}:
        raise ValueError("selection coordinates must contain exactly x, y, and z")
    coordinate_arrays: dict[str, Any] = {}
    for axis in ("x", "y", "z"):
        values = np.asarray(coordinates[axis])
        if (
            values.ndim != 1
            or values.size != selected_count
            or values.dtype.kind not in "fiu"
            or not np.all(np.isfinite(values))
        ):
            raise ValueError(f"selection coordinates.{axis} are invalid")
        coordinate_arrays[axis] = values.astype(np.float64, copy=False)

    expressions = [item["name"] for item in request_value["expressions"]]
    quantities = selection_value["quantities"]
    if not isinstance(quantities, Mapping) or set(quantities) != set(expressions):
        raise ValueError("selection quantities do not match the request")
    quantity_arrays: dict[str, Any] = {}
    for name in expressions:
        values = np.asarray(quantities[name])
        if (
            values.ndim != 1
            or values.size != selected_count
            or values.dtype.kind not in "fiu"
            or not np.all(np.isfinite(values))
        ):
            raise ValueError(f"selection quantities.{name} are invalid")
        quantity_arrays[name] = values.astype(np.float64, copy=False)

    points = np.column_stack(
        (coordinate_arrays[plane_axes[0]], coordinate_arrays[plane_axes[1]])
    )
    unique_points, inverse, duplicate_counts = np.unique(
        points, axis=0, return_inverse=True, return_counts=True
    )
    unique_count = int(unique_points.shape[0])
    collapsed_count = selected_count - unique_count
    collapsed_quantities: dict[str, Any] = {}
    for name in expressions:
        sums = np.zeros(unique_count, dtype=np.float64)
        np.add.at(sums, inverse, quantity_arrays[name])
        collapsed_quantities[name] = sums / duplicate_counts

    method = request_value["grid"]["interpolation"]
    if method == "linear":
        if unique_count < 3:
            raise ValueError("linear interpolation requires at least three unique points")
        if np.linalg.matrix_rank(unique_points - np.mean(unique_points, axis=0)) < 2:
            raise ValueError("linear interpolation unique points must not be collinear")

    rows, columns = request_value["grid"]["shape"]
    column_axis, row_axis = plane_axes
    column_bounds = request_value["coordinate_bounds"][column_axis]
    row_bounds = request_value["coordinate_bounds"][row_axis]
    column_coordinates = np.linspace(column_bounds[0], column_bounds[1], columns)
    row_coordinates = np.linspace(row_bounds[0], row_bounds[1], rows)
    target_column, target_row = np.meshgrid(column_coordinates, row_coordinates)
    grids: dict[str, Any] = {}
    try:
        for name in expressions:
            grids[name] = np.asarray(
                griddata(
                    unique_points,
                    collapsed_quantities[name],
                    (target_column, target_row),
                    method=method,
                ),
                dtype=np.float64,
            )
    except (QhullError, ValueError) as exc:
        raise ValueError(f"{method} field interpolation failed: {exc}") from exc

    missing_mask = np.isnan(grids[expressions[0]])
    if any(np.any(np.isinf(grid)) for grid in grids.values()):
        raise ValueError("field interpolation produced infinite values")
    if any(not np.array_equal(np.isnan(grid), missing_mask) for grid in grids.values()):
        raise ValueError("field interpolation produced inconsistent missing-cell masks")
    missing_count = int(missing_mask.sum())
    covered_count = request_value["grid_point_count"] - missing_count
    return {
        "request_id": request_value["request_id"],
        "request_fingerprint": request_value["request_fingerprint"],
        "view_id": selection_value["view_id"],
        "raw_point_count": raw_count,
        "selected_point_count": selected_count,
        "unique_point_count": unique_count,
        "collapsed_duplicate_point_count": collapsed_count,
        "axis_coordinates": {
            column_axis: column_coordinates,
            row_axis: row_coordinates,
        },
        "quantity_grids": grids,
        "covered_grid_point_count": covered_count,
        "missing_grid_point_count": missing_count,
        "interpolation": method,
    }


__all__ = ["interpolate_field_slice"]
