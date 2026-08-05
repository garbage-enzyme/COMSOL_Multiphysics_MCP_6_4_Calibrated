"""Dependency-free strict validation for public JSON scalar values."""

from __future__ import annotations

import math
from typing import Any


def strict_json_number(
    value: Any,
    label: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> int | float:
    """Return a finite JSON number without accepting booleans or strings."""
    if positive and nonnegative:
        raise ValueError("positive and nonnegative constraints are mutually exclusive")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    number = value
    if isinstance(number, int):
        try:
            finite = math.isfinite(float(number))
        except OverflowError:
            finite = False
    else:
        finite = math.isfinite(number)
    if positive:
        if not finite or number <= 0:
            raise ValueError(f"{label} must be finite and positive")
    elif nonnegative:
        if not finite or number < 0:
            raise ValueError(f"{label} must be finite and non-negative")
    elif not finite:
        raise ValueError(f"{label} must be finite")
    return number


def strict_json_integer(
    value: Any,
    label: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Return an exact JSON integer without coercion or truncation."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} must not exceed {maximum}")
    return value


def strict_json_boolean(value: Any, label: str) -> bool:
    """Return an exact JSON boolean without equality-based coercion."""
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be boolean")
    return value


__all__ = ["strict_json_boolean", "strict_json_integer", "strict_json_number"]
