"""Bounded lexical search for the local COMSOL PDF manuals.

The production index lives on an ASCII-only path and contains one row per PDF
page.  Search and page reads run in a short-lived worker process so a damaged
SQLite database cannot block the COMSOL MCP control process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import multiprocessing as mp
import os
import re
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from comsol_mcp.settings import LEXICAL_DOCS_INDEX_ENV
from comsol_mcp.utils.control_plane import measured_call

DEFAULT_INDEX_DIR: Path | None = None
DEFAULT_INDEX_PATH: Path | None = None
DEFAULT_PDF_DIR = Path(__file__).resolve().parents[2] / "pdf"
SCHEMA_VERSION = "1"
SEARCH_TIMEOUT_SECONDS = 2.0
READ_TIMEOUT_SECONDS = 3.0
QUERY_ALIASES = {
    # ClientAPI identifiers are often rendered as spaced GUI labels in manuals.
    "periodicstructure": '"Periodic Structure"',
    "alpha1_inc": '"first" AND "angle" AND "incidence" AND "periodic"',
}
QUERY_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "can",
    "do",
    "does",
    "for",
    "from",
    "how",
    "i",
    "in",
    "is",
    "it",
    "of",
    "on",
    "please",
    "the",
    "this",
    "to",
    "use",
    "using",
    "what",
    "when",
    "where",
    "with",
}

ProgressCallback = Callable[[dict[str, object]], None]
CancelCallback = Callable[[], bool]


class IndexBuildCancelled(RuntimeError):
    """Raised before publication when the caller cancels an index build."""


def _check_cancelled(cancelled: CancelCallback | None) -> None:
    if cancelled is not None and cancelled():
        raise IndexBuildCancelled("manual index build was cancelled")


def _emit_progress(
    progress: ProgressCallback | None,
    *,
    stage: str,
    percent: int,
    processed_files: int,
    total_files: int,
    processed_pages: int,
    total_pages: int,
    current_source: str | None = None,
) -> None:
    if progress is None:
        return
    progress(
        {
            "stage": stage,
            "percent": max(0, min(int(percent), 100)),
            "processed_files": int(processed_files),
            "total_files": int(total_files),
            "processed_pages": int(processed_pages),
            "total_pages": int(total_pages),
            "current_source": current_source,
        }
    )


def _is_ascii_path(path: Path) -> bool:
    try:
        str(path.resolve()).encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def _normalize_text(text: str) -> str:
    text = text.replace("\x00", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _page_heading(text: str) -> str:
    for line in text.splitlines():
        candidate = re.sub(r"\s+", " ", line).strip()
        if candidate and not candidate.isdigit():
            return candidate[:180]
    return ""


def _open_index(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        uri = path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=0.25)
    else:
        connection = sqlite3.connect(path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    return connection


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=FULL;
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE pages (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            module TEXT NOT NULL,
            page INTEGER NOT NULL,
            heading TEXT NOT NULL,
            text TEXT NOT NULL,
            UNIQUE(source, page)
        );
        CREATE VIRTUAL TABLE pages_fts USING fts5(
            source UNINDEXED,
            module UNINDEXED,
            page UNINDEXED,
            heading,
            text,
            tokenize='unicode61'
        );
        CREATE INDEX pages_source_page ON pages(source, page);
        """
    )


def build_index_from_records(
    records: Iterable[Mapping[str, object]],
    index_path: str | Path,
    *,
    corpus_fingerprint: str = "test-corpus",
    temporary_path: str | Path | None = None,
    progress: ProgressCallback | None = None,
    cancelled: CancelCallback | None = None,
    total_files: int = 0,
    total_pages: int = 0,
) -> dict:
    """Atomically build an FTS index from normalized page records."""
    target = Path(index_path)
    if not _is_ascii_path(target):
        raise ValueError("The lexical index path must contain ASCII characters only")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = (
        Path(temporary_path)
        if temporary_path is not None
        else target.with_suffix(target.suffix + f".tmp-{os.getpid()}")
    )
    if temporary.parent.resolve() != target.parent.resolve() or not temporary.name.startswith(
        target.name + ".tmp-"
    ):
        raise ValueError("temporary index must be a uniquely named sibling of the target")
    if not _is_ascii_path(temporary):
        raise ValueError("The temporary lexical index path must contain ASCII characters only")
    if temporary.exists():
        temporary.unlink()

    connection = _open_index(temporary)
    count = 0
    try:
        _create_schema(connection)
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            [
                ("schema_version", SCHEMA_VERSION),
                ("corpus_fingerprint", corpus_fingerprint),
                ("built_at_epoch", str(time.time())),
            ],
        )
        for record in records:
            _check_cancelled(cancelled)
            text = _normalize_text(str(record["text"]))
            if not text:
                continue
            source = str(record["source"]).replace("\\", "/")
            module = str(record["module"])
            page = int(record["page"])
            heading = str(record.get("heading") or _page_heading(text))
            cursor = connection.execute(
                "INSERT INTO pages(source, module, page, heading, text) VALUES (?, ?, ?, ?, ?)",
                (source, module, page, heading, text),
            )
            connection.execute(
                "INSERT INTO pages_fts(rowid, source, module, page, heading, text) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (cursor.lastrowid, source, module, str(page), heading, text),
            )
            count += 1
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            ("page_count", str(count)),
        )
        connection.commit()
    except Exception:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise
    else:
        connection.close()
    try:
        _check_cancelled(cancelled)
        _emit_progress(
            progress,
            stage="validating",
            percent=94,
            processed_files=total_files,
            total_files=total_files,
            processed_pages=count,
            total_pages=total_pages or count,
        )
        validation = validate_index_file(temporary, expected_page_count=count)
        _check_cancelled(cancelled)
        _emit_progress(
            progress,
            stage="publishing",
            percent=99,
            processed_files=total_files,
            total_files=total_files,
            processed_pages=count,
            total_pages=total_pages or count,
        )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "success": True,
        "index_path": str(target),
        "page_count": count,
        "schema_version": SCHEMA_VERSION,
        "corpus_fingerprint": corpus_fingerprint,
        "validation": validation,
    }


def _pdf_snapshot(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return (
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        stat.st_ino,
    )


def _pdf_fingerprint(
    pdf_dir: Path,
    pdf_files: Sequence[Path],
    snapshots: Mapping[Path, tuple[int, int, int, int]],
) -> str:
    digest = hashlib.sha256()
    for path in pdf_files:
        size, mtime_ns, _ctime_ns, _inode = snapshots[path]
        relative = path.relative_to(pdf_dir).as_posix()
        digest.update(f"{relative}\0{size}\0{mtime_ns}\n".encode("utf-8"))
    return digest.hexdigest()


def build_index_from_pdfs(
    pdf_dir: str | Path = DEFAULT_PDF_DIR,
    index_path: str | Path | None = DEFAULT_INDEX_PATH,
    *,
    temporary_path: str | Path | None = None,
    progress: ProgressCallback | None = None,
    cancelled: CancelCallback | None = None,
) -> dict:
    """Extract all PDF pages once and atomically build the production index."""
    if index_path is None:
        raise ValueError("index_path must be explicitly configured")
    source_root = Path(pdf_dir).resolve()
    pdf_files = sorted(source_root.rglob("*.pdf"))
    if not pdf_files:
        raise FileNotFoundError(f"No PDF manuals found below {source_root}")
    snapshots = {path: _pdf_snapshot(path) for path in pdf_files}
    corpus_fingerprint = _pdf_fingerprint(source_root, pdf_files, snapshots)
    _emit_progress(
        progress,
        stage="scanning",
        percent=0,
        processed_files=0,
        total_files=len(pdf_files),
        processed_pages=0,
        total_pages=0,
    )
    import pymupdf

    page_counts: dict[Path, int] = {}
    total_pages = 0
    for index, pdf_path in enumerate(pdf_files, start=1):
        _check_cancelled(cancelled)
        if _pdf_snapshot(pdf_path) != snapshots[pdf_path]:
            raise RuntimeError("PDF manual changed before page counting")
        with pymupdf.open(pdf_path) as document:
            page_counts[pdf_path] = int(document.page_count)
        total_pages += page_counts[pdf_path]
        _emit_progress(
            progress,
            stage="scanning",
            percent=max(1, round(10 * index / len(pdf_files))),
            processed_files=index,
            total_files=len(pdf_files),
            processed_pages=0,
            total_pages=total_pages,
            current_source=pdf_path.relative_to(source_root).as_posix(),
        )

    processed_pages = 0

    def records():
        nonlocal processed_pages
        for file_index, pdf_path in enumerate(pdf_files, start=1):
            _check_cancelled(cancelled)
            expected_snapshot = snapshots[pdf_path]
            if _pdf_snapshot(pdf_path) != expected_snapshot:
                raise RuntimeError(f"PDF manual changed before extraction: {pdf_path}")
            source = pdf_path.relative_to(source_root).as_posix()
            module = source.split("/", 1)[0]
            with pymupdf.open(pdf_path) as document:
                for page_number, page in enumerate(document, start=1):
                    _check_cancelled(cancelled)
                    text = _normalize_text(page.get_text("text"))
                    processed_pages += 1
                    _emit_progress(
                        progress,
                        stage="extracting",
                        percent=10 + round(83 * processed_pages / max(total_pages, 1)),
                        processed_files=file_index - 1,
                        total_files=len(pdf_files),
                        processed_pages=processed_pages,
                        total_pages=total_pages,
                        current_source=source,
                    )
                    if text:
                        yield {
                            "source": source,
                            "module": module,
                            "page": page_number,
                            "heading": _page_heading(text),
                            "text": text,
                        }
            if _pdf_snapshot(pdf_path) != expected_snapshot:
                raise RuntimeError(f"PDF manual changed during extraction: {pdf_path}")

    result = build_index_from_records(
        records(),
        index_path,
        corpus_fingerprint=corpus_fingerprint,
        temporary_path=temporary_path,
        progress=progress,
        cancelled=cancelled,
        total_files=len(pdf_files),
        total_pages=total_pages,
    )
    result["pdf_count"] = len(pdf_files)
    result["pdf_dir"] = str(source_root)
    _emit_progress(
        progress,
        stage="complete",
        percent=100,
        processed_files=len(pdf_files),
        total_files=len(pdf_files),
        processed_pages=total_pages,
        total_pages=total_pages,
    )
    return result


def _query_parts(query: str) -> list[str]:
    alias = QUERY_ALIASES.get(query.strip().casefold())
    if alias:
        return re.findall(r'"([^"\n]+)"', alias)
    phrases = re.findall(r'"([^"\n]+)"', query)
    remainder = re.sub(r'"[^"\n]+"', " ", query)
    terms = re.findall(r"[\w.:-]+", remainder, flags=re.UNICODE)
    significant_terms = [
        term for term in terms if term.casefold() not in QUERY_STOP_WORDS and len(term) > 1
    ]
    parts = phrases + significant_terms
    if not parts:
        raise ValueError("query must contain at least one searchable term")
    # Bound pathological agent prompts while retaining the earliest technical terms.
    return parts[:16]


def _fts_query(query: str, operator: str = "AND") -> str:
    parts = _query_parts(query)
    escaped = [part.replace('"', '""') for part in parts]
    return f" {operator} ".join(f'"{part}"' for part in escaped)


def _coverage_matches(parts: Sequence[str], text: str) -> list[str]:
    """Match FTS phrases across the same whitespace boundaries as unicode61."""
    haystack = " ".join(text.casefold().split())
    return [part for part in parts if " ".join(part.casefold().split()) in haystack]


def _validated_index_metadata(connection: sqlite3.Connection) -> dict[str, str]:
    metadata = dict(connection.execute("SELECT key, value FROM metadata"))
    missing = {"schema_version", "corpus_fingerprint", "page_count"} - set(metadata)
    if missing:
        raise ValueError(
            f"Manual index metadata is incomplete ({sorted(missing)}); rebuild the index"
        )
    if metadata["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Manual index schema is unsupported; rebuild it with the current package")
    if not metadata["corpus_fingerprint"]:
        raise ValueError("Manual index corpus fingerprint is empty; rebuild the index")
    try:
        page_count = int(metadata["page_count"])
    except (TypeError, ValueError) as exc:
        raise ValueError("Manual index page count is invalid; rebuild the index") from exc
    if page_count < 0:
        raise ValueError("Manual index page count is invalid; rebuild the index")
    return metadata


def validate_index_file(
    index_path: str | Path,
    *,
    expected_page_count: int | None = None,
) -> dict[str, object]:
    """Validate a complete lexical index before it can be published or enabled."""
    path = Path(index_path)
    if not path.is_file():
        raise FileNotFoundError("manual index does not exist")
    with closing(_open_index(path, readonly=True)) as connection:
        metadata = _validated_index_metadata(connection)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError("manual index integrity check failed")
        pages = int(connection.execute("SELECT COUNT(*) FROM pages").fetchone()[0])
        fts_pages = int(connection.execute("SELECT COUNT(*) FROM pages_fts").fetchone()[0])
    declared = int(metadata["page_count"])
    if pages != declared or fts_pages != declared:
        raise ValueError("manual index row counts do not match its metadata")
    if expected_page_count is not None and declared != int(expected_page_count):
        raise ValueError("manual index page count differs from the completed build")
    return {
        "success": True,
        "schema_version": metadata["schema_version"],
        "corpus_fingerprint": metadata["corpus_fingerprint"],
        "page_count": declared,
        "integrity_check": "ok",
    }


def search_index(
    query: str,
    *,
    module: str | None = None,
    limit: int = 5,
    source: str | None = None,
    page_start: int | None = None,
    page_end: int | None = None,
    index_path: str | Path | None = DEFAULT_INDEX_PATH,
    mode: str = "auto",
) -> dict:
    """Search an index, relaxing long natural-language queries when necessary."""
    if index_path is None:
        raise ValueError("index_path must be explicitly configured")
    path = Path(index_path)
    if not path.is_file():
        raise FileNotFoundError(
            f"Manual index not found at {path}. Build it with "
            "python -m comsol_mcp.knowledge.lexical_manual build"
        )
    limit = max(1, min(int(limit), 20))
    mode = mode.strip().lower()
    if mode not in {"auto", "exact"}:
        raise ValueError("mode must be 'auto' or 'exact'")
    parts = _query_parts(query)
    strict_query = _fts_query(query, "AND")
    clauses = ["pages_fts MATCH ?"]
    filter_parameters: list[object] = []
    if module:
        clauses.append("module = ?")
        filter_parameters.append(module)
    if source:
        clauses.append("source = ?")
        filter_parameters.append(source.replace("\\", "/"))
    if page_start is not None:
        clauses.append("CAST(page AS INTEGER) >= ?")
        filter_parameters.append(int(page_start))
    if page_end is not None:
        clauses.append("CAST(page AS INTEGER) <= ?")
        filter_parameters.append(int(page_end))
    sql = f"""
        SELECT source, module, CAST(page AS INTEGER) AS page, heading,
               snippet(pages_fts, 4, '[', ']', ' ... ', 36) AS snippet,
               bm25(pages_fts, 0.0, 0.0, 0.0, 2.0, 1.0) AS rank,
               heading || '\n' || text AS match_text
        FROM pages_fts
        WHERE {" AND ".join(clauses)}
        ORDER BY rank, source, page
        LIMIT ?
    """
    with closing(_open_index(path, readonly=True)) as connection:
        metadata = _validated_index_metadata(connection)
        rows = [
            dict(row)
            for row in connection.execute(
                sql,
                [strict_query, *filter_parameters, limit],
            )
        ]
        strategy = "strict_and"
        if not rows and mode == "auto" and len(parts) > 1:
            relaxed_query = _fts_query(query, "OR")
            candidates = [
                dict(row)
                for row in connection.execute(
                    sql,
                    [relaxed_query, *filter_parameters, max(50, limit * 20)],
                )
            ]
            minimum_matches = max(1, min(3, math.ceil(len(parts) * 0.35)))
            scored = []
            for row in candidates:
                matched = _coverage_matches(parts, row["match_text"])
                row["matched_terms"] = matched
                row["coverage"] = len(matched) / len(parts)
                if len(matched) >= minimum_matches:
                    scored.append(row)
            scored.sort(key=lambda row: (-len(row["matched_terms"]), row["rank"]))
            rows = scored[:limit]
            strategy = "relaxed_coverage_bm25"
    for row in rows:
        match_text = row.pop("match_text")
        row.setdefault("matched_terms", _coverage_matches(parts, match_text))
        row.setdefault("coverage", len(row["matched_terms"]) / len(parts))
        row["coverage"] = round(row["coverage"], 3)
    return {
        "success": True,
        "query": query,
        "mode": mode,
        "strategy": strategy,
        "relaxed": strategy != "strict_and",
        "fts_query": strict_query,
        "count": len(rows),
        "results": rows,
        "index": {
            "schema_version": metadata.get("schema_version"),
            "corpus_fingerprint": metadata.get("corpus_fingerprint"),
            "page_count": int(metadata.get("page_count", "0")),
        },
    }


def read_index_pages(
    source: str,
    pages: Sequence[int],
    *,
    index_path: str | Path | None = DEFAULT_INDEX_PATH,
) -> dict:
    """Read selected pages from the immutable lexical corpus."""
    if index_path is None:
        raise ValueError("index_path must be explicitly configured")
    path = Path(index_path)
    if not path.is_file():
        raise FileNotFoundError(f"Manual index not found at {path}")
    normalized_source = source.replace("\\", "/")
    requested = sorted({int(page) for page in pages})
    if not requested or len(requested) > 20 or any(page < 1 for page in requested):
        raise ValueError("pages must contain between 1 and 20 positive page numbers")
    placeholders = ",".join("?" for _ in requested)
    sql = (
        "SELECT source, module, page, heading, text FROM pages "
        f"WHERE source = ? AND page IN ({placeholders}) ORDER BY page"
    )
    with closing(_open_index(path, readonly=True)) as connection:
        _validated_index_metadata(connection)
        rows = [dict(row) for row in connection.execute(sql, [normalized_source, *requested])]
    return {
        "success": True,
        "source": normalized_source,
        "requested_pages": requested,
        "missing_pages": sorted(set(requested) - {row["page"] for row in rows}),
        "pages": rows,
    }


def run_bounded(operation: str, arguments: dict, timeout: float) -> dict:
    """Run one operation in a lightweight worker and enforce a hard deadline.

    ``multiprocessing.spawn`` re-imports the MCP server on Windows, making a
    lexical query pay the complete tool-registration startup cost.  Invoke a
    dedicated module instead so the worker imports only the SQLite search code.
    """
    command = [sys.executable, "-m", "comsol_mcp.knowledge.lexical_worker"]
    try:
        completed = subprocess.run(
            command,
            input=json.dumps(
                {"operation": operation, "arguments": arguments}, ensure_ascii=False
            ).encode("utf-8"),
            capture_output=True,
            timeout=max(0.05, float(timeout)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error_type": "TimeoutError",
            "error": f"Manual {operation} exceeded the {timeout:.2f}s deadline",
        }
    if completed.returncode != 0:
        return {
            "success": False,
            "error_type": "WorkerError",
            "error": completed.stderr.decode("utf-8", errors="replace").strip()
            or f"worker exited with code {completed.returncode}",
        }
    try:
        return json.loads(completed.stdout.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        return {
            "success": False,
            "error_type": "WorkerError",
            "error": "worker returned invalid JSON",
        }


def register_lexical_manual_tools(mcp) -> None:
    """Register dependency-free, bounded manual retrieval tools."""

    configured_index = os.environ.get(LEXICAL_DOCS_INDEX_ENV)
    index_path = Path(configured_index) if configured_index else None

    @mcp.tool()
    def manual_search(
        query: str,
        module: str | None = None,
        limit: int = 5,
        source: str | None = None,
        page_start: int | None = None,
        page_end: int | None = None,
        mode: str = "auto",
    ) -> dict:
        """Search local COMSOL manuals with robust lexical retrieval.

        Auto mode removes conversational stop words, tries strict AND matching,
        then relaxes zero-result long queries and reranks candidates by technical
        term coverage plus BM25. The response says when relaxation occurred. Use
        mode="exact" to disable fallback. This read-only call runs in an isolated
        worker with a hard deadline. Optional filters select a module, source PDF,
        or inclusive page interval. Use manual_read_pages for full page text.
        """
        if index_path is None:
            return {
                "success": False,
                "error_type": "ConfigurationError",
                "error": "manual index path is not configured",
            }
        return measured_call(
            "manual_search",
            lambda: run_bounded(
                "search",
                {
                    "query": query,
                    "module": module,
                    "limit": limit,
                    "source": source,
                    "page_start": page_start,
                    "page_end": page_end,
                    "mode": mode,
                    "index_path": str(index_path),
                },
                SEARCH_TIMEOUT_SECONDS,
            ),
        )

    @mcp.tool()
    def manual_read_pages(source: str, pages: list[int]) -> dict:
        """Read up to 20 exact pages from a source returned by manual search.

        The text comes from the immutable offline corpus. This read-only call is
        isolated from the COMSOL control process and has a hard deadline.
        """
        if index_path is None:
            return {
                "success": False,
                "error_type": "ConfigurationError",
                "error": "manual index path is not configured",
            }
        return measured_call(
            "manual_read_pages",
            lambda: run_bounded(
                "read",
                {
                    "source": source,
                    "pages": pages,
                    "index_path": str(index_path),
                },
                READ_TIMEOUT_SECONDS,
            ),
        )


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="build the SQLite FTS5 index")
    build.add_argument("--pdf-dir", default=str(DEFAULT_PDF_DIR))
    build.add_argument("--index", required=True)
    status = subparsers.add_parser("status", help="print index metadata")
    status.add_argument("--index", required=True)
    args = parser.parse_args()
    if args.command == "build":
        result = build_index_from_pdfs(args.pdf_dir, args.index)
    else:
        path = Path(args.index)
        if not path.is_file():
            result = {"success": False, "index_path": str(path), "error": "not found"}
        else:
            with closing(_open_index(path, readonly=True)) as connection:
                result = {
                    "success": True,
                    "index_path": str(path),
                    "metadata": dict(connection.execute("SELECT key, value FROM metadata")),
                }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    mp.freeze_support()
    _main()
