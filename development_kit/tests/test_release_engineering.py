"""Dependency-only release engineering release-contract regression tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
import tomllib
import zipfile

import pytest

from development_kit.scripts.generate_release_lock import _render_lock
from development_kit.scripts.python_compatibility_licensed_gate import (
    _select_expected_backend,
    _status_is_clean,
)
from development_kit.scripts.release_gate import (
    _distribution_inventory,
    _lock_lane,
    _validated_dependency_lock,
)
from development_kit.scripts.run_real_release_gate import _wait_clean_ownership


ROOT = Path(__file__).parents[2]
RELEASE = ROOT / "development_kit" / "release"
FIXTURES = RELEASE / "integration_fixtures"
SNAPSHOTS = ROOT / "development_kit" / "tests" / "snapshots"


def _tracked_entries() -> list[tuple[str, str]]:
    completed = subprocess.run(
        ["git", "ls-files", "--stage"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [
        (metadata.split()[0], path)
        for metadata, path in (line.split("\t", 1) for line in completed.stdout.splitlines())
    ]


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_json_sha256(value) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_support_matrix_matches_frozen_profile_counts_and_declared_dependencies():
    matrix = _json(RELEASE / "support_matrix.json")
    names = _json(SNAPSHOTS / "profile_tool_names.json")
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert matrix["schema_name"] == "comsol_mcp.release_support_matrix"
    assert matrix["real_integration"] == {
        "hosted_ci_default": False,
        "licensed_host_required": True,
        "serial_only": True,
        "exact_version_evidence_required": True,
        "pid_and_lease_cleanup_required": True,
    }
    assert {item["name"]: item["tool_count"] for item in matrix["profiles"]} == {
        profile: len(tools) for profile, tools in names.items()
    }
    dependencies = "\n".join(pyproject["project"]["dependencies"])
    for package in ("matplotlib", "mcp", "mph", "numpy", "pydantic", "psutil", "scipy"):
        assert re.search(rf"(?m)^{package}(?:[<>=]|$)", dependencies)
    assert any(item.startswith("build>=") for item in pyproject["project"]["optional-dependencies"]["dev"])
    assert pyproject["project"]["requires-python"] == ">=3.14,<3.15"
    assert pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"] == [
        "/development_kit"
    ]


def test_repository_root_is_release_focused_and_free_of_generated_artifacts():
    entries = _tracked_entries()
    root_files = {path for _mode, path in entries if "/" not in path}
    assert root_files == {
        ".gitattributes",
        ".gitignore",
        "DEPLOYMENT.md",
        "DEPLOYMENT_CN.md",
        "LICENSE",
        "README.md",
        "README_CN.md",
        "pyproject.toml",
    }

    forbidden_suffixes = {".class", ".lock", ".mph", ".pyc", ".recovery", ".status"}
    for mode, path_text in entries:
        path = Path(path_text)
        assert mode != "160000", f"orphaned gitlink: {path_text}"
        assert path.name != ".DS_Store"
        assert "__pycache__" not in path.parts
        assert path.suffix not in forbidden_suffixes
        assert path.name not in {"server_err.txt", "server_log.txt"}


def test_active_implementation_has_only_enumerated_legacy_phase_codes():
    phase_pattern = re.compile(
        r"(?<![A-Za-z0-9_])(?:[EMH][0-9]+[A-Za-z]?|P(?:3|4|9|10|11|12))(?![A-Za-z0-9_])"
        r"|(?<![A-Za-z0-9])(?:h1|h2a|h2d|h2f|h3c|h3d|h3e|h3f|h4a|h4b|h4c|h4d|h4e|h4f|e4r)(?![A-Za-z0-9])"
    )
    allowed = {
        "development_kit/docs/legacy_phase_compatibility.md",
        "development_kit/release/integration_fixtures/reference_power_evidence.json",
        "development_kit/scripts/run_real_release_gate.py",
        "development_kit/tests/test_durable_job_control_plane.py",
        "development_kit/tests/test_reference_power_acceptance.py",
        "development_kit/tests/test_reference_power_release_orchestrator.py",
        "development_kit/tests/test_reference_power_runner.py",
        "development_kit/tests/test_release_engineering.py",
        "src/evidence/reference_power_acceptance.py",
    }
    violations = []
    for _mode, path_text in _tracked_entries():
        path = Path(path_text)
        if path.suffix not in {".json", ".md", ".py", ".toml", ".yaml", ".yml"}:
            continue
        text = (ROOT / path).read_text(encoding="utf-8", errors="replace")
        if phase_pattern.search(text) and path_text not in allowed:
            violations.append(path_text)
    assert violations == []


def test_public_tracked_text_has_no_user_profile_paths():
    text_suffixes = {".json", ".md", ".py", ".toml", ".yaml", ".yml"}
    for _mode, path_text in _tracked_entries():
        path = Path(path_text)
        if path.parts[0] == "development_kit" or path.suffix not in text_suffixes:
            continue
        text = (ROOT / path).read_text(encoding="utf-8", errors="replace")
        assert "C:/Users/" not in text, path_text
        assert "C:\\\\Users\\\\" not in text, path_text


def test_release_integration_fixture_manifest_is_complete_and_sanitized():
    manifest = _json(FIXTURES / "manifest.json")
    expected = {
        "capacitor_clientapi_regression",
        "periodic_mesh_audit",
        "reference_air_polarization",
        "reference_power_evidence",
        "passive_port_closure",
        "source_immutability",
        "job_recovery_cancellation",
        "lexical_manual_retrieval",
    }
    entries = manifest["fixtures"]
    assert {entry["fixture_id"] for entry in entries} == expected

    for entry in entries:
        contract_path = FIXTURES / entry["contract"]
        assert contract_path.parent == FIXTURES
        contract = _json(contract_path)
        assert contract["fixture_id"] == entry["fixture_id"]
        assert contract["schema_version"] == "1.0.0"
        assert contract["acceptance"]
        assert entry["canonical_json_sha256"] == _canonical_json_sha256(contract)
        assert entry["provenance"] == "repository_authored_contract"
        assert entry["redistribution_state"] == "redistributable_under_repository_license"
        assert entry["paper_derived"] is False
        for value in _strings(contract):
            assert "陆星" not in value
            assert "C:\\Users\\" not in value
            assert not PureWindowsPath(value).is_absolute()
            assert not PurePosixPath(value).is_absolute()


def test_distribution_inventory_rejects_development_kit_members(tmp_path):
    clean = tmp_path / "clean.whl"
    with zipfile.ZipFile(clean, "w") as archive:
        archive.writestr("src/server.py", "pass\n")
    inventory = _distribution_inventory(clean)
    assert inventory["development_kit_excluded"] is True
    assert inventory["member_count"] == 1

    contaminated = tmp_path / "contaminated.whl"
    with zipfile.ZipFile(contaminated, "w") as archive:
        archive.writestr("development_kit/tests/test_server.py", "pass\n")
    with pytest.raises(RuntimeError, match="contains development_kit"):
        _distribution_inventory(contaminated)


def test_hosted_ci_is_dependency_only_and_real_gate_is_explicit():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    real_gate = (
        ROOT / "development_kit" / "scripts" / "run_real_release_gate.py"
    ).read_text(encoding="utf-8")

    assert "python -m pytest -q" in workflow
    assert "python -m build" in workflow
    assert "release_gate.py --skip-tests" in workflow
    assert "actions/checkout@v7" in workflow
    assert "actions/setup-python@v6" in workflow
    assert "continue-on-error" not in workflow
    assert "Python 3.14, default production lane" in workflow
    assert "release_locked_py314.txt" in workflow
    assert "-m integration" not in workflow
    assert "RUN_REAL_COMSOL" in real_gate
    assert 'choices=["RUN_REAL_COMSOL"]' in real_gate


def test_release_dependency_lock_is_complete_and_matches_current_lane(tmp_path):
    lane = f"{__import__('sys').version_info.major}.{__import__('sys').version_info.minor}"
    lock = tmp_path / "lock.txt"
    lock.write_text(
        f"# Python-Lane: {lane}\nexample==1.0 \\\n"
        "    --hash=sha256:" + "a" * 64 + "\n",
        encoding="utf-8",
    )
    assert _lock_lane(lock) == lane
    assert _validated_dependency_lock(lock) == lock.resolve()

    rendered = _render_lock(
        lane=lane,
        python_version=f"{lane}.0",
        pins=["example==1.0"],
        hashes={("example", "1.0"): ["b" * 64]},
    )
    assert f"# Python-Lane: {lane}" in rendered
    assert "example==1.0" in rendered
    assert f"--hash=sha256:{'b' * 64}" in rendered

    production_lock = ROOT / "constraints" / "release_locked_py314.txt"
    lock_text = production_lock.read_text(encoding="utf-8")
    assert _lock_lane(production_lock) == "3.14"
    requirement_lines = [
        line for line in lock_text.splitlines() if line and not line.startswith(("#", " "))
    ]
    assert len(requirement_lines) >= 40
    assert all(re.fullmatch(r"[a-z0-9-]+==[^ ]+ \\", line) for line in requirement_lines)
    assert lock_text.count("--hash=sha256:") >= len(requirement_lines)


def test_python_compatibility_gate_requires_exact_backend_and_clean_control_plane():
    backend = _select_expected_backend(
        [
            {
                "name": "6.4",
                "major": 6,
                "minor": 4,
                "patch": 0,
                "build": 293,
                "root": "D:/COMSOL64/Multiphysics",
                "jvm": "D:/COMSOL64/Multiphysics/java/jvm.dll",
            }
        ]
    )
    assert backend["build"] == 293
    with pytest.raises(RuntimeError, match="exactly one"):
        _select_expected_backend([])

    clean = {
        "collision": False,
        "process_inventory": {"complete": True, "fresh": True},
        "lease": {"state": "absent"},
        "durable_jobs": {"available": True, "active_count": 0},
    }
    assert _status_is_clean(clean) is True
    clean["durable_jobs"]["active_count"] = 1
    assert _status_is_clean(clean) is False


def test_installed_probe_checks_every_profile_without_solver_or_heavy_imports():
    probe = (
        ROOT / "development_kit" / "scripts" / "installed_package_probe.py"
    ).read_text(encoding="utf-8")

    assert "for profile in PROFILE_NAMES" in probe
    assert "snapshot_tool_schemas" in probe
    assert "deployment_identity" in probe
    assert "installed_site_package" in probe
    assert "installed-package discovery must not start COMSOL" in probe
    assert {"chromadb", "sentence_transformers", "torch"} <= set(
        re.findall(r'"([a-z_]+)"', probe)
    )


def test_release_documentation_requires_restart_and_clean_tree():
    checklist = (
        ROOT / "development_kit" / "docs" / "release_checklist.md"
    ).read_text(encoding="utf-8")
    migration = (ROOT / "docs" / "profile_migration.md").read_text(encoding="utf-8")

    assert "clean tree" in checklist
    assert "non-editably" in checklist
    assert "Restart the MCP host" in checklist
    assert "Profiles are immutable" in migration
    assert "promotion rejected" in migration


def test_real_release_gate_waits_for_fresh_complete_cleanup_without_stale_authority():
    incomplete = {
        "process_inventory": {"complete": False, "fresh": False},
        "collision": True,
        "lease": {"state": "absent"},
    }
    clean = {
        "process_inventory": {"complete": True, "fresh": True},
        "collision": False,
        "lease": {"state": "absent"},
    }

    class Owner:
        def __init__(self):
            self.values = [incomplete, clean]

        def status(self):
            return self.values.pop(0)

    ticks = iter([0.0, 0.1])
    result = _wait_clean_ownership(
        Owner(),
        timeout_seconds=1.0,
        poll_seconds=0.0,
        clock=lambda: next(ticks),
        sleeper=lambda _seconds: None,
    )

    assert result is clean


def test_real_release_gate_timeout_preserves_fail_closed_collision():
    blocked = {
        "process_inventory": {"complete": False, "fresh": False},
        "collision": True,
        "lease": {"state": "absent"},
    }

    class Owner:
        def status(self):
            return blocked

    result = _wait_clean_ownership(
        Owner(),
        timeout_seconds=0.0,
        clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
    )

    assert result is blocked
    assert result["collision"] is True
