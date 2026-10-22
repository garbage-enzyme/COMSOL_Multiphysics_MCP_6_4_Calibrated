"""Bounded primitive validation for native structure adapter contracts."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from comsol_mcp.durable import validate_finite_json

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _object(value: object, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object with string keys")
    result = dict(value)
    if set(result) != fields:
        missing = sorted(fields - set(result))
        unknown = sorted(set(result) - fields)
        raise ValueError(f"{name} fields mismatch; missing={missing}, unknown={unknown}")
    return result


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a bounded identifier")
    return value


def _text(value: object, name: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a bounded nonempty string")
    return value.strip()


def _bounded_json(value: object, name: str, maximum_bytes: int) -> Any:
    validate_finite_json(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(encoded) > maximum_bytes:
        raise ValueError(f"{name} exceeds the bounded JSON size")
    return json.loads(encoded)


def _timestamp(value: object, name: str) -> str:
    text = _text(value, name, maximum=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include an explicit timezone")
    return text
