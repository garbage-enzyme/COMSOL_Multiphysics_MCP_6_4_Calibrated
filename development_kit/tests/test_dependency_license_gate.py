"""Dependency-license review and fail-closed receipt tests."""

from __future__ import annotations

import json
from datetime import date
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest

from development_kit.scripts.dependency_license_gate import (
    build_license_receipt,
    declared_runtime_dependencies,
    distribution_license_record,
    load_license_review,
)

ROOT = Path(__file__).parents[2]
PYPROJECT = ROOT / "pyproject.toml"
REVIEW = ROOT / "development_kit" / "release" / "dependency_license_review.json"


def _string_leaves(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _string_leaves(key)
            yield from _string_leaves(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _string_leaves(item)


def _normalized_path_text(value: str | Path) -> str:
    return str(value).replace("\\", "/").casefold()


def _reviewed_metadata(entries: list[dict]) -> dict[str, SimpleNamespace]:
    reviewed = {}
    for entry in entries:
        message = Message()
        message["Name"] = entry["dependency"]
        message["Version"] = "review-snapshot"
        for signal in entry["accepted_signals"]:
            kind, value = signal.split(":", 1)
            if kind == "license":
                message["License"] = value
            elif kind == "license-expression":
                message["License-Expression"] = value
            elif kind == "classifier":
                message["Classifier"] = value
        reviewed[entry["dependency"]] = SimpleNamespace(metadata=message)
    return reviewed


def test_committed_runtime_dependencies_match_reviewed_license_snapshot() -> None:
    review = json.loads(REVIEW.read_text(encoding="utf-8"))
    reviewed_metadata = _reviewed_metadata(review["entries"])
    dependencies = declared_runtime_dependencies(PYPROJECT)
    reviewed_on = date.fromisoformat(review["reviewed_on"])
    receipt = build_license_receipt(
        PYPROJECT,
        REVIEW,
        as_of=reviewed_on,
        distribution_provider=lambda name: reviewed_metadata[name],
    )

    assert receipt["status"] == "passed"
    assert receipt["as_of"] == review["reviewed_on"]
    assert receipt["dependency_count"] == len(dependencies)
    assert receipt["failures"] == []
    assert len(receipt["pyproject_sha256"]) == 64
    assert len(receipt["review_sha256"]) == 64
    receipt_strings = tuple(_normalized_path_text(value) for value in _string_leaves(receipt))
    sensitive_paths = (ROOT, PYPROJECT, REVIEW, Path.home())
    for sensitive_path in sensitive_paths:
        needle = _normalized_path_text(sensitive_path.resolve())
        assert all(needle not in value for value in receipt_strings)


def _metadata(name: str, version: str, license_value: str) -> SimpleNamespace:
    message = Message()
    message["Name"] = name
    message["Version"] = version
    message["License"] = license_value
    return SimpleNamespace(metadata=message)


def test_review_fails_closed_for_unreviewed_and_stale_dependencies(
    tmp_path: Path,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\ndependencies = ["alpha>=1", "beta>=1"]\n',
        encoding="utf-8",
    )
    review = tmp_path / "review.json"
    review.write_text(
        json.dumps(
            {
                "schema_name": "comsol_mcp.dependency_license_review",
                "schema_version": "1.0.0",
                "reviewed_on": "2026-01-01",
                "expires_on": "2027-01-01",
                "entries": [
                    {
                        "dependency": "alpha",
                        "accepted_signals": ["license:MIT"],
                        "reason": "Reviewed.",
                    },
                    {
                        "dependency": "stale",
                        "accepted_signals": ["license:MIT"],
                        "reason": "Reviewed.",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    receipt = build_license_receipt(
        pyproject,
        review,
        as_of=date(2026, 7, 18),
        distribution_provider=lambda name: _metadata(name, "1.0", "MIT"),
    )

    assert receipt["status"] == "failed"
    assert {item["reason_code"] for item in receipt["failures"]} >= {
        "unreviewed_dependency",
        "stale_review_entry",
    }


def test_expired_or_unmatched_license_review_fails_closed(tmp_path: Path) -> None:
    review_value = json.loads(REVIEW.read_text(encoding="utf-8"))
    review_value["reviewed_on"] = "2025-01-01"
    review_value["expires_on"] = "2026-01-01"
    review = tmp_path / "review.json"
    review.write_text(json.dumps(review_value), encoding="utf-8")

    receipt = build_license_receipt(
        PYPROJECT,
        review,
        as_of=date(2026, 7, 18),
        distribution_provider=lambda name: _metadata(name, "1.0", "UNKNOWN"),
    )

    reasons = {item["reason_code"] for item in receipt["failures"]}
    assert "review_expired" in reasons
    assert "license_metadata_unmatched" in reasons


def test_review_schema_and_dependency_declarations_are_bounded(tmp_path: Path) -> None:
    invalid_review = tmp_path / "review.json"
    invalid_review.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="fields"):
        load_license_review(invalid_review)

    duplicate = tmp_path / "pyproject.toml"
    duplicate.write_text(
        '[project]\ndependencies = ["same_pkg>=1", "same-pkg<2"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        declared_runtime_dependencies(duplicate)

    malformed = tmp_path / "malformed.toml"
    malformed.write_text(
        '[project]\ndependencies = ["valid>=1 trailing-garbage"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid"):
        declared_runtime_dependencies(malformed)

    marked = tmp_path / "marked.toml"
    marked.write_text(
        "[project]\ndependencies = [\n"
        "  \"same_pkg>=1; sys_platform == 'win32'\",\n"
        "  \"same-pkg<2; sys_platform != 'win32'\",\n"
        "]\n",
        encoding="utf-8",
    )
    assert declared_runtime_dependencies(marked) == ("same-pkg",)


@pytest.mark.parametrize(
    "signal",
    ["x" * 510 + ":MIT", "license:MIT\rhidden", "license:MIT\0hidden"],
)
def test_review_rejects_signals_that_installed_metadata_cannot_emit(
    tmp_path: Path, signal: str
) -> None:
    value = json.loads(REVIEW.read_text(encoding="utf-8"))
    value["entries"][0]["accepted_signals"] = [signal]
    review = tmp_path / "review.json"
    review.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="entry is invalid"):
        load_license_review(review)


def test_future_review_date_fails_closed() -> None:
    receipt = build_license_receipt(
        PYPROJECT,
        REVIEW,
        as_of=date(2026, 7, 17),
    )

    assert receipt["status"] == "failed"
    assert {item["reason_code"] for item in receipt["failures"]} >= {"review_date_in_future"}


def test_installed_license_expression_and_classifiers_are_bounded() -> None:
    expression = Message()
    expression["Name"] = "example"
    expression["Version"] = "1.0"
    expression["License-Expression"] = "x" * 600
    with pytest.raises(ValueError, match="unbounded"):
        distribution_license_record(SimpleNamespace(metadata=expression))

    classifiers = Message()
    classifiers["Name"] = "example"
    classifiers["Version"] = "1.0"
    for index in range(129):
        classifiers["Classifier"] = f"License :: Example :: {index}"
    with pytest.raises(ValueError, match="classifiers"):
        distribution_license_record(SimpleNamespace(metadata=classifiers))


def test_invalid_installed_metadata_is_a_structured_receipt_failure(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\ndependencies = ["alpha>=1"]\n', encoding="utf-8")
    review_value = json.loads(REVIEW.read_text(encoding="utf-8"))
    review_value["entries"] = [
        {"dependency": "alpha", "accepted_signals": ["license:MIT"], "reason": "Reviewed."}
    ]
    review = tmp_path / "review.json"
    review.write_text(json.dumps(review_value), encoding="utf-8")

    receipt = build_license_receipt(
        pyproject,
        review,
        as_of=date(2026, 8, 4),
        distribution_provider=lambda _name: _metadata("invalid name", "1.0", "MIT"),
    )

    assert {item["reason_code"] for item in receipt["failures"]} == {"installed_metadata_invalid"}


def test_every_observed_license_signal_requires_review_coverage(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\ndependencies = ["alpha>=1"]\n', encoding="utf-8")
    review_value = json.loads(REVIEW.read_text(encoding="utf-8"))
    review_value["entries"] = [
        {"dependency": "alpha", "accepted_signals": ["license:MIT"], "reason": "Reviewed."}
    ]
    review = tmp_path / "review.json"
    review.write_text(json.dumps(review_value), encoding="utf-8")
    metadata = Message()
    metadata["Name"] = "alpha"
    metadata["Version"] = "1.0"
    metadata["License"] = "MIT"
    metadata["License-Expression"] = "MIT"

    receipt = build_license_receipt(
        pyproject,
        review,
        as_of=date(2026, 8, 4),
        distribution_provider=lambda _name: SimpleNamespace(metadata=metadata),
    )

    assert {item["reason_code"] for item in receipt["failures"]} == {"license_metadata_unmatched"}


def test_license_receipt_hashes_the_same_single_input_snapshots(
    tmp_path: Path, monkeypatch
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\ndependencies = ["alpha>=1"]\n', encoding="utf-8")
    review = tmp_path / "review.json"
    review.write_text(
        json.dumps(
            {
                "schema_name": "comsol_mcp.dependency_license_review",
                "schema_version": "1.0.0",
                "reviewed_on": "2026-01-01",
                "expires_on": "2027-01-01",
                "entries": [
                    {
                        "dependency": "alpha",
                        "accepted_signals": ["license:MIT"],
                        "reason": "Reviewed.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    expected = {
        pyproject.resolve(): pyproject.read_bytes(),
        review.resolve(): review.read_bytes(),
    }
    calls = {path: 0 for path in expected}
    original = Path.read_bytes

    def tracked(path):
        resolved = path.resolve()
        if resolved in calls:
            calls[resolved] += 1
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", tracked)
    receipt = build_license_receipt(
        pyproject,
        review,
        as_of=date(2026, 7, 18),
        distribution_provider=lambda name: _metadata(name, "1.0", "MIT"),
    )

    import hashlib

    assert calls == {pyproject.resolve(): 1, review.resolve(): 1}
    assert receipt["pyproject_sha256"] == hashlib.sha256(expected[pyproject.resolve()]).hexdigest()
    assert receipt["review_sha256"] == hashlib.sha256(expected[review.resolve()]).hexdigest()
