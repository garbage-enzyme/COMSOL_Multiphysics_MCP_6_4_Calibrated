"""Shared structural limits for public discovery and runtime arguments."""

from __future__ import annotations

import inspect
import math
from copy import deepcopy
from functools import wraps
from typing import Any, Callable

from pydantic import BaseModel

MAX_PUBLIC_STRING_LENGTH = 16_384
MAX_PUBLIC_COLLECTION_ITEMS = 2_048
MAX_PUBLIC_OBJECT_FIELDS = 256
MAX_PUBLIC_NESTING_DEPTH = 64
MAX_PUBLIC_NUMBER_MAGNITUDE = 1.0e308


def bounded_public_schema(value: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded closed-object discovery schema without mutating input."""
    active: set[int] = set()

    def validate_graph(node: Any, depth: int) -> None:
        if depth > MAX_PUBLIC_NESTING_DEPTH:
            raise ValueError("public schema exceeds the nesting limit")
        if not isinstance(node, (dict, list)):
            return
        identity = id(node)
        if identity in active:
            raise ValueError("public schema contains a cycle")
        active.add(identity)
        try:
            nested = node.values() if isinstance(node, dict) else node
            for item in nested:
                validate_graph(item, depth + 1)
        finally:
            active.remove(identity)

    validate_graph(value, 0)
    schema = deepcopy(value)

    def clamp_maximum(node: dict[str, Any], key: str, limit: int | float) -> None:
        current = node.get(key)
        if current is None:
            node[key] = limit
        elif isinstance(current, bool) or not isinstance(current, (int, float)):
            raise ValueError(f"public schema {key} must be numeric")
        else:
            node[key] = min(current, limit)

    def clamp_minimum(node: dict[str, Any], key: str, limit: int | float) -> None:
        current = node.get(key)
        if current is None:
            node[key] = limit
        elif isinstance(current, bool) or not isinstance(current, (int, float)):
            raise ValueError(f"public schema {key} must be numeric")
        else:
            node[key] = max(current, limit)

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        node_type = node.get("type")
        node_types = node_type if isinstance(node_type, list) else [node_type]
        if "string" in node_types:
            clamp_maximum(node, "maxLength", MAX_PUBLIC_STRING_LENGTH)
        if "array" in node_types:
            clamp_maximum(node, "maxItems", MAX_PUBLIC_COLLECTION_ITEMS)
        if "object" in node_types:
            clamp_maximum(node, "maxProperties", MAX_PUBLIC_OBJECT_FIELDS)
            if node.get("additionalProperties") is True:
                node["additionalProperties"] = False
            elif "additionalProperties" not in node:
                node["additionalProperties"] = False
        if "integer" in node_types or "number" in node_types:
            clamp_minimum(node, "minimum", -MAX_PUBLIC_NUMBER_MAGNITUDE)
            clamp_maximum(node, "maximum", MAX_PUBLIC_NUMBER_MAGNITUDE)
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
    if isinstance(value, int):
        if abs(value) > MAX_PUBLIC_NUMBER_MAGNITUDE:
            raise ValueError(f"{path} must be a finite structurally bounded number")
        return
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) > MAX_PUBLIC_NUMBER_MAGNITUDE:
            raise ValueError(f"{path} must be a finite structurally bounded number")
        return
    if isinstance(value, BaseModel):
        validate_public_structure(value.model_dump(mode="python"), path=path, depth=depth)
        return
    if isinstance(value, list):
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
    parameters = tuple(inspect.signature(function).parameters.values())

    def has_bound_receiver(args: tuple[Any, ...]) -> bool:
        if not parameters or not args or parameters[0].name not in {"self", "cls"}:
            return False
        receiver = args[0]
        owner = receiver if isinstance(receiver, type) else type(receiver)
        try:
            attribute = inspect.getattr_static(owner, function.__name__)
        except AttributeError:
            return False
        if isinstance(attribute, (classmethod, staticmethod)):
            attribute = attribute.__func__
        return inspect.unwrap(attribute) is function

    def supplied_positional(args: tuple[Any, ...]) -> list[Any]:
        return list(args[1:] if has_bound_receiver(args) else args)

    if inspect.iscoroutinefunction(function):

        @wraps(function)
        async def async_wrapped(*args: Any, **kwargs: Any) -> Any:
            validate_public_structure(supplied_positional(args), path="arguments.positional")
            validate_public_structure(kwargs, path="arguments.named")
            return await function(*args, **kwargs)

        return async_wrapped

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        validate_public_structure(supplied_positional(args), path="arguments.positional")
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
