"""Deterministic dependency-drift report classification tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from development_kit.scripts.dependency_drift_report import (
    build_dependency_drift_report,
    canonical_distribution_name,
    serialize_dependency_drift_report,
)

ROOT = Path(__file__).resolve().parents[2]


LOCK_DRIFTS = {
    "annotated-types": ("0.7.0", "0.8.0"),
    "certifi": ("2026.6.17", "2026.7.22"),
    "cryptography": ("49.0.0", "50.0.0"),
    "matplotlib": ("3.11.0", "3.11.1"),
    "mcp": ("1.28.1", "1.29.0"),
    "sse-starlette": ("3.4.5", "3.4.6"),
    "uvicorn": ("0.51.0", "0.52.1"),
}


def _write_fixture(root: Path) -> tuple[Path, Path, Path]:
    pyproject = root / "pyproject.toml"
    pyproject.write_text(
        """
[project]
dependencies = [
  "mcp>=1.28.1,<2",
  "MPh>=1.3.1,<1.4",
  "matplotlib>=3.9,<4",
  "pydantic>=2,<3",
]

[project.optional-dependencies]
manuals = ["PyMuPDF>=1.24,<2"]
dev = ["mypy>=2.3,<3"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    lock = root / "release_locked_py314.txt"
    locked = {
        **{name: before for name, (before, _after) in LOCK_DRIFTS.items()},
        "pydantic": "2.13.4",
        "pydantic-core": "2.46.4",
    }
    lock.write_text(
        "\n".join(f"{name}=={version} \\" for name, version in sorted(locked.items())) + "\n",
        encoding="utf-8",
    )
    lock_hash = hashlib.sha256(lock.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    tested = root / "tested_versions.json"
    tested.write_text(
        json.dumps(
            {
                "production_python_3_14": {
                    "direct_dependencies": {
                        "mcp": "1.28.1",
                        "MPh": "1.3.1",
                        "matplotlib": "3.11.0",
                        "pydantic": "2.13.4",
                    },
                    "optional_dependencies": {"manuals": {"PyMuPDF": "1.28.0"}},
                },
                "release_lock": {
                    "path": lock.name,
                    "sha256": lock_hash,
                    "requirement_count": len(locked),
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return pyproject, tested, lock


def _installed_environment() -> list[dict[str, str]]:
    versions = {
        **{name: after for name, (_before, after) in LOCK_DRIFTS.items()},
        "MPh": "1.3.1",
        "mypy": "2.3.0",
        "pip": "26.1.2",
        "pydantic": "2.13.4",
        "pydantic_core": "2.46.4",
        "PyMuPDF": "1.28.0",
    }
    return [{"name": name, "version": version} for name, version in versions.items()]


def _outdated_environment() -> list[dict[str, str]]:
    return [
        {"name": "mcp", "version": "1.29.0", "latest_version": "2.0.0"},
        {"name": "pydantic_core", "version": "2.46.4", "latest_version": "2.47.0"},
        {"name": "pip", "version": "26.1.2", "latest_version": "26.2"},
    ]


def test_pep503_distribution_name_normalization() -> None:
    assert canonical_distribution_name("MPh") == "mph"
    assert canonical_distribution_name("pydantic_core") == "pydantic-core"
    assert canonical_distribution_name("A..B__C---D") == "a-b-c-d"


def test_report_classifies_trigger_edge_cases_and_all_lock_drifts(tmp_path: Path) -> None:
    pyproject, tested, lock = _write_fixture(tmp_path)

    report = build_dependency_drift_report(
        pyproject_path=pyproject,
        tested_versions_path=tested,
        release_lock_path=lock,
        installed_environment=_installed_environment(),
        outdated_dependencies=_outdated_environment(),
        installed_requirements={"pydantic": ["pydantic-core==2.46.4"]},
        source_commit="0f2ebbcb4eb953b8e1c86c8b149195ec39cb523f",
        generated_at_utc="2026-08-03T00:00:00Z",
        python_identity={
            "version": "3.14.6",
            "abi": "cp314-win_amd64",
            "platform": "win_amd64",
        },
    )

    packages = {item["name"]: item for item in report["packages"]}
    assert packages["mcp"] == {
        "name": "mcp",
        "scope": "runtime_direct",
        "declared_specifier": ">=1.28.1,<2",
        "reviewed_version": "1.28.1",
        "locked_version": "1.28.1",
        "installed_version": "1.29.0",
        "latest_available": "2.0.0",
        "latest_allowed_by_project": "1.29.0",
        "latest_allowed_basis": "current_compatible_resolution",
        "decision": "outside_project_range",
    }
    assert packages["pydantic-core"]["scope"] == "runtime_transitive"
    assert packages["pydantic-core"]["decision"] == "paired_only"
    assert packages["pydantic-core"]["required_by"] == [
        {"name": "pydantic", "specifier": "==2.46.4"}
    ]
    assert packages["pymupdf"]["scope"] == "optional_manuals"
    assert packages["pymupdf"]["installed_version"] == "1.28.0"
    assert packages["pymupdf"]["decision"] == "informational"
    assert packages["pip"]["scope"] == "bootstrap"
    assert packages["pip"]["decision"] == "informational"
    assert packages["mph"]["scope"] == "runtime_direct"

    lock_drift = {
        item["name"]: (item["locked_version"], item["installed_version"])
        for item in report["release_lock_comparison"]["drifted"]
    }
    assert lock_drift == LOCK_DRIFTS
    assert report["release_lock_comparison"]["drift_count"] == 7
    assert report["pip_check"] == "passed"


def test_report_serialization_is_deterministic_and_utf8(tmp_path: Path) -> None:
    pyproject, tested, lock = _write_fixture(tmp_path)
    arguments = {
        "pyproject_path": pyproject,
        "tested_versions_path": tested,
        "release_lock_path": lock,
        "installed_environment": _installed_environment(),
        "outdated_dependencies": _outdated_environment(),
        "installed_requirements": {"pydantic": ["pydantic-core==2.46.4"]},
        "source_commit": "a" * 40,
        "generated_at_utc": "2026-08-03T00:00:00Z",
        "python_identity": {
            "version": "3.14.6",
            "abi": "cp314-win_amd64",
            "platform": "win_amd64",
        },
    }

    first = serialize_dependency_drift_report(build_dependency_drift_report(**arguments))
    second = serialize_dependency_drift_report(build_dependency_drift_report(**arguments))

    assert first == second
    assert first.endswith(b"\n")
    assert json.loads(first) == json.loads(second)


def test_workflow_installs_manuals_and_routes_the_tested_generator() -> None:
    workflow = (ROOT / ".github" / "workflows" / "dependency_report.yml").read_text(
        encoding="utf-8"
    )

    assert 'python -m pip install ".[dev,manuals]"' in workflow
    assert "development_kit/scripts/dependency_drift_report.py" in workflow
    assert "--installed installed-environment.json" in workflow
    assert "--outdated outdated-dependencies.json" in workflow
    assert "ConvertFrom-Json" not in workflow
