"""Finite JSON validation and explicitly versioned canonical identities."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

MAX_CANONICAL_DEPTH = 64
MAX_CANONICAL_NODES = 1_000_000
MAX_CANONICAL_STRING_BYTES = 1_048_576
MAX_CANONICAL_INTEGER_BITS = 4096
_DOMAIN_PREFIX = b"comsol-mcp.identity\x00v2\x00"


def validate_finite_json(value: Any) -> None:
    """Reject non-JSON, non-finite, over-deep, or excessive values."""
    remaining = MAX_CANONICAL_NODES

    def visit(item: Any, depth: int) -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0:
            raise ValueError("JSON value exceeds the canonical node limit")
        if depth > MAX_CANONICAL_DEPTH:
            raise ValueError("JSON value exceeds the canonical nesting limit")
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, str):
            try:
                encoded = json.dumps(item, ensure_ascii=False).encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("JSON strings must be valid UTF-8") from exc
            if len(encoded) - 2 > MAX_CANONICAL_STRING_BYTES:
                raise ValueError("JSON string exceeds the canonical byte limit")
            return
        if isinstance(item, int):
            if item.bit_length() > MAX_CANONICAL_INTEGER_BITS:
                raise ValueError("JSON integer exceeds the canonical bit limit")
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("JSON numbers must be finite")
            return
        if isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested, depth + 1)
            return
        if isinstance(item, dict):
            for key, nested in item.items():
                if not isinstance(key, str):
                    raise ValueError("JSON object keys must be strings")
                visit(key, depth + 1)
                visit(nested, depth + 1)
            return
        raise ValueError(f"Unsupported JSON value type: {type(item).__name__}")

    visit(value, 0)


def canonical_json_v1(value: Any) -> bytes:
    """Return the legacy canonical bytes without normalization or defaults."""
    validate_finite_json(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256_v1(value: Any) -> str:
    """Return the legacy SHA-256 over canonical version-one bytes."""
    return hashlib.sha256(canonical_json_v1(value)).hexdigest()


def domain_sha256_v2(domain: str, value: Any) -> str:
    """Return a version-two domain-separated identity for new schemas only."""
    if not isinstance(domain, str) or not domain or len(domain) > 128:
        raise ValueError("identity domain must be a bounded nonempty string")
    try:
        domain_bytes = domain.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("identity domain must contain ASCII characters only") from exc
    payload = _DOMAIN_PREFIX + domain_bytes + b"\x00" + canonical_json_v1(value)
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "MAX_CANONICAL_DEPTH",
    "MAX_CANONICAL_NODES",
    "MAX_CANONICAL_STRING_BYTES",
    "MAX_CANONICAL_INTEGER_BITS",
    "canonical_json_v1",
    "canonical_sha256_v1",
    "domain_sha256_v2",
    "validate_finite_json",
]
