"""Background manual-index build and publication tests."""

from __future__ import annotations

from pathlib import Path

import fitz
import pytest

from comsol_mcp.knowledge.lexical_manual import search_index, validate_index_file
from settings_gui.manual_index import ManualIndexBuildTask


def _write_pdf(path: Path, text: str) -> None:
    document = fitz.open()
    try:
        page = document.new_page()
        page.insert_text((72, 72), text)
        document.save(path)
    finally:
        document.close()


def test_background_build_reports_progress_and_publishes_valid_index(tmp_path):
    pdf_root = tmp_path / "manuals"
    pdf_root.mkdir()
    _write_pdf(pdf_root / "guide.pdf", "Periodic Structure manual search content")
    target = tmp_path / "index" / "manuals.sqlite3"
    task = ManualIndexBuildTask(pdf_root=pdf_root, index_path=target)

    task.start()

    assert task.wait(timeout=15) is True
    events = task.drain_events()
    result = next(event for event in events if event["event"] == "result")
    assert result["pdf_count"] == 1
    assert result["page_count"] == 1
    assert validate_index_file(target)["integrity_check"] == "ok"
    assert search_index("Periodic Structure", index_path=target)["count"] == 1
    assert not task.temporary_path.exists()


def test_background_build_rejects_invalid_paths_before_start(tmp_path):
    task = ManualIndexBuildTask(
        pdf_root=tmp_path / "missing",
        index_path=tmp_path / "manuals.sqlite3",
    )

    with pytest.raises(ValueError, match="PDF root"):
        task.start()

    assert task.running is False
