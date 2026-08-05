"""Dependency-only release engineering release-contract regression tests."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import struct
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace

import pytest
import yaml

from development_kit.scripts import generate_release_lock as lock_generator
from development_kit.scripts import installed_package_probe
from development_kit.scripts import python_compatibility_licensed_gate as compatibility_gate
from development_kit.scripts import release_gate as release_gate_module
from development_kit.scripts.generate_release_lock import _render_lock
from development_kit.scripts.planning_code_gate import (
    TEXT_SUFFIXES,
    load_planning_code_allowlist,
    verify_planning_code_texts,
)
from development_kit.scripts.python_compatibility_licensed_gate import (
    _runtime_process_evidence,
    _select_expected_backend,
    _status_is_clean,
    _terminate_owned_tree,
    _write_receipt,
)
from development_kit.scripts.release_gate import (
    PLANNING_CODE_ALLOWLIST,
    _dependency_lock_location,
    _distribution_artifacts,
    _distribution_inventory,
    _lock_lane,
    _run,
    _sanitized_probe_environment,
    _validated_dependency_lock,
)
from development_kit.scripts.run_real_release_gate import _wait_clean_ownership

ROOT = Path(__file__).parents[2]
RELEASE = ROOT / "development_kit" / "release"
FIXTURES = RELEASE / "integration_fixtures"
SNAPSHOTS = ROOT / "development_kit" / "tests" / "snapshots"


@pytest.mark.parametrize(
    "relative_path",
    ["comsol_mcp/tools/ownership.py", "comsol_mcp/jobs/store.py"],
)
def test_windows_lock_backend_is_not_imported_at_module_scope(relative_path):
    tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
    imported = {
        alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names
    }

    assert "msvcrt" not in imported


def test_python_compatibility_receipt_publication_preserves_existing_output(tmp_path):
    output = tmp_path / "receipt.json"
    output.write_text('{"owner":"competitor"}\n', encoding="utf-8")

    with pytest.raises(FileExistsError):
        _write_receipt(output, {"owner": "gate"})

    assert json.loads(output.read_text(encoding="utf-8")) == {"owner": "competitor"}


def test_python_compatibility_parent_cleans_worker_path_after_output_collision(
    tmp_path, monkeypatch
):
    output = tmp_path / "receipt.json"
    clean = {
        "process_inventory": {"complete": True, "fresh": True},
        "collision": False,
        "lease": {"state": "absent"},
        "durable_jobs": {"available": True, "active_count": 0, "active": []},
    }
    waits = 0

    def wait_clean(_owner, timeout_seconds=30.0):
        nonlocal waits
        waits += 1
        if waits == 1:
            output.write_text('{"owner":"competitor"}\n', encoding="utf-8")
        return clean

    class Owner:
        def __init__(self, *_args, **_kwargs):
            pass

        def preflight(self, **_kwargs):
            return {"ready": False, "blockers": ["injected"]}

    monkeypatch.setattr(compatibility_gate, "_git_identity", lambda: {"dirty_entry_count": 0})
    monkeypatch.setattr(compatibility_gate, "_wait_clean", wait_clean)
    monkeypatch.setattr(compatibility_gate, "_descendant_identities", lambda _pid: [])
    monkeypatch.setattr(compatibility_gate, "SolverOwnership", Owner)

    with pytest.raises(FileExistsError):
        compatibility_gate._run_parent(
            SimpleNamespace(
                output=output,
                runtime_root=tmp_path / "runtime",
                minimum_free_gb=0.0,
                cores=1,
                timeout_seconds=1.0,
            )
        )

    assert json.loads(output.read_text(encoding="utf-8")) == {"owner": "competitor"}
    assert list(tmp_path.glob(".receipt.worker.*.json")) == []


def test_compatibility_cleanup_terminates_captured_descendants_after_worker_exit(
    monkeypatch,
):
    actions = []
    identities = [
        {
            "pid": 41002,
            "process_create_time": 20.0,
            "command_signature": "b" * 64,
        }
    ]

    class ExitedProcess:
        pid = 41001

        @staticmethod
        def poll():
            return 0

    monkeypatch.setattr(
        compatibility_gate,
        "terminate_exact",
        lambda identity, force: actions.append((identity, force)) or {"acted": True},
    )
    monkeypatch.setattr(
        compatibility_gate,
        "verify_absent",
        lambda captured: {"absent": captured == identities, "verdicts": []},
    )

    result = _terminate_owned_tree(ExitedProcess(), identities)

    assert actions == [(identities[0], True)]
    assert result["direct_was_running"] is False
    assert result["passed"] is True


def test_compatibility_cleanup_preserves_primary_error_and_continues_after_release_error(
    tmp_path, monkeypatch
):
    output = tmp_path / "receipt.json"
    clean = {
        "process_inventory": {"complete": True, "fresh": True},
        "collision": False,
        "lease": {"state": "absent"},
        "durable_jobs": {"available": True, "active_count": 0, "active": []},
    }
    waits = []

    class Owner:
        def __init__(self, *_args, **_kwargs):
            pass

        def preflight(self, **_kwargs):
            return {"ready": True}

        def acquire(self, **_kwargs):
            return {"success": True}

        def release(self):
            raise OSError("injected release failure")

    monkeypatch.setattr(compatibility_gate, "_git_identity", lambda: {"dirty_entry_count": 0})
    monkeypatch.setattr(
        compatibility_gate,
        "_wait_clean",
        lambda _owner, timeout_seconds=30.0: waits.append(timeout_seconds) or clean,
    )
    monkeypatch.setattr(compatibility_gate, "SolverOwnership", Owner)
    streams = []

    def fail_launch(*_args, **kwargs):
        streams.extend([kwargs["stdout"], kwargs["stderr"]])
        assert kwargs["stdout"] is not subprocess.PIPE
        assert kwargs["stderr"] is not subprocess.PIPE
        assert not kwargs.get("text", False)
        raise OSError("injected launch failure")

    monkeypatch.setattr(compatibility_gate.subprocess, "Popen", fail_launch)
    monkeypatch.setattr(compatibility_gate, "_descendant_identities", lambda _pid: [])
    monkeypatch.setattr(
        compatibility_gate,
        "_listener_inventory",
        lambda _pids: {"complete": True, "error": None, "listeners": []},
    )

    returncode = compatibility_gate._run_parent(
        SimpleNamespace(
            output=output,
            runtime_root=tmp_path / "runtime",
            minimum_free_gb=0.0,
            cores=1,
            timeout_seconds=1.0,
        )
    )
    receipt = json.loads(output.read_text(encoding="utf-8"))

    assert returncode == 1
    assert receipt["schema_version"] == "1.1.0"
    assert receipt["error"].startswith("OSError: injected launch failure")
    assert {item["stage"] for item in receipt["cleanup"]["errors"]} == {"lease_release"}
    assert receipt["ownership_after"] is not None
    assert len(waits) == 2
    assert all(stream.closed for stream in streams)
    assert not list(tmp_path.glob(".receipt.worker.*.stdout.log"))
    assert not list(tmp_path.glob(".receipt.worker.*.stderr.log"))


def test_compatibility_final_descendant_snapshot_is_recorded_once(tmp_path, monkeypatch):
    output = tmp_path / "receipt.json"
    clean = {
        "process_inventory": {"complete": True, "fresh": True},
        "collision": False,
        "lease": {"state": "absent"},
        "durable_jobs": {"available": True, "active_count": 0, "active": []},
    }
    descendant = {"pid": 42001, "process_create_time": 30.0}
    snapshots = []

    class Owner:
        def __init__(self, *_args, **_kwargs):
            pass

        def preflight(self, **_kwargs):
            return {"ready": False, "blockers": ["injected"]}

    monkeypatch.setattr(compatibility_gate, "_git_identity", lambda: {"dirty_entry_count": 0})
    monkeypatch.setattr(compatibility_gate, "_wait_clean", lambda *_args, **_kwargs: clean)
    monkeypatch.setattr(compatibility_gate, "SolverOwnership", Owner)
    monkeypatch.setattr(
        compatibility_gate,
        "_descendant_identities",
        lambda _pid: snapshots.append(True) or [descendant],
    )
    monkeypatch.setattr(
        compatibility_gate,
        "_listener_inventory",
        lambda _pids: {"complete": True, "error": None, "listeners": []},
    )

    assert (
        compatibility_gate._run_parent(
            SimpleNamespace(
                output=output,
                runtime_root=tmp_path / "runtime",
                minimum_free_gb=0.0,
                cores=1,
                timeout_seconds=1.0,
            )
        )
        == 1
    )
    receipt = json.loads(output.read_text(encoding="utf-8"))

    assert snapshots == [True]
    assert receipt["cleanup"]["owned_descendants"] == [descendant]
    assert receipt["cleanup"]["owned_descendants_absent"] is False


def test_compatibility_listener_evidence_is_explicitly_sampled_not_exhaustive():
    evidence = _runtime_process_evidence({}, {}, {})

    assert evidence["owned_listeners"] == []
    assert evidence["listener_inventory_samples_complete"] is True
    assert evidence["listener_sampling_exhaustive"] is False
    assert evidence["listener_evidence_scope"] == "observed_samples_only"


def test_production_runtime_guards_survive_python_optimization():
    assert_statements = []
    for package in ("comsol_mcp", "src"):
        for path in sorted((ROOT / package).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            assert_statements.extend(
                f"{path.relative_to(ROOT)}:{node.lineno}"
                for node in ast.walk(tree)
                if isinstance(node, ast.Assert)
            )
    assert assert_statements == []

    code = """
from src.evidence.spectral_characterization import _fit_candidate

try:
    _fit_candidate(
        method='local_polynomial_fit',
        wavelengths=[1.0, 2.0, 3.0],
        oriented=[0.0, 1.0, 0.0],
        candidate_index=1,
        support_count=3,
        baseline=0.0,
        polynomial_degree=None,
        max_evaluations=10,
    )
except ValueError as exc:
    if str(exc) != 'polynomial_degree is required for local_polynomial_fit':
        raise
else:
    raise SystemExit('optimized Python skipped the production runtime guard')
"""
    completed = subprocess.run(
        [sys.executable, "-O", "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr


def test_wheel_distributes_only_reviewed_runtime_packages():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    wheel = project["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["comsol_mcp", "settings_gui"]
    assert wheel["exclude"] == [
        "/settings_gui/tests",
        "/settings_gui/locales/**/*.po",
        "/settings_gui/locales/*.pot",
    ]
    assert project["project"]["scripts"]["comsol-mcp-settings"] == "settings_gui.__main__:main"


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


def test_release_gate_subprocesses_use_hidden_windows_launch(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))

    monkeypatch.setattr(subprocess, "run", fake_run)
    _run(["python", "-c", "pass"])

    assert calls[0][0] == ["python", "-c", "pass"]
    expected = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    assert calls[0][1]["creationflags"] == expected
    assert calls[0][1]["check"] is True


def test_support_matrix_matches_frozen_profile_counts_and_declared_dependencies():
    matrix = _json(RELEASE / "support_matrix.json")
    names = _json(SNAPSHOTS / "profile_tool_names.json")
    features = _json(SNAPSHOTS / "feature_tool_names.json")
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert matrix["schema_name"] == "comsol_mcp.release_support_matrix"
    assert matrix["release_identity_sources"] == {
        "package_version": "comsol_mcp/__init__.py",
        "runtime_compatibility": "comsol_mcp/compatibility_manifest.json",
        "dependency_ranges": "pyproject.toml",
    }
    assert matrix["real_integration"] == {
        "hosted_ci_default": False,
        "licensed_host_required": True,
        "serial_only": True,
        "exact_version_evidence_required": True,
        "pid_and_lease_cleanup_required": True,
    }
    profile_support = {item["name"]: item["support"] for item in matrix["profiles"]}
    assert profile_support["wave_optics"] == "experimental"
    assert all("v2_in_progress" not in support for support in profile_support.values())
    assert {item["name"]: item["tool_count"] for item in matrix["profiles"]} == {
        profile: len(tools) for profile, tools in names.items()
    }
    assert {item["name"]: item["tool_count"] for item in matrix["features"]} == {
        feature: len(tools) for feature, tools in features.items()
    }
    assert all(item["default_enabled"] is False for item in matrix["features"])
    dependencies = "\n".join(pyproject["project"]["dependencies"])
    for package in ("matplotlib", "mcp", "mph", "numpy", "pydantic", "psutil", "scipy"):
        assert re.search(rf"(?m)^{package}(?:[<>=]|$)", dependencies)
    assert any(
        item.startswith("build>=") for item in pyproject["project"]["optional-dependencies"]["dev"]
    )
    assert pyproject["build-system"]["requires"] == ["hatchling==1.31.0"]
    assert pyproject["project"]["optional-dependencies"]["manuals"] == ["pymupdf>=1.24.0,<2"]
    assert pyproject["project"]["requires-python"] == ">=3.14,<3.15"
    assert pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"] == [
        "/development_kit",
        "/.claude",
    ]


def test_recommended_profile_migration_records_the_exact_discovery_diff():
    migration = _json(RELEASE / "profile_migration.json")
    assert migration["profile"] == "wave_optics"
    assert migration["before"] == {
        "tool_count": 68,
        "tools_removed_from_recommended_surface": ["study_staged_parametric_sweep"],
    }
    assert migration["after"] == {
        "tool_count": 67,
        "tools_added_to_recommended_surface": [],
    }
    assert migration["replacement"] == ("job_submit/job_status/job_tail/job_cancel/job_resume")
    assert migration["restart_required"] is True


def test_repository_root_is_release_focused_and_free_of_generated_artifacts():
    entries = _tracked_entries()
    root_files = {path for _mode, path in entries if "/" not in path}
    assert root_files == {
        ".gitattributes",
        ".gitignore",
        "AGENTS.md",
        "CLAUDE.md",
        "DEPLOYMENT.md",
        "DEPLOYMENT_CN.md",
        "LICENSE",
        "Open_Settings_GUI.ps1",
        "README.md",
        "README_CN.md",
        "pyproject.toml",
        "settings.json",
    }

    forbidden_suffixes = {".class", ".lock", ".mph", ".pyc", ".recovery", ".status"}
    for mode, path_text in entries:
        path = Path(path_text)
        assert mode != "160000", f"orphaned gitlink: {path_text}"
        assert path.name != ".DS_Store"
        assert "__pycache__" not in path.parts
        assert path.suffix not in forbidden_suffixes
        assert path.name not in {"server_err.txt", "server_log.txt"}


def test_repository_layout_documents_every_tracked_file_once():
    layout_path = ROOT / "development_kit" / "docs" / "layout.md"
    layout = layout_path.read_text(encoding="utf-8")
    entries = re.findall(r"(?m)^- `([^`]+)` — (.+)$", layout)
    documented = [path for path, _description in entries]
    tracked = {path for _mode, path in _tracked_entries()}
    tracked.add("development_kit/docs/layout.md")

    assert len(documented) == len(set(documented))
    assert set(documented) == tracked
    for path, description in entries:
        assert description.isascii(), path
        assert description.endswith("."), path
        assert "\n" not in description, path


def test_active_implementation_has_only_enumerated_legacy_phase_codes():
    texts = {}
    for _mode, path_text in _tracked_entries():
        path = Path(path_text)
        if path.suffix not in TEXT_SUFFIXES:
            continue
        texts[path_text] = (ROOT / path).read_text(encoding="utf-8", errors="replace")
    receipt = verify_planning_code_texts(
        texts,
        allowlist=load_planning_code_allowlist(PLANNING_CODE_ALLOWLIST),
        require_all_allowlisted=True,
    )
    assert receipt["verified"] is True
    crlf_receipt = verify_planning_code_texts(
        {path: text.replace("\n", "\r\n") for path, text in texts.items()},
        allowlist=load_planning_code_allowlist(PLANNING_CODE_ALLOWLIST),
        require_all_allowlisted=True,
    )
    assert crlf_receipt == receipt


def test_planning_code_gate_detects_codes_inside_underscore_identifiers():
    text = "prefix_" + "H" + "1" + "_suffix = 1"
    with pytest.raises(RuntimeError, match=r"unexpected=\['sample.py'\]"):
        verify_planning_code_texts(
            {"sample.py": text},
            allowlist={},
            require_all_allowlisted=True,
        )


def test_planning_code_allowlist_rejects_unknown_top_level_fields(tmp_path):
    path = tmp_path / "allowlist.json"
    path.write_text(
        json.dumps(
            {
                "schema_name": "comsol_mcp.planning_code_allowlist",
                "schema_version": "1.0.0",
                "entries": [],
                "entires": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="schema is invalid"):
        load_planning_code_allowlist(path)


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
    fixture_ids = [entry["fixture_id"] for entry in entries]
    assert len(fixture_ids) == len(set(fixture_ids))
    assert set(fixture_ids) == expected

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
        archive.writestr("comsol_mcp/server.py", "pass\n")
    inventory = _distribution_inventory(clean)
    assert inventory["development_kit_excluded"] is True
    assert inventory["forbidden_entries_absent"] is True
    assert inventory["planning_code_gate"]["verified"] is True
    assert inventory["member_count"] == 1

    contaminated = tmp_path / "contaminated.whl"
    with zipfile.ZipFile(contaminated, "w") as archive:
        archive.writestr("development_kit/tests/test_server.py", "pass\n")
    with pytest.raises(RuntimeError, match="forbidden members"):
        _distribution_inventory(contaminated)

    agent_config = tmp_path / "agent-config.whl"
    with zipfile.ZipFile(agent_config, "w") as archive:
        archive.writestr(".claude/settings.local.json", "{}\n")
    with pytest.raises(RuntimeError, match="forbidden members"):
        _distribution_inventory(agent_config)


def test_distribution_artifacts_require_one_wheel_and_one_sdist(tmp_path):
    wheel = tmp_path / "package-1-py3-none-any.whl"
    sdist = tmp_path / "package-1.tar.gz"
    wheel.touch()
    sdist.touch()
    assert _distribution_artifacts(tmp_path) == [wheel, sdist]

    (tmp_path / "package-2-py3-none-any.whl").touch()
    with pytest.raises(RuntimeError, match="exactly one wheel and one sdist"):
        _distribution_artifacts(tmp_path)


def test_distribution_inventory_rejects_archive_links(tmp_path):
    symlink_wheel = tmp_path / "linked.whl"
    with zipfile.ZipFile(symlink_wheel, "w") as archive:
        info = zipfile.ZipInfo("comsol_mcp/alias.py")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        archive.writestr(info, "target.py")
    with pytest.raises(RuntimeError, match="link member"):
        _distribution_inventory(symlink_wheel)

    hardlink_sdist = tmp_path / "package-1.tar.gz"
    with tarfile.open(hardlink_sdist, "w:gz") as archive:
        payload = b"pass\n"
        source = tarfile.TarInfo("package-1/comsol_mcp/source.py")
        source.size = len(payload)
        archive.addfile(source, io.BytesIO(payload))
        link = tarfile.TarInfo("package-1/comsol_mcp/alias.py")
        link.type = tarfile.LNKTYPE
        link.linkname = source.name
        archive.addfile(link)
    with pytest.raises(RuntimeError, match="link member"):
        _distribution_inventory(hardlink_sdist)


def test_distribution_inventory_rejects_normalized_name_collisions(tmp_path):
    wheel = tmp_path / "duplicate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("comsol_mcp/first.py", "first\n")
        archive.writestr("comsol_mcp/./first.py", "second\n")
    with pytest.raises(RuntimeError, match="duplicate normalized paths"):
        _distribution_inventory(wheel)

    sdist = tmp_path / "package-1.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for name, payload in (
            ("package-1/comsol_mcp/first.py", b"first\n"),
            ("package-1/comsol_mcp/./first.py", b"second\n"),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    with pytest.raises(RuntimeError, match="duplicate normalized paths"):
        _distribution_inventory(sdist)


def test_sdist_inventory_requires_its_exact_filename_root(tmp_path):
    valid = tmp_path / "package-1.tar.gz"
    with tarfile.open(valid, "w:gz") as archive:
        payload = b"pass\n"
        member = tarfile.TarInfo("package-1/comsol_mcp/server.py")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    assert _distribution_inventory(valid)["member_count"] == 1

    invalid = tmp_path / "package-2.tar.gz"
    with tarfile.open(invalid, "w:gz") as archive:
        payload = b"pass\n"
        member = tarfile.TarInfo("comsol_mcp-unrelated/comsol_mcp/server.py")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    with pytest.raises(RuntimeError, match="outside the exact archive root"):
        _distribution_inventory(invalid)


def test_distribution_inventory_enforces_frozen_planning_codes_and_private_paths(tmp_path):
    legacy = tmp_path / "legacy.whl"
    with zipfile.ZipFile(legacy, "w") as archive:
        archive.writestr(
            "comsol_mcp/evidence/reference_power_acceptance.py",
            (ROOT / "comsol_mcp" / "evidence" / "reference_power_acceptance.py").read_bytes(),
        )
    assert _distribution_inventory(legacy)["planning_code_gate"]["matched_occurrence_count"] == 31

    unexpected = tmp_path / "unexpected.whl"
    with zipfile.ZipFile(unexpected, "w") as archive:
        archive.writestr("comsol_mcp/new_module.py", "marker = '" + "E" + "2'\n")
    with pytest.raises(RuntimeError, match="planning-code compatibility surface changed"):
        _distribution_inventory(unexpected)

    private = tmp_path / "private.whl"
    with zipfile.ZipFile(private, "w") as archive:
        archive.writestr("comsol_mcp/config.json", '{"path":"C:/Users/example/private"}\n')
    with pytest.raises(RuntimeError, match="private user path"):
        _distribution_inventory(private)

    model = tmp_path / "model.whl"
    with zipfile.ZipFile(model, "w") as archive:
        archive.writestr("comsol_mcp/private_model.mph", b"binary")
    with pytest.raises(RuntimeError, match="forbidden members"):
        _distribution_inventory(model)


def test_hosted_ci_is_dependency_only_and_real_gate_is_explicit():
    workflow_path = ROOT / ".github" / "workflows" / "ci.yml"
    workflow = workflow_path.read_text(encoding="utf-8")
    workflow_data = yaml.safe_load(workflow)
    dependency_report = (ROOT / ".github" / "workflows" / "dependency_report.yml").read_text(
        encoding="utf-8"
    )
    dependency_report_data = yaml.safe_load(dependency_report)
    dependency_report_generator = (
        ROOT / "development_kit" / "scripts" / "dependency_drift_report.py"
    ).read_text(encoding="utf-8")
    report_job = dependency_report_data["jobs"]["report"]
    report_commands = "\n".join(
        str(step.get("run", "")) for step in report_job["steps"] if isinstance(step, dict)
    )
    real_gate = (ROOT / "development_kit" / "scripts" / "run_real_release_gate.py").read_text(
        encoding="utf-8"
    )
    quality_gate = (ROOT / "development_kit" / "scripts" / "quality_gate.py").read_text(
        encoding="utf-8"
    )

    jobs = workflow_data["jobs"]
    assert workflow_data["name"] == "solver-free-ci"
    dependency_job = jobs["dependency-compatibility"]
    unit_job = jobs["unit-and-package-py314"]
    gui_job = jobs["solver-free-settings-gui"]
    dependency_steps = dependency_job["steps"]
    dependency_commands = "\n".join(
        str(step.get("run", "")) for step in dependency_steps if isinstance(step, dict)
    )
    unit_commands = "\n".join(
        str(step.get("run", "")) for step in unit_job["steps"] if isinstance(step, dict)
    )
    security_job = jobs["locked-runtime-security"]
    security_commands = "\n".join(
        str(step.get("run", "")) for step in security_job["steps"] if isinstance(step, dict)
    )
    gui_commands = "\n".join(
        str(step.get("run", "")) for step in gui_job["steps"] if isinstance(step, dict)
    )
    all_commands = "\n".join(
        str(step.get("run", ""))
        for job in jobs.values()
        for step in job["steps"]
        if isinstance(step, dict)
    )

    assert "python -u -m pytest -vv" in dependency_commands
    assert " -n " not in dependency_commands
    assert 'os.environ.get("GITHUB_ACTIONS", "").casefold() == "true"' in quality_gate
    assert "python -m build" in unit_commands
    assert "release_gate.py --skip-tests" in unit_commands
    action_references = [
        step["uses"].split("@", 1)
        for document in (workflow_data, dependency_report_data)
        for job in document["jobs"].values()
        for step in job["steps"]
        if "uses" in step
    ]
    assert len(action_references) == 14
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for _action, revision in action_references)
    assert "# actions/checkout v7.0.0" in workflow
    assert "# actions/setup-python v6.2.0" in workflow
    assert "# actions/checkout v7.0.0" in dependency_report
    assert "# actions/setup-python v6.2.0" in dependency_report
    assert "# actions/upload-artifact v7.0.1" in dependency_report
    assert workflow_data["concurrency"]["cancel-in-progress"] is True
    assert dependency_report_data["concurrency"]["cancel-in-progress"] is True
    assert "pip list --format=json" in report_commands
    assert "List installed direct dependency versions" not in dependency_report
    assert "constraints/tested_versions.json" in report_commands
    assert "constraints/release_locked_py314.txt" in report_commands
    assert "reviewed release-lock hash differs" in dependency_report_generator
    assert "dependency-drift-report.json" in report_commands
    upload = next(
        step for step in report_job["steps"] if "actions/upload-artifact" in step.get("uses", "")
    )
    assert upload["with"]["path"] == "dependency-drift-report.json"
    assert upload["with"]["if-no-files-found"] == "error"
    assert all("continue-on-error" not in step for job in jobs.values() for step in job["steps"])
    assert unit_job["name"] == "unit-and-package (Python 3.14, default production lane)"
    assert dependency_job["name"] == ("dependency compatibility (${{ matrix.lane }}, Python 3.14)")
    assert dependency_job["timeout-minutes"] == 15
    assert dependency_job["strategy"]["matrix"]["lane"] == [
        "minimum-supported",
        "current-compatible",
    ]
    assert dependency_steps[-1]["env"]["PYTHONUNBUFFERED"] == "1"
    assert "-o faulthandler_timeout=120" in dependency_commands
    assert "--basetemp D:\\comsol_pytest\\dependency-main" in dependency_commands
    assert "New-Item -ItemType Directory -Force -Path D:\\comsol_pytest" in dependency_commands
    assert "--ignore development_kit/tests/test_control_plane_startup.py" in dependency_commands
    assert "test_control_plane_startup.py --basetemp" in dependency_commands
    assert any(
        "constraints/minimum_supported_py314.txt" in str(step.get("run", ""))
        for step in dependency_steps
    )
    assert "--upgrade-strategy eager" in dependency_commands
    assert security_job["name"] == "locked runtime vulnerability policy"
    assert gui_job["name"] == "Settings GUI, package, and installed entry"
    assert "settings_gui/tests" in gui_commands
    assert "test_settings_gui_direct_entry.py" in gui_commands
    assert "settings_gui_package_probe.py" in gui_commands
    assert "installed_package_probe.py" in gui_commands
    assert "forbidden process" in gui_commands
    assert "pip-audit==2.10.1" in security_commands
    assert "constraints/release_locked_py314.txt --no-deps --format json" in security_commands
    assert "vulnerability_allowlist.json" in security_commands
    assert "security_gate.py" in security_commands
    assert "dependency_license_gate.py" in dependency_commands
    assert "dependency_license_review.json" in dependency_commands
    assert "quality_gate.py" in unit_commands
    assert "release_locked_py314.txt" in unit_commands
    assert "-m integration" not in all_commands
    assert "RUN_REAL_COMSOL" in real_gate
    assert 'choices=["RUN_REAL_COMSOL"]' in real_gate


def test_release_dependency_lock_is_complete_and_matches_current_lane(tmp_path):
    lane = f"{__import__('sys').version_info.major}.{__import__('sys').version_info.minor}"
    lock = tmp_path / "lock.txt"
    lock.write_text(
        f"# Python-Lane: {lane}\nexample==1.0 \\\n    --hash=sha256:" + "a" * 64 + "\n",
        encoding="utf-8",
    )
    assert _lock_lane(lock) == lane
    assert _validated_dependency_lock(lock) == lock.resolve()

    rendered = _render_lock(
        platform_name=lock_generator.SUPPORTED_RELEASE_PLATFORM,
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


def test_release_lock_rejects_unhashed_direct_reference_dependencies():
    assert lock_generator._runtime_pins("comsol-mcp @ file:///tmp/root.whl\nexample==1.2.3\n") == [
        "example==1.2.3"
    ]

    with pytest.raises(RuntimeError, match="non-exact pip freeze entry"):
        lock_generator._runtime_pins("example @ https://packages.invalid/example-1.2.3.whl\n")


def test_installed_profiles_must_share_one_release_inventory():
    first = {"schema_registry_sha256": "a" * 64, "schema_entry_count": 10}
    assert (
        installed_package_probe._bind_release_inventory(
            None,
            first,
            profile="core",
        )
        == first
    )
    assert (
        installed_package_probe._bind_release_inventory(
            first,
            dict(first),
            profile="full",
        )
        is first
    )
    with pytest.raises(AssertionError, match="release inventory differs"):
        installed_package_probe._bind_release_inventory(
            first,
            {**first, "schema_entry_count": 11},
            profile="full",
        )

    identity = {"schema_name": "comsol_mcp.deployment_identity"}
    assert (
        installed_package_probe._consistent_deployment_identity([identity, dict(identity)])
        is identity
    )
    with pytest.raises(AssertionError, match="no deployment identities"):
        installed_package_probe._consistent_deployment_identity([])
    with pytest.raises(AssertionError, match="disagree"):
        installed_package_probe._consistent_deployment_identity(
            [identity, {"schema_name": "other"}]
        )


def test_installed_gui_launcher_pe_subsystem_is_read_without_execution(tmp_path):
    launcher = tmp_path / "settings-gui.exe"
    raw = bytearray(0x80 + 24 + 70)
    raw[:2] = b"MZ"
    struct.pack_into("<I", raw, 0x3C, 0x80)
    raw[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", raw, 0x80 + 24, 0x20B)
    struct.pack_into("<H", raw, 0x80 + 24 + 68, 2)
    launcher.write_bytes(raw)

    assert installed_package_probe._windows_pe_subsystem(launcher) == 2

    launcher.write_bytes(b"not-pe")
    with pytest.raises(AssertionError, match="Windows PE"):
        installed_package_probe._windows_pe_subsystem(launcher)


def test_installed_direct_entry_probe_cleans_root_after_child_failure(tmp_path, monkeypatch):
    import settings_gui.desktop_shortcut as shortcut_module

    monkeypatch.setattr(shortcut_module, "installed_entry_executable", lambda: tmp_path / "x.exe")
    monkeypatch.setattr(shortcut_module, "known_desktop_path", lambda: tmp_path / "desktop")
    monkeypatch.setattr(installed_package_probe, "_shortcut_bytes_identity", lambda _path: None)
    monkeypatch.setattr(installed_package_probe, "_forbidden_process_snapshot", lambda: {})

    def fail_child(*_args, cwd, **_kwargs):
        (Path(cwd) / "child-residue.tmp").write_text("residue", encoding="utf-8")
        raise subprocess.TimeoutExpired("settings", 20)

    monkeypatch.setattr(installed_package_probe.subprocess, "run", fail_child)

    with pytest.raises(subprocess.TimeoutExpired):
        installed_package_probe._probe_direct_settings_entry(tmp_path)

    assert not (tmp_path / "settings-gui-direct-entry-probe").exists()


def test_installed_probe_accepts_transient_waited_launcher_enumeration(monkeypatch):
    snapshots = iter([{101: "comsol-mcp-settings.exe"}, {}])
    monkeypatch.setattr(
        installed_package_probe,
        "_forbidden_process_snapshot",
        lambda: next(snapshots),
    )

    assert installed_package_probe._wait_for_forbidden_process_exit({}) == {}


def test_installed_probe_rejects_persistent_forbidden_process(monkeypatch):
    monkeypatch.setattr(
        installed_package_probe,
        "_forbidden_process_snapshot",
        lambda: {101: "comsol-mcp-settings.exe"},
    )

    assert installed_package_probe._wait_for_forbidden_process_exit({}, settle_seconds=0) == {
        101: "comsol-mcp-settings.exe"
    }


def test_release_receipt_accepts_external_lock_and_probes_drop_pythonpath(
    tmp_path,
    monkeypatch,
):
    external = (tmp_path / "external-lock.txt").resolve()
    assert _dependency_lock_location(external) == {
        "path": str(external),
        "path_scope": "absolute_external",
    }

    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "shadow"))
    assert "PYTHONPATH" not in _sanitized_probe_environment()
    source = Path(release_gate_module.__file__).read_text(encoding="utf-8")
    assert source.count("env=environment") == 7


def test_release_lock_binds_the_declared_platform_to_the_target_interpreter(monkeypatch):
    calls = []

    def fake_run(command, *, cwd=lock_generator.ROOT, capture=False):
        calls.append((command, cwd, capture))
        return "CPython\n3.14\nwin-amd64\n"

    monkeypatch.setattr(lock_generator, "_run", fake_run)
    target = Path("C:/Python314/python.exe")

    assert lock_generator._validated_target_platform(target) == "win-amd64"
    assert calls == [
        (
            [
                str(target),
                "-c",
                (
                    "import platform, sys, sysconfig; "
                    "print(platform.python_implementation()); "
                    "print(f'{sys.version_info.major}.{sys.version_info.minor}'); "
                    "print(sysconfig.get_platform())"
                ),
            ],
            lock_generator.ROOT,
            True,
        )
    ]


@pytest.mark.parametrize("platform_name", ["win32", "win-arm64", "linux-x86_64", ""])
def test_release_lock_rejects_a_non_amd64_target_interpreter(monkeypatch, platform_name):
    monkeypatch.setattr(
        lock_generator,
        "_run",
        lambda *_args, **_kwargs: f"CPython\n3.14\n{platform_name}\n",
    )

    with pytest.raises(SystemExit, match="win-amd64 target interpreter"):
        lock_generator._validated_target_platform(Path("C:/Python314/python.exe"))


@pytest.mark.parametrize("identity", ["PyPy\n3.14\nwin-amd64\n", "CPython\n3.13\nwin-amd64\n"])
def test_release_lock_rejects_unsupported_interpreter_identity(monkeypatch, identity):
    monkeypatch.setattr(lock_generator, "_run", lambda *_args, **_kwargs: identity)

    with pytest.raises(SystemExit, match="CPython 3.14"):
        lock_generator._validated_target_platform(Path("C:/Python314/python.exe"))


def test_release_lock_surfaces_bounded_captured_command_diagnostics(monkeypatch):
    monkeypatch.setattr(
        lock_generator.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(7, ["pip"], stderr="resolver failed")
        ),
    )

    with pytest.raises(RuntimeError, match="resolver failed"):
        lock_generator._run(["pip"], capture=True)


def test_ci_and_local_ignore_preserve_failure_and_secret_boundaries():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "$auditExitCode = $LASTEXITCODE" in ci
    assert "if ($auditExitCode -gt 1) { exit $auditExitCode }" in ci
    format_command = "python -m ruff format --check settings_gui"
    format_tail = ci.split(format_command, 1)[1].split("python -m ruff check", 1)[0]
    assert "if ($LASTEXITCODE -ne 0)" in format_tail
    assert ".env\n" in ignored
    assert ".env.*\n" in ignored
    assert "!.env.example\n" in ignored


def test_release_lock_installs_from_the_exact_downloaded_wheelhouse(tmp_path, monkeypatch):
    source = tmp_path / "comsol_mcp-0.6.0-py3-none-any.whl"
    source.write_bytes(b"root-wheel")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    commands = []

    def fake_run(command, *, cwd=lock_generator.ROOT, capture=False):
        commands.append(list(command))
        if "download" in command:
            destination = Path(command[command.index("--dest") + 1])
            (destination / source.name).write_bytes(source.read_bytes())
            (destination / "example-1.0-py3-none-any.whl").write_bytes(b"dependency-wheel")
        if "freeze" in command:
            return "comsol-mcp==0.6.0\nexample==1.0\n"
        return ""

    monkeypatch.setattr(lock_generator, "_run", fake_run)

    _python, download_dir, freeze = lock_generator._resolve_and_install_wheelhouse(
        Path("C:/Python314/python.exe"), source, workspace
    )

    download_index = next(index for index, command in enumerate(commands) if "download" in command)
    install_index = next(index for index, command in enumerate(commands) if "install" in command)
    install = commands[install_index]
    assert download_index < install_index
    assert "--no-index" in install
    assert install[install.index("--find-links") + 1] == str(download_dir)
    assert Path(install[-1]).parent == download_dir
    assert Path(install[-1]).read_bytes() == source.read_bytes()
    assert freeze == "comsol-mcp==0.6.0\nexample==1.0\n"


def test_minimum_supported_lane_matches_reviewed_manifest_and_package_ranges():
    manifest = _json(ROOT / "constraints" / "tested_versions.json")
    lane = manifest["minimum_supported_python_3_14"]
    constraints_path = ROOT / lane["constraints"]
    pins = {}
    for line in constraints_path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        name, version = line.split("==", 1)
        pins[name] = version

    assert lane["python"] == "3.14.6"
    assert lane["abi"] == "cp314-win_amd64"
    assert lane["gil_mode"] == "standard"
    assert pins == lane["direct_dependencies"]
    assert lane["local_resolution_result"] == "non-editable package install and pip check passed"
    assert lane["hosted_ci_result"] == "passed"
    hosted = manifest["hosted_dependency_ci"]
    assert hosted["workflow"] == "solver-free-ci"
    assert hosted["result"] == "passed"
    assert re.fullmatch(r"[0-9a-f]{40}", hosted["source_commit"])
    assert isinstance(hosted["run_id"], int) and hosted["run_id"] > 0
    assert set(hosted["jobs"].values()) == {"passed"}
    release_lock = manifest["release_lock"]
    lock_path = ROOT / release_lock["path"]
    canonical_lock = lock_path.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(canonical_lock).hexdigest() == release_lock["sha256"]


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
    probe = (ROOT / "development_kit" / "scripts" / "installed_package_probe.py").read_text(
        encoding="utf-8"
    )

    assert "for profile in PROFILE_NAMES" in probe
    assert "snapshot_tool_schemas" in probe
    assert "deployment_identity" in probe
    assert "release_inventories" in probe
    assert "installed_site_package" in probe
    assert "installed-package discovery must not start COMSOL" in probe
    assert {"chromadb", "sentence_transformers", "torch"} <= set(re.findall(r'"([a-z_]+)"', probe))
    release_gate = (ROOT / "development_kit" / "scripts" / "release_gate.py").read_text(
        encoding="utf-8"
    )
    assert "sbom.cdx.json" in release_gate
    assert "release_gate_receipt" in release_gate
    assert "receipt_sha256" in release_gate
    assert "inventory_hashes" in release_gate
    assert "installed_stdio_probe.py" in release_gate
    assert "installed_stdio_probe" in release_gate


def test_release_documentation_requires_restart_and_clean_tree():
    checklist = (ROOT / "development_kit" / "docs" / "release_checklist.md").read_text(
        encoding="utf-8"
    )
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
