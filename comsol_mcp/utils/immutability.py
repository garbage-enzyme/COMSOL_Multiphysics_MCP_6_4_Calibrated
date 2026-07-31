"""Recursively immutable JSON-compatible snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class FrozenDict(dict):
    """A JSON-serializable dictionary that rejects mutation after construction."""

    def _immutable(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("frozen snapshot cannot be mutated")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable

    def __deepcopy__(self, _memo: dict[int, Any]) -> "FrozenDict":
        return self


def deep_freeze(value: Any) -> Any:
    """Copy JSON-like containers into recursively immutable equivalents."""
    if isinstance(value, Mapping):
        return FrozenDict((key, deep_freeze(item)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(item) for item in value)
    return value


def deep_thaw(value: Any) -> Any:
    """Return mutable JSON-compatible copies of recursively frozen values."""
    if isinstance(value, Mapping):
        return {key: deep_thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [deep_thaw(item) for item in value]
    return value


__all__ = ["FrozenDict", "deep_freeze", "deep_thaw"]
