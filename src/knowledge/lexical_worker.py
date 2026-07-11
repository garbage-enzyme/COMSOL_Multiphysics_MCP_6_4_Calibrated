"""Lightweight JSON worker for bounded lexical-manual operations."""

from __future__ import annotations

import json
import sys

from .lexical_manual import read_index_pages, search_index


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    try:
        request = json.load(sys.stdin)
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
    json.dump(result, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
