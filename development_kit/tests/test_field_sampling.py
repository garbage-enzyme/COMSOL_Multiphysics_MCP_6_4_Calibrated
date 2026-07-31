from __future__ import annotations

from copy import deepcopy

import numpy as np
import pytest
from src.evidence.field_bundle import normalize_field_evidence_request
from src.evidence.field_sampling import select_field_slice_samples

from development_kit.tests.test_field_bundle import _request


def _samples(*, interpolation: str = "linear") -> tuple[dict, dict]:
    raw = _request(paired=False, png=False)
    raw["grid"]["interpolation"] = interpolation
    request = normalize_field_evidence_request(raw)
    x = np.array([-1.2, -0.8, 0.0, 0.8, 1.2, -0.8, 0.0, 0.8])
    y = np.array([0.0, -1.0, -1.0, -1.0, 0.0, 1.0, 1.0, 1.0])
    z = np.array([0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.7, 0.5])
    return request, {
        "request": request,
        "view_id": "on",
        "coordinates": {"x": x, "y": y, "z": z},
        "quantities": {
            "electric_norm": x**2 + y**2,
            "magnetic_norm": np.sqrt(x**2 + y**2 + 1.0),
        },
    }


def test_selection_applies_bounds_and_slice_tolerance_with_exact_counts():
    request, kwargs = _samples()
    result = select_field_slice_samples(**kwargs)

    assert result["raw_point_count"] == 8
    assert result["selected_point_count"] == 5
    assert result["rejected_point_count"] == 3
    assert result["plane_axes"] == ["x", "y"]
    assert np.all(np.abs(result["coordinates"]["z"] - 0.5) <= 0.01)
    np.testing.assert_allclose(result["coordinates"]["x"], [-0.8, 0.0, 0.8, -0.8, 0.8])
    np.testing.assert_allclose(result["coordinates"]["y"], [-1.0, -1.0, -1.0, 1.0, 1.0])
    np.testing.assert_allclose(result["coordinates"]["z"], [0.5] * 5)
    np.testing.assert_allclose(result["quantities"]["electric_norm"], [1.64, 1.0, 1.64, 1.64, 1.64])
    np.testing.assert_allclose(
        result["quantities"]["magnetic_norm"],
        [np.sqrt(2.64), np.sqrt(2.0), np.sqrt(2.64), np.sqrt(2.64), np.sqrt(2.64)],
    )
    assert result["request_fingerprint"] == request["request_fingerprint"]


def test_selection_includes_exact_spatial_and_slice_tolerance_boundaries():
    _, kwargs = _samples(interpolation="nearest")
    kwargs["coordinates"] = {
        "x": np.array([-1.0, 1.0, 0.0, 0.0]),
        "y": np.array([-1.5, 1.5, 0.0, 0.0]),
        "z": np.array([0.5, 0.5, 0.49, 0.51]),
    }
    kwargs["quantities"] = {
        "electric_norm": np.arange(4.0),
        "magnetic_norm": np.arange(4.0) + 10.0,
    }

    result = select_field_slice_samples(**kwargs)

    assert result["selected_point_count"] == 4
    for axis in ("x", "y", "z"):
        np.testing.assert_array_equal(
            result["coordinates"][axis], kwargs["coordinates"][axis]
        )
    for name in ("electric_norm", "magnetic_norm"):
        np.testing.assert_array_equal(
            result["quantities"][name], kwargs["quantities"][name]
        )


def test_selection_rejects_empty_and_too_few_linear_samples():
    _, empty = _samples()
    empty["coordinates"]["z"][:] = 0.8
    _, too_few = _samples()
    too_few["coordinates"]["z"][:] = 0.8
    too_few["coordinates"]["z"][:2] = 0.5

    with pytest.raises(ValueError, match="no field samples"):
        select_field_slice_samples(**empty)
    with pytest.raises(ValueError, match="at least three"):
        select_field_slice_samples(**too_few)


def test_linear_selection_rejects_no_axis_variation_and_collinearity():
    _, no_variation = _samples()
    no_variation["coordinates"]["y"][:] = 0.0
    _, collinear = _samples()
    collinear["coordinates"]["x"] = np.linspace(-0.8, 0.8, 8)
    collinear["coordinates"]["y"] = collinear["coordinates"]["x"]
    collinear["coordinates"]["z"][:] = 0.5

    with pytest.raises(ValueError, match="variation on both"):
        select_field_slice_samples(**no_variation)
    with pytest.raises(ValueError, match="must not be collinear"):
        select_field_slice_samples(**collinear)


def test_nearest_selection_allows_one_sample():
    _, kwargs = _samples(interpolation="nearest")
    kwargs["coordinates"]["z"][:] = 0.8
    kwargs["coordinates"]["z"][0] = 0.5
    kwargs["coordinates"]["x"][0] = 0.0

    result = select_field_slice_samples(**kwargs)

    assert result["selected_point_count"] == 1


def test_raw_arrays_must_be_finite_aligned_numeric_and_bounded():
    request, kwargs = _samples()
    nonfinite_coordinate = deepcopy(kwargs)
    nonfinite_coordinate["coordinates"]["x"][0] = np.nan
    nonfinite_quantity = deepcopy(kwargs)
    nonfinite_quantity["quantities"]["electric_norm"][0] = np.inf
    mismatched = deepcopy(kwargs)
    mismatched["coordinates"]["y"] = np.ones(2)
    oversized = deepcopy(kwargs)
    count = request["limits"]["max_raw_points"] + 1
    oversized["coordinates"] = {axis: np.zeros(count) for axis in ("x", "y", "z")}
    oversized["quantities"] = {name: np.zeros(count) for name in ("electric_norm", "magnetic_norm")}

    for value, message in (
        (nonfinite_coordinate, "only finite"),
        (nonfinite_quantity, "only finite"),
        (mismatched, "same length"),
        (oversized, "caller-declared point limit"),
    ):
        with pytest.raises(ValueError, match=message):
            select_field_slice_samples(**value)


@pytest.mark.parametrize(
    "target,replacement,match",
    [
        (("coordinates", "x"), np.array(["x"] * 8), "one-dimensional numeric"),
        (("coordinates", "y"), np.array([object()] * 8), "one-dimensional numeric"),
        (("coordinates", "z"), np.zeros((2, 4)), "one-dimensional numeric"),
        (("coordinates", "x"), np.array([]), "must not be empty"),
        (("quantities", "electric_norm"), np.array(["1"] * 8), "one-dimensional numeric"),
        (("quantities", "magnetic_norm"), np.zeros((2, 4)), "one-dimensional numeric"),
        (("quantities", "electric_norm"), np.array([]), "matching coordinates"),
    ],
)
def test_raw_arrays_reject_invalid_dtype_dimensionality_and_empty_values(
    target, replacement, match
):
    _, kwargs = _samples()
    kwargs[target[0]][target[1]] = replacement

    with pytest.raises(ValueError, match=match):
        select_field_slice_samples(**kwargs)


def test_oversized_coordinate_is_rejected_before_array_conversion():
    request, kwargs = _samples()

    class OversizedCoordinate:
        def __len__(self):
            return request["limits"]["max_raw_points"] + 1

        def __array__(self, *_args, **_kwargs):
            pytest.fail("oversized coordinate must not be converted")

    kwargs["coordinates"]["x"] = OversizedCoordinate()

    with pytest.raises(ValueError, match="caller-declared point limit"):
        select_field_slice_samples(**kwargs)


def test_coordinate_and_quantity_keys_are_exact_and_view_is_bound():
    _, kwargs = _samples()
    bad_view = deepcopy(kwargs)
    bad_view["view_id"] = "missing"

    for mutation in (
        lambda value: value["coordinates"].__setitem__("r", np.zeros(8)),
        lambda value: value["coordinates"].pop("z"),
    ):
        malformed = deepcopy(kwargs)
        mutation(malformed)
        with pytest.raises(ValueError, match="exactly x, y, and z"):
            select_field_slice_samples(**malformed)
    for mutation in (
        lambda value: value["quantities"].pop("magnetic_norm"),
        lambda value: value["quantities"].__setitem__("extra", np.zeros(8)),
    ):
        malformed = deepcopy(kwargs)
        mutation(malformed)
        with pytest.raises(ValueError, match="exactly the requested expressions"):
            select_field_slice_samples(**malformed)
    with pytest.raises(ValueError, match="exactly one requested view"):
        select_field_slice_samples(**bad_view)
