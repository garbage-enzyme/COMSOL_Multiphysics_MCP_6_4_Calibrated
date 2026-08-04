"""SDK-version-neutral decoding for in-process public MCP tool tests."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any


def _object_payload(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    nested = value.get("result")
    return nested if isinstance(nested, dict) else value


def decode_tool_result(result: Any) -> dict[str, Any]:
    """Decode v1 lists/tuples and v2 ``CallToolResult`` without changing payloads."""
    direct = _object_payload(result)
    if direct is not None:
        return direct

    for attribute in ("structured_content", "structuredContent"):
        structured = _object_payload(getattr(result, attribute, None))
        if structured is not None:
            return structured

    blocks: Any = getattr(result, "content", None)
    if blocks is None and isinstance(result, tuple) and len(result) == 2:
        structured = _object_payload(result[1])
        if structured is not None:
            return structured
        blocks = result[0]
    if blocks is None:
        blocks = result
    if not isinstance(blocks, Iterable) or isinstance(blocks, (str, bytes, dict)):
        raise ValueError("public MCP tool result did not contain content blocks")

    candidates = []
    for block in blocks:
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        payload = _object_payload(value)
        if payload is not None:
            candidates.append(payload)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise ValueError("public MCP tool result contained multiple JSON objects")
    raise ValueError("public MCP tool result did not contain one JSON object")


__all__ = ["decode_tool_result"]
