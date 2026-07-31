import shutil
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from src.knowledge import lexical_manual as manual_module
from src.knowledge.lexical_manual import (
    build_index_from_records,
    read_index_pages,
    run_bounded,
    search_index,
)
from src.tools.session import session_manager


@pytest.fixture()
def manual_index() -> Path:
    root = Path("D:/comsol_docs_fts_test") / uuid.uuid4().hex
    index = root / "manuals.sqlite3"
    build_index_from_records(
        [
            {
                "source": "Wave_Optics_Module/WaveOpticsModuleUsersGuide.pdf",
                "module": "Wave_Optics_Module",
                "page": 151,
                "heading": "Periodic Ports",
                "text": "PeriodicStructure uses a homogeneous medium adjacent to a periodic port.",
            },
            {
                "source": "Wave_Optics_Module/WaveOpticsModuleUsersGuide.pdf",
                "module": "Wave_Optics_Module",
                "page": 136,
                "heading": "Periodic Structure",
                "text": "Set the first angle of incidence for the periodic port mode.",
            },
            {
                "source": "COMSOL_Multiphysics/COMSOL_ProgrammingReferenceManual.pdf",
                "module": "COMSOL_Multiphysics",
                "page": 812,
                "heading": "Geometry methods",
                "text": "The getUpDown method returns adjacent domain information for boundaries.",
            },
            {
                "source": "COMSOL_Multiphysics/COMSOL_ReferenceManual.pdf",
                "module": "COMSOL_Multiphysics",
                "page": 2033,
                "heading": "Copy Face",
                "text": "CopyFace copies a mesh from source faces to destination faces.",
            },
        ],
        index,
        corpus_fingerprint="fixture-v1",
    )
    try:
        yield index
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_exact_and_term_search_returns_compact_page_references(manual_index: Path):
    result = search_index("periodic homogeneous", index_path=manual_index)

    assert result["success"] is True
    assert result["count"] == 1
    assert result["results"][0]["page"] == 151
    assert "[PeriodicStructure]" not in result["results"][0]["snippet"]
    assert result["index"]["corpus_fingerprint"] == "fixture-v1"


def test_phrase_module_and_page_filters(manual_index: Path):
    result = search_index(
        '"adjacent domain"',
        module="COMSOL_Multiphysics",
        page_start=800,
        page_end=900,
        index_path=manual_index,
    )

    assert [row["page"] for row in result["results"]] == [812]


def test_clientapi_alias_finds_manual_ui_terminology(manual_index: Path):
    result = search_index("alpha1_inc", index_path=manual_index)

    assert result["fts_query"] == '"first" AND "angle" AND "incidence" AND "periodic"'
    assert [row["page"] for row in result["results"]] == [136]


def test_long_agent_query_relaxes_and_reranks_by_term_coverage(manual_index: Path):
    query = "How do I configure CopyFace source and destination mesh faces in COMSOL?"

    exact = search_index(query, mode="exact", index_path=manual_index)
    automatic = search_index(query, mode="auto", index_path=manual_index)

    assert exact["count"] == 0
    assert automatic["strategy"] == "relaxed_coverage_bm25"
    assert automatic["relaxed"] is True
    assert automatic["results"][0]["page"] == 2033
    assert {"CopyFace", "source", "destination", "mesh", "faces"} <= set(
        automatic["results"][0]["matched_terms"]
    )


def test_read_pages_reports_missing_pages(manual_index: Path):
    result = read_index_pages(
        "COMSOL_Multiphysics/COMSOL_ReferenceManual.pdf",
        [2033, 2034],
        index_path=manual_index,
    )

    assert [row["page"] for row in result["pages"]] == [2033]
    assert result["missing_pages"] == [2034]


@pytest.mark.parametrize("operation", ["search", "read"])
def test_readers_reject_an_obsolete_index_schema(manual_index: Path, operation):
    with sqlite3.connect(manual_index) as connection:
        connection.execute("UPDATE metadata SET value = 'obsolete' WHERE key = 'schema_version'")
        connection.commit()

    with pytest.raises(ValueError, match="schema is unsupported"):
        if operation == "search":
            search_index("CopyFace", index_path=manual_index)
        else:
            read_index_pages(
                "COMSOL_Multiphysics/COMSOL_ReferenceManual.pdf",
                [2033],
                index_path=manual_index,
            )


def test_pdf_index_build_rejects_a_manual_changed_during_extraction(
    ascii_tmp_path,
    monkeypatch,
):
    pdf_root = ascii_tmp_path / "pdf"
    pdf_root.mkdir()
    source = pdf_root / "manual.pdf"
    source.write_bytes(b"initial-pdf")
    index = ascii_tmp_path / "manuals.sqlite3"

    class Page:
        def get_text(self, _format):
            source.write_bytes(b"changed-pdf-with-a-different-size")
            return "Searchable manual page"

    class Document:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            return iter([Page()])

    monkeypatch.setitem(sys.modules, "fitz", SimpleNamespace(open=lambda _path: Document()))

    with pytest.raises(RuntimeError, match="changed during extraction"):
        manual_module.build_index_from_pdfs(pdf_root, index)

    assert not index.exists()
    assert not list(ascii_tmp_path.glob("manuals.sqlite3.tmp-*"))


def test_read_only_lexical_connections_close_deterministically(manual_index: Path, monkeypatch):
    real_open = manual_module._open_index
    tracked = []

    class TrackingConnection:
        def __init__(self, connection):
            self.connection = connection
            self.closed = False

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def close(self):
            self.connection.close()
            self.closed = True

    def tracking_open(*args, **kwargs):
        connection = TrackingConnection(real_open(*args, **kwargs))
        tracked.append(connection)
        return connection

    monkeypatch.setattr(manual_module, "_open_index", tracking_open)

    search_index("CopyFace", index_path=manual_index)
    read_index_pages(
        "COMSOL_Multiphysics/COMSOL_ReferenceManual.pdf",
        [2033],
        index_path=manual_index,
    )

    assert len(tracked) == 2
    assert all(connection.closed for connection in tracked)


def test_bounded_worker_searches_without_loading_comsol(manual_index: Path):
    result = run_bounded(
        "search",
        {"query": "CopyFace", "index_path": str(manual_index)},
        timeout=3.0,
    )

    assert result["success"] is True
    assert result["results"][0]["page"] == 2033


def test_bounded_worker_enforces_deadline(manual_index: Path):
    result = run_bounded(
        "search",
        {"query": "CopyFace", "index_path": str(manual_index)},
        timeout=0.001,
    )

    assert result["success"] is False
    assert result["error_type"] == "TimeoutError"
    started = time.perf_counter()
    status = session_manager.get_status()
    assert time.perf_counter() - started < 0.1
    assert "connected" in status


def test_non_ascii_index_path_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="ASCII"):
        build_index_from_records([], tmp_path / "中文" / "manuals.sqlite3")


def test_index_replacement_failure_removes_completed_temporary_database(
    ascii_tmp_path, monkeypatch
):
    target = ascii_tmp_path / "manuals.sqlite3"

    def fail_replace(_source, destination):
        assert Path(destination) == target
        raise PermissionError("injected replacement failure")

    monkeypatch.setattr(manual_module.os, "replace", fail_replace)

    with pytest.raises(PermissionError, match="replacement failure"):
        build_index_from_records(
            [
                {
                    "source": "manual.pdf",
                    "module": "manual",
                    "page": 1,
                    "heading": "Heading",
                    "text": "Searchable content",
                }
            ],
            target,
        )

    assert not target.exists()
    assert not list(ascii_tmp_path.glob("manuals.sqlite3.tmp-*"))
