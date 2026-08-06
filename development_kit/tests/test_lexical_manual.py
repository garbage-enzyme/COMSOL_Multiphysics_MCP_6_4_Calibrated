import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from src.knowledge import lexical_manual as manual_module
from src.knowledge.lexical_manual import (
    IndexBuildCancelled,
    build_index_from_records,
    read_index_pages,
    run_bounded,
    search_index,
    validate_index_file,
)
from src.tools.session import session_manager


@pytest.fixture()
def manual_index(ascii_tmp_path) -> Path:
    root = ascii_tmp_path / "lexical"
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
            {
                "source": "Wave_Optics_Module/WaveOpticsModuleUsersGuide.pdf",
                "module": "Wave_Optics_Module",
                "page": 152,
                "heading": "Wrapped phrase",
                "text": "Periodic\nStructure supports bounded optical ports.",
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


def test_relaxed_phrase_coverage_matches_fts_whitespace(manual_index: Path):
    result = search_index(
        '"Periodic Structure" unavailable',
        mode="auto",
        index_path=manual_index,
    )

    assert result["strategy"] == "relaxed_coverage_bm25"
    wrapped = next(row for row in result["results"] if row["page"] == 152)
    assert "Periodic Structure" in wrapped["matched_terms"]


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
    with closing(sqlite3.connect(manual_index)) as connection:
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
        page_count = 1

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            return iter([Page()])

    opened = []

    def open_document(path):
        opened.append(path)
        return Document()

    monkeypatch.setitem(sys.modules, "fitz", SimpleNamespace(open=open_document))

    with pytest.raises(RuntimeError, match="changed during extraction"):
        manual_module.build_index_from_pdfs(pdf_root, index)

    assert len(opened) == 2
    assert all(os.path.samefile(opened_path, source) for opened_path in opened)
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


def test_bounded_worker_enforces_deadline(manual_index: Path, monkeypatch):
    def block_until_deadline(command, **kwargs):
        assert kwargs["timeout"] == 0.05
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(manual_module.subprocess, "run", block_until_deadline)
    result = run_bounded(
        "search",
        {"query": "CopyFace", "index_path": str(manual_index)},
        timeout=0.001,
    )

    assert result["success"] is False
    assert result["error_type"] == "TimeoutError"
    status = session_manager.get_status()
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


def test_cancel_before_publication_preserves_previous_valid_index(ascii_tmp_path):
    target = ascii_tmp_path / "manuals.sqlite3"
    build_index_from_records(
        [
            {
                "source": "old.pdf",
                "module": "manual",
                "page": 1,
                "heading": "Old",
                "text": "preserved old content",
            }
        ],
        target,
        corpus_fingerprint="old-corpus",
    )
    temporary = target.with_name(target.name + ".tmp-explicit")

    with pytest.raises(IndexBuildCancelled):
        build_index_from_records(
            [
                {
                    "source": "new.pdf",
                    "module": "manual",
                    "page": 1,
                    "heading": "New",
                    "text": "replacement content",
                }
            ],
            target,
            corpus_fingerprint="new-corpus",
            temporary_path=temporary,
            cancelled=lambda: True,
        )

    assert search_index("preserved", index_path=target)["count"] == 1
    assert not temporary.exists()


def test_completed_index_is_validated_before_publication(manual_index: Path):
    result = validate_index_file(manual_index, expected_page_count=5)

    assert result == {
        "success": True,
        "schema_version": "1",
        "corpus_fingerprint": "fixture-v1",
        "page_count": 5,
        "integrity_check": "ok",
    }


def test_pdf_build_emits_monotonic_stage_and_percentage_progress(ascii_tmp_path):
    import fitz

    pdf_root = ascii_tmp_path / "pdf-progress"
    pdf_root.mkdir()
    document = fitz.open()
    try:
        document.new_page().insert_text((72, 72), "searchable first page")
        document.new_page().insert_text((72, 72), "searchable second page")
        document.save(pdf_root / "guide.pdf")
    finally:
        document.close()
    events = []
    target = ascii_tmp_path / "progress.sqlite3"

    result = manual_module.build_index_from_pdfs(
        pdf_root,
        target,
        progress=events.append,
    )

    assert result["page_count"] == 2
    assert [event["stage"] for event in events if event["stage"] in {"validating", "publishing", "complete"}] == [
        "validating",
        "publishing",
        "complete",
    ]
    assert [event["percent"] for event in events] == sorted(
        event["percent"] for event in events
    )
    assert events[-1]["percent"] == 100
    assert events[-1]["total_pages"] == 2
