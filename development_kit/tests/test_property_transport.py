"""Tests for bounded generic clientapi property transport."""

from __future__ import annotations

import math

import pytest
from src.tools.property_transport import (
    MAX_LIST_ITEMS,
    MAX_PROPERTY_KEYS,
    normalize_property_value,
    validate_properties,
    validate_property_name,
)


def test_properties_accept_scalar_vector_and_matrix_values():
    properties = {
        "label": "sample",
        "active": True,
        "count": 2,
        "scale": 0.25,
        "optional": None,
        "size": ["1", 2, 3.0],
        "basis": [[1.0, 0.0], [0.0, 1.0]],
    }

    assert validate_properties(properties) == properties


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_properties_reject_nonfinite_numbers(value):
    with pytest.raises(ValueError, match="finite"):
        validate_properties({"value": value})


@pytest.mark.parametrize(
    "value",
    [
        [1.0, math.inf],
        [1.0, -math.inf],
        [[1.0, math.nan]],
        [[math.inf], [-math.inf]],
    ],
)
def test_properties_reject_nested_nonfinite_numbers(value):
    with pytest.raises(ValueError, match="finite"):
        validate_properties({"value": value})


@pytest.mark.parametrize(
    "value, message",
    [
        ([1, [2]], "cannot mix"),
        ([[1], [[2]]], "nesting depth"),
        ([[1, 2], [3]], "rectangular"),
        ([object()], "JSON scalars"),
    ],
)
def test_properties_reject_unknown_container_shapes(value, message):
    with pytest.raises((TypeError, ValueError), match=message):
        normalize_property_value(value)


@pytest.mark.parametrize(
    "name",
    ["__class__", "run()", "feature.tag", "filename", "script", "command"],
)
def test_properties_reject_callable_and_file_property_names(name):
    with pytest.raises(ValueError):
        validate_property_name(name)


def test_properties_enforce_key_and_list_limits():
    with pytest.raises(ValueError, match="at most 64 keys"):
        validate_properties({f"key{i}": i for i in range(MAX_PROPERTY_KEYS + 1)})
    with pytest.raises(ValueError, match="at most 4096 items"):
        validate_properties({"values": [0] * (MAX_LIST_ITEMS + 1)})
