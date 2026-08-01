from __future__ import annotations

from copy import deepcopy

import numpy as np
import pytest
from src.evidence.field_bundle import normalize_field_evidence_request
from src.evidence.field_interpolation import interpolate_field_slice
from src.evidence.field_sampling import select_field_slice_samples

from development_kit.tests.test_field_bundle import _request


def _selection(*, method: str = "linear", triangular: bool = False):
    raw = _request(paired=False, png=False)
    raw["grid"]["shape"] = [9, 11]
    raw["grid"]["interpolation"] = method
    raw["limits"]["max_grid_points"] = 200
    request = normalize_field_evidence_request(raw)
    if triangular:
        x = np.array([-1.0, 1.0, -1.0])
        y = np.array([-1.5, -1.5, 1.5])
    else:
        x = np.array([-1.0, 1.0, -1.0, 1.0])
        y = np.array([-1.5, -1.5, 1.5, 1.5])
    z = np.full(x.shape, 0.5)
    selection = select_field_slice_samples(
        request=request,
        view_id="on",
        coordinates={"x": x, "y": y, "z": z},
        quantities={
            "electric_norm": x + 2.0 * y,
            "magnetic_norm": 3.0 * x - y,
        },
    )
    return request, selection


def test_linear_interpolation_reproduces_planar_fields_on_declared_grid():
    request, selection = _selection()
    result = interpolate_field_slice(request=request, selection=selection)
    x = result["axis_coordinates"]["x"]
    y = result["axis_coordinates"]["y"]
    xx, yy = np.meshgrid(x, y)

    assert np.allclose(result["quantity_grids"]["electric_norm"], xx + 2.0 * yy)
    assert np.allclose(result["quantity_grids"]["magnetic_norm"], 3.0 * xx - yy)
    assert result["missing_grid_point_count"] == 0
    assert result["covered_grid_point_count"] == request["grid_point_count"]


def test_linear_interpolation_preserves_convex_hull_gaps():
    request, selection = _selection(triangular=True)
    result = interpolate_field_slice(request=request, selection=selection)

    assert 0 < result["missing_grid_point_count"] < request["grid_point_count"]
    assert (
        np.isnan(result["quantity_grids"]["electric_norm"]).sum()
        == result["missing_grid_point_count"]
    )


def test_nearest_interpolation_has_complete_coverage():
    request, selection = _selection(method="nearest", triangular=True)
    result = interpolate_field_slice(request=request, selection=selection)

    assert result["missing_grid_point_count"] == 0
    assert all(np.all(np.isfinite(grid)) for grid in result["quantity_grids"].values())


def test_exact_duplicate_locations_are_averaged_before_interpolation():
    request, selection = _selection(method="nearest")
    for axis in ("x", "y", "z"):
        selection["coordinates"][axis] = np.append(
            selection["coordinates"][axis], selection["coordinates"][axis][0]
        )
    for name in selection["quantities"]:
        selection["quantities"][name] = np.append(
            selection["quantities"][name], selection["quantities"][name][0] + 2.0
        )
    selection["selected_point_count"] += 1
    selection["raw_point_count"] += 1

    result = interpolate_field_slice(request=request, selection=selection)

    assert result["unique_point_count"] == 4
    assert result["collapsed_duplicate_point_count"] == 1
    x_coordinates = result["axis_coordinates"]["x"]
    y_coordinates = result["axis_coordinates"]["y"]
    column = int(np.flatnonzero(x_coordinates == selection["coordinates"]["x"][0])[0])
    row = int(np.flatnonzero(y_coordinates == selection["coordinates"]["y"][0])[0])
    for name, values in selection["quantities"].items():
        assert result["quantity_grids"][name][row, column] == pytest.approx(
            (values[0] + values[-1]) / 2.0
        )


def test_selection_identity_shape_and_counts_fail_closed():
    request, selection = _selection()
    wrong_request = deepcopy(selection)
    wrong_request["request_fingerprint"] = "0" * 64
    wrong_view = deepcopy(selection)
    wrong_view["view_id"] = "missing"
    wrong_count = deepcopy(selection)
    wrong_count["raw_point_count"] += 1
    wrong_shape = deepcopy(selection)
    wrong_shape["coordinates"]["x"] = np.ones(2)
    unknown = deepcopy(selection)
    unknown["callback"] = "arbitrary"

    for value, message in (
        (wrong_request, "request identity"),
        (wrong_view, "view identity"),
        (wrong_count, "point counts"),
        (wrong_shape, "coordinates.x are invalid"),
        (unknown, "canonical field-slice selection"),
    ):
        with pytest.raises(ValueError, match=message):
            interpolate_field_slice(request=request, selection=value)


def test_linear_interpolation_rechecks_unique_geometry_after_duplicate_collapse():
    request, selection = _selection()
    selection["coordinates"]["x"] = np.array([-1.0, 0.0, 1.0, 1.0])
    selection["coordinates"]["y"] = np.array([-1.5, 0.0, 1.5, 1.5])

    with pytest.raises(ValueError, match="unique points must not be collinear"):
        interpolate_field_slice(request=request, selection=selection)


def test_interpolation_rejects_impossible_or_oversized_standalone_counts():
    request, selection = _selection()
    impossible = deepcopy(selection)
    impossible["selected_point_count"] = 5
    impossible["rejected_point_count"] = -1
    oversized = deepcopy(selection)
    oversized["raw_point_count"] = request["limits"]["max_raw_points"] + 1
    oversized["rejected_point_count"] = (
        oversized["raw_point_count"] - oversized["selected_point_count"]
    )

    for value in (impossible, oversized):
        with pytest.raises(ValueError, match="point counts"):
            interpolate_field_slice(request=request, selection=value)


def test_interpolation_revalidates_bounds_slice_and_declared_ranges():
    request, selection = _selection()
    reordered = deepcopy(selection)
    permutation = np.array([3, 0, 2, 1])
    for values in (reordered["coordinates"], reordered["quantities"]):
        for name in values:
            values[name] = values[name][permutation]
    assert interpolate_field_slice(request=request, selection=reordered)["unique_point_count"] == 4

    outside = deepcopy(selection)
    outside["coordinates"]["x"][0] = -2.0
    wrong_slice = deepcopy(selection)
    wrong_slice["coordinates"]["z"][0] = 0.7
    wrong_range = deepcopy(selection)
    wrong_range["coordinate_ranges"]["x"][1] -= 0.25
    wrong_unit = deepcopy(selection)
    wrong_unit["coordinate_ranges"]["unit"] = "m"

    for value, message in (
        (outside, "requested bounds"),
        (wrong_slice, "requested slice"),
        (wrong_range, "selected coordinates"),
        (wrong_unit, "unit does not match"),
    ):
        with pytest.raises(ValueError, match=message):
            interpolate_field_slice(request=request, selection=value)
