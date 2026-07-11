"""Bounded lexical search for the local COMSOL PDF manuals.

The production index lives on an ASCII-only path and contains one row per PDF
page.  Search and page reads run in a short-lived worker process so a damaged
SQLite database cannot block the COMSOL MCP control process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import os
import re
import sqlite3
import subprocess
import sys
import time
import math
from pathlib import Path
from typing import Iterable, Mapping, Sequence


DEFAULT_INDEX_DIR = Path("D:/comsol_docs_fts")
DEFAULT_INDEX_PATH = DEFAULT_INDEX_DIR / "manuals.sqlite3"
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
    "a", "an", "and", "are", "can", "do", "does", "for", "from", "how",
    "i", "in", "is", "it", "of", "on", "please", "the", "this", "to",
    "use", "using", "what", "when", "where", "with",
}


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
) -> dict:
    """Atomically build an FTS index from normalized page records."""
    target = Path(index_path)
    if not _is_ascii_path(target):
        raise ValueError("The lexical index path must contain ASCII characters only")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + f".tmp-{os.getpid()}")
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
    os.replace(temporary, target)
    return {
        "success": True,
        "index_path": str(target),
        "page_count": count,
        "schema_version": SCHEMA_VERSION,
        "corpus_fingerprint": corpus_fingerprint,
    }


def _pdf_fingerprint(pdf_dir: Path, pdf_files: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for path in pdf_files:
        stat = path.stat()
        relative = path.relative_to(pdf_dir).as_posix()
        digest.update(f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode("utf-8"))
    return digest.hexdigest()


def build_index_from_pdfs(
    pdf_dir: str | Path = DEFAULT_PDF_DIR,
    index_path: str | Path = DEFAULT_INDEX_PATH,
) -> dict:
    """Extract all PDF pages once and atomically build the production index."""
    source_root = Path(pdf_dir).resolve()
    pdf_files = sorted(source_root.rglob("*.pdf"))
    if not pdf_files:
        raise FileNotFoundError(f"No PDF manuals found below {source_root}")

    def records():
        import fitz

        for pdf_path in pdf_files:
            source = pdf_path.relative_to(source_root).as_posix()
            module = source.split("/", 1)[0]
            with fitz.open(pdf_path) as document:
                for page_number, page in enumerate(document, start=1):
                    text = _normalize_text(page.get_text("text"))
                    if text:
                        yield {
                            "source": source,
                            "module": module,
                            "page": page_number,
                            "heading": _page_heading(text),
                            "text": text,
                        }

    result = build_index_from_records(
        records(),
        index_path,
        corpus_fingerprint=_pdf_fingerprint(source_root, pdf_files),
    )
    result["pdf_count"] = len(pdf_files)
    result["pdf_dir"] = str(source_root)
    return result


def _query_parts(query: str) -> list[str]:
    alias = QUERY_ALIASES.get(query.strip().casefold())
    if alias:
        return re.findall(r'"([^"\n]+)"', alias)
    phrases = re.findall(r'"([^"\n]+)"', query)
    remainder = re.sub(r'"[^"\n]+"', " ", query)
    terms = re.findall(r"[\w.:-]+", remainder, flags=re.UNICODE)
    significant_terms = [
        term for term in terms
        if term.casefold() not in QUERY_STOP_WORDS and len(term) > 1
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


def search_index(
    query: str,
    *,
    module: str | None = None,
    limit: int = 5,
    source: str | None = None,
    page_start: int | None = None,
    page_end: int | None = None,
    index_path: str | Path = DEFAULT_INDEX_PATH,
    mode: str = "auto",
) -> dict:
    """Search an index, relaxing long natural-language queries when necessary."""
    path = Path(index_path)
    if not path.is_file():
        raise FileNotFoundError(
            f"Manual index not found at {path}. Build it with "
            "python -m src.knowledge.lexical_manual build"
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
        WHERE {' AND '.join(clauses)}
        ORDER BY rank, source, page
        LIMIT ?
    """
    with _open_index(path, readonly=True) as connection:
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
                haystack = row["match_text"].casefold()
                matched = [part for part in parts if part.casefold() in haystack]
                row["matched_terms"] = matched
                row["coverage"] = len(matched) / len(parts)
                if len(matched) >= minimum_matches:
                    scored.append(row)
            scored.sort(key=lambda row: (-len(row["matched_terms"]), row["rank"]))
            rows = scored[:limit]
            strategy = "relaxed_coverage_bm25"
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
    for row in rows:
        haystack = row.pop("match_text").casefold()
        row.setdefault("matched_terms", [part for part in parts if part.casefold() in haystack])
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
    index_path: str | Path = DEFAULT_INDEX_PATH,
) -> dict:
    """Read selected pages from the immutable lexical corpus."""
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
    with _open_index(path, readonly=True) as connection:
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
    command = [sys.executable, "-m", "src.knowledge.lexical_worker"]
    try:
        completed = subprocess.run(
            command,
            input=json.dumps({"operation": operation, "arguments": arguments}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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
            "error": completed.stderr.strip() or f"worker exited with code {completed.returncode}",
        }
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {
            "success": False,
            "error_type": "WorkerError",
            "error": "worker returned invalid JSON",
        }


def register_lexical_manual_tools(mcp) -> None:
    """Register dependency-free, bounded manual retrieval tools."""

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
        return run_bounded(
            "search",
            {
                "query": query,
                "module": module,
                "limit": limit,
                "source": source,
                "page_start": page_start,
                "page_end": page_end,
                "mode": mode,
                "index_path": str(DEFAULT_INDEX_PATH),
            },
            SEARCH_TIMEOUT_SECONDS,
        )

    @mcp.tool()
    def manual_read_pages(source: str, pages: list[int]) -> dict:
        """Read up to 20 exact pages from a source returned by manual search.

        The text comes from the immutable offline corpus. This read-only call is
        isolated from the COMSOL control process and has a hard deadline.
        """
        return run_bounded(
            "read",
            {
                "source": source,
                "pages": pages,
                "index_path": str(DEFAULT_INDEX_PATH),
            },
            READ_TIMEOUT_SECONDS,
        )


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="build the SQLite FTS5 index")
    build.add_argument("--pdf-dir", default=str(DEFAULT_PDF_DIR))
    build.add_argument("--index", default=str(DEFAULT_INDEX_PATH))
    status = subparsers.add_parser("status", help="print index metadata")
    status.add_argument("--index", default=str(DEFAULT_INDEX_PATH))
    args = parser.parse_args()
    if args.command == "build":
        result = build_index_from_pdfs(args.pdf_dir, args.index)
    else:
        path = Path(args.index)
        if not path.is_file():
            result = {"success": False, "index_path": str(path), "error": "not found"}
        else:
            with _open_index(path, readonly=True) as connection:
                result = {
                    "success": True,
                    "index_path": str(path),
                    "metadata": dict(connection.execute("SELECT key, value FROM metadata")),
                }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    mp.freeze_support()
    _main()
