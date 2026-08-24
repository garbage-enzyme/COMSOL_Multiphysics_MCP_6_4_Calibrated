"""Solver-free contract suite for offline model identity and checkpoints.

The suite never starts COMSOL, Java, MPh, or JPype: live-session lanes are
exercised with injected passive providers plus the real disconnected-session
path, and runtime versions must come from local package metadata without
importing those distributions.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer

from comsol_mcp.contracts.mph_inspection import MphInspectionLimits
from comsol_mcp.evidence.inspection.archive import MphInspectionError
from comsol_mcp.evidence.inspection.summary import build_mph_inspection_summary
from comsol_mcp.evidence.model_identity import (
    MODEL_IDENTITY_SCHEMA_NAME,
    MODEL_IDENTITY_SCHEMA_VERSION,
    ModelIdentityError,
    build_model_identity,
)
from comsol_mcp.schema_registry import check_schema_support
from comsol_mcp.tools.model_identity import register_model_identity_tools

_FILEVERSION_TEMPLATE = "{marker}:COMSOL {version}\n"

_MODELINFO_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<modelInfo comsolVersion="{version}" modelType="MODEL" nodeType="solved"
 isRunnable="false" title="{title}" description="" startMode="edit"
 lastComputationTime="" lastComputationDate="">
  <historyInfo createdIn="COMSOL Multiphysics {version}" author=""/>
  <licenseInfo products="COMSOL"/>
</modelInfo>
"""

_DMODEL_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<Model>
  <ModelParam>{params}</ModelParam>
  <PhysicsList>
    <Physics tag="ewfd" op="ElectromagneticWavesFrequencyDomain" name="ewfd"/>
  </PhysicsList>
  <StudyList><Study tag="std1" name="Study 1"/></StudyList>
</Model>
"""


def _write_valid_mph(
    path: Path,
    *,
    version: str = "6.4.0.293",
    title: str = "fixture",
    params: tuple[tuple[str, str], ...] = (("wl", "1.0[um]"),),
    with_savepoint: bool = True,
) -> Path:
    rendered_params = "".join(
        f'<expressions name="{name}" expr="{expr}"/>' for name, expr in params
    )
    payload = {
        "fileversion": _FILEVERSION_TEMPLATE.format(marker=2092, version=version).encode(),
        "modelinfo.xml": _MODELINFO_TEMPLATE.format(version=version, title=title).encode(),
        "usedlicenses.txt": b"COMSOL\n",
        "dmodel.xml": _DMODEL_TEMPLATE.format(params=rendered_params).encode(),
    }
    if with_savepoint:
        payload["savepoint1/savepoint.xml"] = b"<savepoint/>"
    with zipfile.ZipFile(path, "w") as archive:
        for name in sorted(payload):
            archive.writestr(name, payload[name])
    return path


def _available_session(**overrides: Any) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "available": True,
        "active_model_tag": "m1",
        "bound_model_tag": "m1",
        "label": None,
        "revision": None,
        "comsol_version": None,
        "shared_session": False,
    }
    snapshot.update(overrides)
    return snapshot


# ---------------------------------------------------------------------------
# Offline identity contract
# ---------------------------------------------------------------------------


def test_offline_identity_is_complete_deterministic_and_ready(tmp_path):
    fixture = _write_valid_mph(tmp_path / "compact.mph")
    first = build_model_identity(fixture)
    second = build_model_identity(fixture)

    assert first["schema_name"] == MODEL_IDENTITY_SCHEMA_NAME
    assert first["schema_version"] == MODEL_IDENTITY_SCHEMA_VERSION
    for field in (
        "active_model_tag",
        "bound_model_tag",
        "title",
        "label",
        "source_path_redacted",
        "derived_path_redacted",
        "model_path",
        "comsol_version",
        "mph_version",
        "jpype_version",
        "python_version",
        "source_sha256",
        "revision",
        "read_only",
        "checkpoint_ready",
        "checkpoint_path_redacted",
        "checkpoint_sha256",
        "shared_session",
        "warnings",
    ):
        assert field in first, field
    assert first["identity_disposition"] == "ready"
    assert first["failure_reasons"] == []
    assert first["read_only"] is True
    assert first["checkpoint_ready"] is False
    assert first["checkpoint_sha256"] is None
    assert first["shared_session"] is False
    assert first["session_identity"]["availability"] == "not_requested"
    assert first["source_sha256"] == first["file_sha256"]
    assert first["comsol_version"] == "6.4.0.293"
    assert first["title"] == first["label"] == "fixture"
    assert "source_provenance_undeclared" in first["warnings"]
    assert first == second


def test_runtime_versions_use_package_metadata_without_importing_solver_packages(
    tmp_path,
):
    """A fresh interpreter must gain identity without importing mph or jpype.

    The probe runs in a subprocess because parallel pytest workers legitimately
    share a process in which other suites may already have imported those
    packages lazily; the identity builder itself must never need them.
    """
    fixture = _write_valid_mph(tmp_path / "meta.mph")
    root = Path(__file__).parents[2]
    code = (
        "import json, sys\n"
        "from importlib import metadata as importlib_metadata\n"
        "identity_parent = sys.argv[1]\n"
        "fixture = sys.argv[2]\n"
        "from comsol_mcp.evidence.model_identity import build_model_identity\n"
        "identity = build_model_identity(fixture)\n"
        "try:\n"
        "    expected_mph = importlib_metadata.version('MPh')\n"
        "except importlib_metadata.PackageNotFoundError:\n"
        "    expected_mph = None\n"
        "print(json.dumps({\n"
        "    'mph_version': identity['mph_version'],\n"
        "    'expected_mph': expected_mph,\n"
        "    'python_version': identity['python_version'],\n"
        "    'mph_loaded': 'mph' in sys.modules,\n"
        "    'jpype_loaded': 'jpype' in sys.modules,\n"
        "    'identity_parent_loaded': identity_parent in sys.modules,\n"
        "}))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), str(fixture)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["mph_version"] == payload["expected_mph"]
    assert payload["python_version"].count(".") == 2
    assert payload["mph_loaded"] is False
    assert payload["jpype_loaded"] is False


def test_save_reload_equivalence_preserves_the_identity_fingerprint(tmp_path):
    saved_dir = tmp_path / "reload"
    saved_dir.mkdir()
    saved = _write_valid_mph(saved_dir / "same-name.mph", with_savepoint=False)
    reloaded_dir = tmp_path / "reloaded"
    reloaded_dir.mkdir()
    reloaded = reloaded_dir / "same-name.mph"
    shutil.copyfile(saved, reloaded)

    saved_identity = build_model_identity(saved)
    reloaded_identity = build_model_identity(reloaded)
    assert saved_identity == reloaded_identity
    assert saved_identity["identity_fingerprint"] == reloaded_identity["identity_fingerprint"]


def test_derived_source_pair_binds_provenance_and_hash_conflicts_fail_closed(tmp_path):
    source = _write_valid_mph(tmp_path / "base.mph")
    derived = _write_valid_mph(
        tmp_path / "derived.mph", params=(("wl", "1.55[um]"),), with_savepoint=False
    )

    bound = build_model_identity(derived, source_path=source)
    assert bound["identity_disposition"] == "ready"
    assert bound["derived_path_redacted"] == "**/derived.mph"
    assert bound["source_path_redacted"] == "**/base.mph"
    assert bound["source_sha256"] == build_mph_inspection_summary(source)["sha256"]

    mismatched = build_model_identity(
        derived,
        source_path=source,
        expected_source_sha256="f" * 64,
    )
    assert mismatched["identity_disposition"] == "pause_and_repair"
    assert "declared_source_hash_mismatch" in mismatched["failure_reasons"]

    identical = build_model_identity(source, source_path=source)
    assert identical["derived_path_redacted"] is None
    assert "source_and_derived_bytes_identical" in identical["warnings"]

    missing_source = build_model_identity(derived, source_path=tmp_path / "absent.mph")
    assert missing_source["identity_disposition"] == "pause_and_repair"
    assert "source_unavailable" in missing_source["failure_reasons"]
    assert missing_source["source_sha256"] is None


def test_declared_file_hash_mismatch_is_pause_and_repair(tmp_path):
    fixture = _write_valid_mph(tmp_path / "hash.mph")
    identity = build_model_identity(fixture, expected_file_sha256="0" * 64)
    assert identity["identity_disposition"] == "pause_and_repair"
    assert identity["failure_reasons"] == ["declared_file_hash_mismatch"]


def test_source_over_declared_byte_limit_fails_closed(tmp_path):
    fixture = _write_valid_mph(tmp_path / "big-source.mph")
    limits = MphInspectionLimits(max_archive_bytes=1)
    with pytest.raises(MphInspectionError) as excinfo:
        build_model_identity(fixture, limits=limits)
    assert excinfo.value.reason_code == "mph_file_too_large"


# ---------------------------------------------------------------------------
# Checkpoint readiness
# ---------------------------------------------------------------------------


def test_checkpoint_readiness_tracks_bytes_and_declared_hashes(tmp_path):
    model = _write_valid_mph(tmp_path / "with-checkpoint.mph")
    checkpoint = _write_valid_mph(tmp_path / "ckpt.mph")
    checkpoint_hash = build_mph_inspection_summary(checkpoint)["sha256"]

    ready = build_model_identity(model, checkpoint_path=checkpoint)
    assert ready["checkpoint_ready"] is True
    assert ready["checkpoint_path_redacted"] == "**/ckpt.mph"
    assert ready["checkpoint_sha256"] == checkpoint_hash
    assert ready["identity_disposition"] == "ready"

    confirmed = build_model_identity(
        model, checkpoint_path=checkpoint, expected_checkpoint_sha256=checkpoint_hash.upper()
    )
    assert confirmed["checkpoint_ready"] is True

    drifted = build_model_identity(
        model, checkpoint_path=checkpoint, expected_checkpoint_sha256="a" * 64
    )
    assert drifted["checkpoint_ready"] is False
    assert "declared_checkpoint_hash_mismatch" in drifted["failure_reasons"]
    assert drifted["identity_disposition"] == "pause_and_repair"


def test_missing_checkpoint_cases_are_typed(tmp_path):
    model = _write_valid_mph(tmp_path / "plain.mph")

    undeclared = build_model_identity(model)
    assert undeclared["checkpoint_ready"] is False
    assert "checkpoint_not_declared" in undeclared["warnings"]
    assert undeclared["identity_disposition"] == "ready"

    expected_absent = build_model_identity(
        model,
        checkpoint_path=tmp_path / "absent.mph",
        expected_checkpoint_sha256="b" * 64,
    )
    assert expected_absent["checkpoint_ready"] is False
    assert expected_absent["failure_reasons"] == ["checkpoint_unavailable"]
    assert expected_absent["identity_disposition"] == "pause_and_repair"

    empty = tmp_path / "empty.mph"
    empty.write_bytes(b"")
    empty_identity = build_model_identity(model, checkpoint_path=empty)
    assert empty_identity["checkpoint_ready"] is False
    assert "checkpoint_empty" in empty_identity["failure_reasons"]
    assert empty_identity["checkpoint_sha256"] is None

    declared_only = build_model_identity(model, expected_checkpoint_sha256="c" * 64)
    assert declared_only["failure_reasons"] == ["checkpoint_missing"]
    assert declared_only["identity_disposition"] == "pause_and_repair"


# ---------------------------------------------------------------------------
# Session-identity lanes
# ---------------------------------------------------------------------------


def test_requested_session_identity_uses_passive_provider_evidence(tmp_path):
    fixture = _write_valid_mph(tmp_path / "session.mph")
    identity = build_model_identity(
        fixture, session_provider=lambda: _available_session(revision="d" * 64)
    )
    assert identity["session_identity"]["availability"] == "available"
    assert identity["active_model_tag"] == "m1"
    assert identity["bound_model_tag"] == "m1"
    assert identity["revision"] == "d" * 64
    assert identity["shared_session"] is False
    assert identity["identity_disposition"] == "ready"


def test_shared_session_binding_is_reported_without_guessing(tmp_path):
    fixture = _write_valid_mph(tmp_path / "shared.mph")

    def provider() -> dict[str, Any]:
        return _available_session(shared_session=True, bound_model_tag=None)

    identity = build_model_identity(fixture, session_provider=provider)
    assert identity["shared_session"] is True
    assert identity["bound_model_tag"] is None
    assert identity["active_model_tag"] == "m1"


def test_unavailable_and_failed_providers_are_structured_not_fatal(tmp_path):
    fixture = _write_valid_mph(tmp_path / "unavailable.mph")

    none_identity = build_model_identity(fixture, session_provider=lambda: None)
    assert none_identity["session_identity"]["availability"] == "unavailable"
    assert none_identity["session_identity"]["reason_codes"] == [
        "identity_session_provider_unavailable"
    ]
    assert none_identity["active_model_tag"] is None
    assert "live_session_unavailable" in none_identity["warnings"]
    assert none_identity["identity_disposition"] == "ready"

    def failing() -> dict[str, Any]:
        raise RuntimeError("observer lost")

    failed_identity = build_model_identity(fixture, session_provider=failing)
    assert failed_identity["session_identity"]["availability"] == "unavailable"
    assert failed_identity["session_identity"]["reason_codes"] == [
        "identity_session_provider_failed"
    ]

    disconnected = build_model_identity(
        fixture,
        session_provider=lambda: {
            "available": False,
            "reason_codes": ["no_connected_session"],
            "shared_session": False,
        },
    )
    assert disconnected["session_identity"]["reason_codes"] == ["no_connected_session"]
    assert disconnected["identity_disposition"] == "ready"


@pytest.mark.parametrize(
    "provider",
    [
        lambda: {"available": True},
        lambda: {"available": True, "active_model_tag": "bad tag!", "shared_session": False},
        lambda: {"available": False, "reason_codes": [], "unexpected": 1},
        lambda: {"available": "yes", "shared_session": False},
        lambda: {"available": True, "active_model_tag": "m1", "shared_session": "maybe"},
    ],
)
def test_invalid_provider_shapes_raise_a_typed_refusal(tmp_path, provider):
    fixture = _write_valid_mph(tmp_path / "invalid-provider.mph")
    with pytest.raises(ModelIdentityError) as excinfo:
        build_model_identity(fixture, session_provider=provider)
    assert excinfo.value.reason_code == "identity_session_provider_invalid"


def test_session_version_conflict_is_pause_and_repair(tmp_path):
    fixture = _write_valid_mph(tmp_path / "conflict.mph")
    identity = build_model_identity(
        fixture,
        session_provider=lambda: _available_session(comsol_version="9.9.9.999"),
    )
    assert identity["failure_reasons"] == ["declared_session_version_mismatch"]
    assert identity["identity_disposition"] == "pause_and_repair"


def test_matching_session_version_stays_ready(tmp_path):
    fixture = _write_valid_mph(tmp_path / "match.mph")
    identity = build_model_identity(
        fixture,
        session_provider=lambda: _available_session(comsol_version="6.4.0.293"),
    )
    assert identity["identity_disposition"] == "ready"


# ---------------------------------------------------------------------------
# Redaction and registry membership
# ---------------------------------------------------------------------------


def test_non_ascii_external_name_stays_redacted(tmp_path):
    fixture = _write_valid_mph(tmp_path / "\u6a21\u578b.mph")
    identity = build_model_identity(fixture)
    serialized = json.dumps(identity, ensure_ascii=False)
    assert identity["model_path"] == "**/\u6a21\u578b.mph"
    assert str(tmp_path) not in serialized
    assert "\\" not in serialized


def test_schema_registry_supports_the_published_contract():
    read_support = check_schema_support(MODEL_IDENTITY_SCHEMA_NAME, "1.0.0")
    assert read_support["supported"] is True
    write_support = check_schema_support(MODEL_IDENTITY_SCHEMA_NAME, "1.0.0", for_write=True)
    assert write_support["supported"] is True
    assert write_support["producer"] == "comsol_mcp.evidence.model_identity"


# ---------------------------------------------------------------------------
# Public MCP dispatch
# ---------------------------------------------------------------------------


def _tools() -> dict:
    server = MCPServer("model-identity-test")
    register_model_identity_tools(server)
    return server._tool_manager._tools


def test_public_dispatch_reports_success_and_structured_refusals(tmp_path):
    tools = _tools()
    fixture = _write_valid_mph(tmp_path / "dispatch.mph")
    result = tools["model_identity"].fn(str(fixture))
    assert result["success"] is True
    assert result["solver_started"] is False
    assert result["filesystem_modified"] is False
    assert result["schema_name"] == MODEL_IDENTITY_SCHEMA_NAME
    assert result["identity"]["identity_disposition"] == "ready"

    absent = tools["model_identity"].fn(str(tmp_path / "absent.mph"))
    assert absent["success"] is False
    assert absent["reason_code"] == "mph_source_unavailable"
    assert absent["solver_started"] is False

    rejected = tools["model_identity"].fn(str(fixture), limits=MphInspectionLimits(max_entries=1))
    assert rejected["success"] is False
    assert rejected["reason_code"] == "mph_too_many_entries"


def test_dispatch_session_request_on_a_disconnected_process_is_passive(tmp_path):
    tools = _tools()
    fixture = _write_valid_mph(tmp_path / "passive.mph")
    result = tools["model_identity"].fn(str(fixture), request_session_identity=True)
    assert result["success"] is True
    assert result["solver_started"] is False
    session_identity = result["identity"]["session_identity"]
    assert session_identity["availability"] in {"unavailable", "available"}
    if session_identity["availability"] == "unavailable":
        assert "no_connected_session" in session_identity["reason_codes"]
        assert "live_session_unavailable" in result["identity"]["warnings"]


def test_passive_provider_degrades_a_connected_model_free_session(monkeypatch):
    """A connected client with zero tracked models must stay structured."""
    from comsol_mcp.tools import model_identity as adapter
    from comsol_mcp.tools import session as session_module

    class _FakeManager:
        def get_status(self):
            return {"connected": True, "models": [], "current_model": None}

    monkeypatch.setattr(session_module, "session_manager", _FakeManager(), raising=False)
    snapshot = adapter._passive_live_session_provider()
    assert snapshot is not None
    assert snapshot["available"] is False
    assert snapshot["reason_codes"] == ["no_active_tracked_model"]

    class _TaggedManager:
        def get_status(self):
            return {
                "connected": True,
                "current_model": None,
                "models": [{"name": "m9", "revision_sha256": "e" * 64}],
                "version": None,
            }

    monkeypatch.setattr(session_module, "session_manager", _TaggedManager(), raising=False)
    available_snapshot = adapter._passive_live_session_provider()
    assert available_snapshot["available"] is True
    assert available_snapshot["active_model_tag"] == "m9"
    assert available_snapshot["revision"] == "e" * 64


def test_identity_modules_never_import_solver_or_process_dependencies():
    # Contracts and evidence stay fully solver-free: no such import anywhere.
    offline_sources = [
        Path("comsol_mcp") / "contracts" / "model_identity.py",
        Path("comsol_mcp") / "evidence" / "model_identity.py",
    ]
    forbidden_prefixes = (
        "import mph",
        "from mph",
        "import jpype",
        "from jpype",
        "import subprocess",
        "from comsol_mcp.tools.session import",
        "from comsol_mcp.tools.shared_session import",
    )
    for relative in offline_sources:
        text = relative.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            assert not stripped.startswith(forbidden_prefixes), (relative, stripped)
            assert "comsol_start(" not in stripped, (relative, stripped)
    # The tool adapter must import solver-free at module level. Only narrow
    # function-local lazy imports for explicitly requested passive session
    # reads are tolerated there.
    tool_source = Path("comsol_mcp") / "tools" / "model_identity.py"
    text = tool_source.read_text(encoding="utf-8")
    for line in text.splitlines():
        assert "comsol_start(" not in line, (tool_source, line)
        if line and not line[0].isspace():
            assert not line.strip().startswith(forbidden_prefixes), (tool_source, line)
