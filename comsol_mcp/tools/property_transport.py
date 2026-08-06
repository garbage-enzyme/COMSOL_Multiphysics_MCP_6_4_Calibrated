"""Bounded JSON transport shared by constrained clientapi property tools."""

from __future__ import annotations

import json
import math
import re
from typing import TypeAlias

JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list[JSONScalar] | list[list[JSONScalar]]

MAX_PROPERTY_KEYS = 64
MAX_PROPERTY_KEY_LENGTH = 128
MAX_LIST_ITEMS = 4096
MAX_SCALAR_BYTES = 64 * 1024
MAX_SERIALIZED_BYTES = 256 * 1024

_PROPERTY_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_FORBIDDEN_PROPERTY_NAMES = frozenset(
    {
        "class",
        "classname",
        "cmd",
        "command",
        "executable",
        "file",
        "filename",
        "filepath",
        "function",
        "method",
        "script",
    }
)


def validate_property_name(name: str) -> str:
    """Validate bounded syntax and reject known callable/file property controls."""
    if not isinstance(name, str):
        raise TypeError("property names must be strings")
    if not name or len(name) > MAX_PROPERTY_KEY_LENGTH:
        raise ValueError(f"property names must contain 1-{MAX_PROPERTY_KEY_LENGTH} characters")
    if not _PROPERTY_NAME.fullmatch(name):
        raise ValueError(f"invalid clientapi property name: {name!r}")
    if name.lower().replace("_", "") in _FORBIDDEN_PROPERTY_NAMES:
        raise ValueError(f"file/callable clientapi property is forbidden: {name!r}")
    return name


def _normalize_scalar(value: object) -> JSONScalar:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if len(value) > MAX_SCALAR_BYTES:
            raise ValueError(f"property strings may contain at most {MAX_SCALAR_BYTES} bytes")
        try:
            byte_count = len(value.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError("property strings must be valid UTF-8") from exc
        if byte_count > MAX_SCALAR_BYTES:
            raise ValueError(f"property strings may contain at most {MAX_SCALAR_BYTES} bytes")
        return value
    if isinstance(value, int):
        if _integer_serialized_bytes(value) > MAX_SCALAR_BYTES:
            raise ValueError(f"property integers may contain at most {MAX_SCALAR_BYTES} bytes")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("property numbers must be finite")
        return value
    raise TypeError("property values must be JSON scalars, scalar lists, or scalar matrices")


def _integer_decimal_digits(value: int) -> int:
    magnitude = abs(value)
    if magnitude == 0:
        return 1
    digits = (magnitude.bit_length() * 30103) // 100000 + 1
    power = 10 ** (digits - 1)
    while magnitude < power:
        digits -= 1
        power //= 10
    while magnitude >= power * 10:
        digits += 1
        power *= 10
    return digits


def _integer_serialized_bytes(value: int) -> int:
    return _integer_decimal_digits(value) + (1 if value < 0 else 0)


def _serialized_value_bytes(value: JSONValue) -> int:
    if value is None:
        return 4
    if isinstance(value, bool):
        return 4 if value else 5
    if isinstance(value, str):
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    if isinstance(value, int):
        return _integer_serialized_bytes(value)
    if isinstance(value, float):
        return len(json.dumps(value, allow_nan=False).encode("ascii"))
    return 2 + max(0, len(value) - 1) + sum(_serialized_value_bytes(item) for item in value)


def _enforce_serialized_value_bound(value: JSONValue) -> JSONValue:
    if _serialized_value_bytes(value) > MAX_SERIALIZED_BYTES:
        raise ValueError(f"serialized property value exceeds {MAX_SERIALIZED_BYTES} bytes")
    return value


def normalize_property_value(value: object) -> JSONValue:
    """Normalize one bounded scalar, vector, or rectangular scalar matrix."""
    if not isinstance(value, (list, tuple)):
        return _enforce_serialized_value_bound(_normalize_scalar(value))
    if len(value) > MAX_LIST_ITEMS:
        raise ValueError(f"property lists may contain at most {MAX_LIST_ITEMS} items")
    if not value:
        return []

    contains_rows = [isinstance(item, (list, tuple)) for item in value]
    if any(contains_rows) and not all(contains_rows):
        raise TypeError("property lists cannot mix scalars and nested rows")
    if not any(contains_rows):
        return _enforce_serialized_value_bound([_normalize_scalar(item) for item in value])

    rows = []
    width: int | None = None
    item_count = 0
    for raw_row in value:
        row = list(raw_row)
        if not row:
            raise ValueError("property matrix rows must not be empty")
        if len(row) > MAX_LIST_ITEMS:
            raise ValueError(f"property rows may contain at most {MAX_LIST_ITEMS} items")
        if any(isinstance(item, (list, tuple)) for item in row):
            raise TypeError("property nesting depth cannot exceed two")
        if width is None:
            width = len(row)
        elif len(row) != width:
            raise ValueError("property matrices must be rectangular")
        item_count += len(row)
        if item_count > MAX_LIST_ITEMS:
            raise ValueError(f"property matrices may contain at most {MAX_LIST_ITEMS} scalar items")
        rows.append([_normalize_scalar(item) for item in row])
    return _enforce_serialized_value_bound(rows)


def validate_properties(properties: object | None) -> dict[str, JSONValue]:
    """Validate and size-bound a property mapping before clientapi access."""
    if properties is None:
        return {}
    if not isinstance(properties, dict):
        raise TypeError("properties must be a JSON object")
    if len(properties) > MAX_PROPERTY_KEYS:
        raise ValueError(f"properties may contain at most {MAX_PROPERTY_KEYS} keys")

    normalized = {
        validate_property_name(name): normalize_property_value(value)
        for name, value in properties.items()
    }
    payload_bytes = (
        2
        + max(0, len(normalized) - 1)
        + sum(
            len(json.dumps(name, ensure_ascii=False).encode("utf-8"))
            + 1
            + _serialized_value_bytes(value)
            for name, value in normalized.items()
        )
    )
    if payload_bytes > MAX_SERIALIZED_BYTES:
        raise ValueError(f"serialized properties exceed {MAX_SERIALIZED_BYTES} bytes")
    return normalized


__all__ = [
    "JSONScalar",
    "JSONValue",
    "MAX_LIST_ITEMS",
    "MAX_PROPERTY_KEYS",
    "MAX_PROPERTY_KEY_LENGTH",
    "MAX_SCALAR_BYTES",
    "MAX_SERIALIZED_BYTES",
    "normalize_property_value",
    "validate_properties",
    "validate_property_name",
]
