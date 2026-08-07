"""Isolated JSON-lines worker for cancellable lexical manual-index builds."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .lexical_manual import IndexBuildCancelled, build_index_from_pdfs

MAX_REQUEST_BYTES = 16 * 1024


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _error_payload(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, IndexBuildCancelled):
        code = "cancelled"
        message = "Index generation was cancelled. The previous index was preserved."
    elif isinstance(exc, FileNotFoundError):
        code = "pdfs_not_found"
        message = "No readable PDF manuals were found below the selected folder."
    elif isinstance(exc, ModuleNotFoundError):
        code = "pdf_dependency_missing"
        message = "PDF extraction support is not installed in this environment."
    elif isinstance(exc, ValueError):
        code = "index_configuration_invalid"
        message = "The PDF root or SQLite index destination is invalid."
    elif isinstance(exc, RuntimeError):
        code = "source_changed"
        message = "A source PDF changed during indexing. Retry after the files are stable."
    else:
        code = "index_build_failed"
        message = "The index could not be generated. Check access and free disk space."
    return {"event": "error", "reason_code": code, "message": message}


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if not raw or len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("request size is invalid")
        request = json.loads(raw.decode("utf-8"))
        if not isinstance(request, dict) or set(request) != {
            "pdf_root",
            "index_path",
            "temporary_path",
        }:
            raise ValueError("request shape is invalid")
        for key, value in request.items():
            if not isinstance(value, str) or not value or len(value) > 4096:
                raise ValueError(f"{key} is invalid")
        result = build_index_from_pdfs(
            Path(request["pdf_root"]),
            Path(request["index_path"]),
            temporary_path=Path(request["temporary_path"]),
            progress=lambda payload: _emit({"event": "progress", **payload}),
        )
        _emit(
            {
                "event": "result",
                "success": True,
                "pdf_count": result["pdf_count"],
                "page_count": result["page_count"],
                "schema_version": result["schema_version"],
                "corpus_fingerprint": result["corpus_fingerprint"],
            }
        )
        return 0
    except (OSError, RuntimeError, ValueError, ModuleNotFoundError, json.JSONDecodeError) as exc:
        _emit(_error_payload(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
