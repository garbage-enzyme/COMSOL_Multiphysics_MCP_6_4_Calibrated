"""Lightweight JSON worker for bounded lexical-manual operations."""

from __future__ import annotations

import json
import sys

from .lexical_manual import read_index_pages, search_index


def main() -> None:
    try:
        request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        operation = request["operation"]
        arguments = request["arguments"]
        if operation == "search":
            result = search_index(**arguments)
        elif operation == "read":
            result = read_index_pages(**arguments)
        else:
            raise ValueError(f"Unknown lexical manual operation: {operation}")
    except Exception as exc:
        result = {
            "success": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    try:
        encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
    except TypeError, ValueError:
        encoded = json.dumps(
            {
                "success": False,
                "error_type": "SerializationError",
                "error": "worker result is not JSON-serializable",
            },
            ensure_ascii=False,
        ).encode("utf-8")
    sys.stdout.buffer.write(encoded)


if __name__ == "__main__":
    main()
