"""Isolate GUI tests from the host ProgramData directory."""

from __future__ import annotations

import os
import shutil

import pytest

from development_kit.tests.conftest import _create_ascii_temp_dir


@pytest.fixture(autouse=True)
def isolated_ascii_program_data(monkeypatch):
    root = _create_ascii_temp_dir()
    monkeypatch.setenv("PROGRAMDATA", str(root))
    if os.name != "nt":
        monkeypatch.setenv("HOME", str(root))
        for kind, suffix in (
            ("CONFIG", "config"),
            ("STATE", "state"),
            ("DATA", "data"),
            ("CACHE", "cache"),
        ):
            monkeypatch.setenv(f"XDG_{kind}_HOME", str(root / suffix))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)
