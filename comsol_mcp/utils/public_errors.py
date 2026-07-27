"""Stable path-free errors for public MCP responses and resources."""

from __future__ import annotations

from typing import Any


def public_error(reason_code: str, message: str) -> dict[str, Any]:
    """Build one bounded public failure without serializing an exception."""
    return {
        "success": False,
        "reason_code": reason_code,
        "error": message,
    }


__all__ = ["public_error"]
