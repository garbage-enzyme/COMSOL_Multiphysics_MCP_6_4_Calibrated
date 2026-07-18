"""Shared structural limits for public discovery and runtime arguments."""

from __future__ import annotations

import math
from copy import deepcopy
from functools import wraps
from typing import Any, Callable

MAX_PUBLIC_STRING_LENGTH = 16_384
MAX_PUBLIC_COLLECTION_ITEMS = 2_048
MAX_PUBLIC_OBJECT_FIELDS = 256
MAX_PUBLIC_NESTING_DEPTH = 64
MAX_PUBLIC_NUMBER_MAGNITUDE = 1.0e308


def bounded_public_schema(value: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded closed-object discovery schema without mutating input."""
    schema = deepcopy(value)

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        node_type = node.get("type")
        if node_type == "string":
            node.setdefault("maxLength", MAX_PUBLIC_STRING_LENGTH)
        elif node_type == "array":
            node.setdefault("maxItems", MAX_PUBLIC_COLLECTION_ITEMS)
        elif node_type == "object":
            node.setdefault("maxProperties", MAX_PUBLIC_OBJECT_FIELDS)
            if "properties" in node and "additionalProperties" not in node:
                node["additionalProperties"] = False
        elif node_type in {"integer", "number"}:
            node.setdefault("minimum", -MAX_PUBLIC_NUMBER_MAGNITUDE)
            node.setdefault("maximum", MAX_PUBLIC_NUMBER_MAGNITUDE)
        for nested in node.values():
            visit(nested)

    visit(schema)
    return schema


def validate_public_structure(value: Any, *, path: str = "arguments", depth: int = 0) -> None:
    """Apply the same generic structural limits before any tool side effect."""
    if depth > MAX_PUBLIC_NESTING_DEPTH:
        raise ValueError(f"{path} exceeds the public nesting limit")
    if isinstance(value, str):
        if len(value) > MAX_PUBLIC_STRING_LENGTH:
            raise ValueError(f"{path} exceeds the public string limit")
        return
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)) or abs(value) > MAX_PUBLIC_NUMBER_MAGNITUDE:
            raise ValueError(f"{path} must be a finite structurally bounded number")
        return
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_PUBLIC_COLLECTION_ITEMS:
            raise ValueError(f"{path} exceeds the public collection limit")
        for index, item in enumerate(value):
            validate_public_structure(item, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_PUBLIC_OBJECT_FIELDS:
            raise ValueError(f"{path} exceeds the public object-field limit")
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} object keys must be strings")
            validate_public_structure(key, path=f"{path}.<key>", depth=depth + 1)
            validate_public_structure(item, path=f"{path}.{key}", depth=depth + 1)
        return
    raise ValueError(f"{path} contains an unsupported public input type")


def structurally_guarded(function: Callable[..., Any]) -> Callable[..., Any]:
    """Validate all supplied arguments before entering a public tool function."""

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        validate_public_structure(args, path="arguments.positional")
        validate_public_structure(kwargs, path="arguments.named")
        return function(*args, **kwargs)

    return wrapped


__all__ = [
    "MAX_PUBLIC_COLLECTION_ITEMS",
    "MAX_PUBLIC_NESTING_DEPTH",
    "MAX_PUBLIC_NUMBER_MAGNITUDE",
    "MAX_PUBLIC_OBJECT_FIELDS",
    "MAX_PUBLIC_STRING_LENGTH",
    "bounded_public_schema",
    "structurally_guarded",
    "validate_public_structure",
]
