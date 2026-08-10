"""Host-neutral licensed acceptance resource contracts."""

import re
from pathlib import Path

import pytest

from development_kit.tests.integration import acceptance_resources


def test_acceptance_cores_are_caller_declared_and_live_bounded(monkeypatch):
    monkeypatch.setattr(acceptance_resources.os, "cpu_count", lambda: 4)

    assert acceptance_resources.required_acceptance_cores(
        {acceptance_resources.ACCEPTANCE_CORES_ENV: "3"}
    ) == 3
    with pytest.raises(RuntimeError, match="explicitly configured"):
        acceptance_resources.required_acceptance_cores({})
    with pytest.raises(ValueError, match="canonical positive integer"):
        acceptance_resources.required_acceptance_cores(
            {acceptance_resources.ACCEPTANCE_CORES_ENV: "01"}
        )
    with pytest.raises(ValueError, match="exceeds live host"):
        acceptance_resources.required_acceptance_cores(
            {acceptance_resources.ACCEPTANCE_CORES_ENV: "5"}
        )


def test_acceptance_cores_fail_closed_without_live_capacity(monkeypatch):
    monkeypatch.setattr(acceptance_resources.os, "cpu_count", lambda: None)

    with pytest.raises(RuntimeError, match="capacity is unavailable"):
        acceptance_resources.required_acceptance_cores(
            {acceptance_resources.ACCEPTANCE_CORES_ENV: "1"}
        )


def test_caller_acceptance_values_do_not_enter_tracked_product_text():
    root = Path(__file__).resolve().parents[2]
    text_suffixes = {
        ".json",
        ".md",
        ".ps1",
        ".psm1",
        ".py",
        ".toml",
        ".yaml",
        ".yml",
    }
    caller_cores = str(7 * 2)
    caller_commit_percent = str(9 * 10)
    patterns = (
        re.compile(rf"\b{caller_cores}\s+cores?\b", re.IGNORECASE),
        re.compile(
            rf"COMSOL_MCP_ACCEPTANCE_CORES[^\r\n]{{0,80}}[\"']{caller_cores}[\"']"
        ),
        re.compile(
            rf"\b{caller_commit_percent}%\s+(?:host\s+)?commit(?:ment)?(?:[- ]limit)?",
            re.IGNORECASE,
        ),
    )
    violations: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in text_suffixes:
            continue
        if ".git" in path.parts or "__pycache__" in path.parts:
            continue
        content = path.read_text(encoding="utf-8", errors="strict")
        for line_number, line in enumerate(content.splitlines(), start=1):
            if any(pattern.search(line) for pattern in patterns):
                violations.append(f"{path.relative_to(root).as_posix()}:{line_number}")
    assert violations == [], (
        "caller-owned workstation acceptance values entered tracked product text: "
        + ", ".join(violations)
    )
