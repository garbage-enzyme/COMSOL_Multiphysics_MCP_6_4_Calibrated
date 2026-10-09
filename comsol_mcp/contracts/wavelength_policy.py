"""Explicit wavelength control and SI-unit agreement checks."""

from __future__ import annotations

import math
import re
from typing import Annotated, Any

from pydantic import ConfigDict, Field, with_config
from typing_extensions import TypedDict


@with_config(ConfigDict(extra="forbid", strict=True))
class StrictWavelengthPolicy(TypedDict):
    relative_tolerance: Annotated[float, Field(ge=0, strict=True, allow_inf_nan=False)]
    absolute_tolerance_m: Annotated[float, Field(ge=0, strict=True, allow_inf_nan=False)]
    dataset_tag: Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$", strict=True)]


_UNITS = {"m": 1.0, "mm": 1e-3, "um": 1e-6, "µm": 1e-6, "nm": 1e-9}
_VALUE = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\[([^\]]+)\]$")


def normalize_policy(value: Any) -> dict[str, Any]:
    keys = {"relative_tolerance", "absolute_tolerance_m", "dataset_tag"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("strict wavelength policy requires tolerances and dataset_tag only")
    result = dict(value)
    for name in ("relative_tolerance", "absolute_tolerance_m"):
        number = value[name]
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise ValueError("wavelength tolerance must be numeric")
        if not math.isfinite(number) or number < 0:
            raise ValueError("wavelength tolerance must be finite and nonnegative")
        result[name] = float(number)
    if result["relative_tolerance"] == result["absolute_tolerance_m"] == 0:
        raise ValueError("at least one wavelength tolerance must be positive")
    tag = value["dataset_tag"]
    if not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", tag):
        raise ValueError("dataset_tag must be a bounded COMSOL tag")
    return result


def requested_metres(value: Any, unit: str | None) -> float:
    if isinstance(value, str):
        match = _VALUE.fullmatch(value.strip())
        if match is None:
            raise ValueError("strict wavelength values require a numeric value and explicit unit")
        value, unit = float(match[1]), match[2]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or unit not in _UNITS:
        raise ValueError("strict wavelengths require m, mm, um, µm or nm")
    result = float(value) * _UNITS[unit]
    if not math.isfinite(result) or result <= 0:
        raise ValueError("requested wavelength must be positive and finite")
    return result


def verify_metres(requested: float, evaluated: Any, solved: Any, policy: dict[str, Any]) -> None:
    for actual in (evaluated, solved):
        if isinstance(actual, bool) or not isinstance(actual, (int, float)):
            raise ValueError("wavelength controls must be real scalar values")
        if not math.isfinite(actual) or actual <= 0:
            raise ValueError("wavelength controls must be positive and finite")
        limit = policy["absolute_tolerance_m"] + policy["relative_tolerance"] * abs(requested)
        if abs(requested - actual) > limit:
            raise ValueError("requested, evaluated and solved wavelengths do not agree")
