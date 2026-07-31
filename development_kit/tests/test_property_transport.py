"""Tests for bounded generic clientapi property transport."""

from __future__ import annotations

import math

import pytest
from src.tools.property_transport import (
    MAX_LIST_ITEMS,
    MAX_PROPERTY_KEYS,
    MAX_SCALAR_BYTES,
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


@pytest.mark.parametrize(
    "name",
    [
        "Command",
        " FILEPATH ",
        "file_path",
        "file-path",
        "class.name",
        1,
        None,
    ],
)
def test_forbidden_property_names_reject_case_separator_whitespace_and_key_type(name):
    with pytest.raises((TypeError, ValueError)):
        validate_property_name(name)


def test_properties_enforce_key_and_list_limits():
    exact_keys = {f"key{i}": i for i in range(MAX_PROPERTY_KEYS)}
    assert validate_properties(exact_keys) == exact_keys
    assert normalize_property_value([0] * MAX_LIST_ITEMS) == [0] * MAX_LIST_ITEMS
    assert normalize_property_value([[0] * 64 for _ in range(64)]) == [[0] * 64 for _ in range(64)]
    with pytest.raises(ValueError, match="at most 64 keys"):
        validate_properties({f"key{i}": i for i in range(MAX_PROPERTY_KEYS + 1)})
    with pytest.raises(ValueError, match="at most 4096 items"):
        validate_properties({"values": [0] * (MAX_LIST_ITEMS + 1)})
    with pytest.raises(ValueError, match="at most 4096 scalar items"):
        normalize_property_value([[0] * 65 for _ in range(64)])


def test_empty_vector_is_supported_but_empty_matrix_rows_are_rejected():
    assert normalize_property_value([]) == []
    with pytest.raises(ValueError, match="rows must not be empty"):
        normalize_property_value([[]])


def test_property_scalars_are_bounded_before_aggregate_serialization():
    assert normalize_property_value("x" * MAX_SCALAR_BYTES) == "x" * MAX_SCALAR_BYTES
    with pytest.raises(ValueError, match="strings may contain at most"):
        normalize_property_value("x" * (MAX_SCALAR_BYTES + 1))
    with pytest.raises(ValueError, match="strings may contain at most"):
        normalize_property_value("界" * (MAX_SCALAR_BYTES // 3 + 1))
    with pytest.raises(ValueError, match="integers may contain at most"):
        normalize_property_value(1 << (MAX_SCALAR_BYTES * 4))
