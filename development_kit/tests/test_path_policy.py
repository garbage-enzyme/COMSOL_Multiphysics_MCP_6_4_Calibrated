"""Configured model-read and owned-artifact path containment tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import unicodedata

import pytest

from src.operation_arbiter import guard_tool_call
from src.path_policy import (
    ARTIFACT_WRITE_ROOT_ENV,
    MODEL_READ_ROOTS_ENV,
    PathPolicy,
)
from src.tools.capabilities import get_capabilities
from src.tools.profiles import ProfileSelection


@pytest.fixture
def ascii_root():
    base = Path("D:/comsol_runtime") if Path("D:/").exists() else Path(
        os.environ.get("SystemRoot", "C:/Windows")
    ) / "Temp"
    root = Path(tempfile.mkdtemp(prefix="comsol_mcp_path_policy_", dir=base))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _policy(tmp_path, ascii_root):
    read_root = tmp_path / "models"
    write_root = ascii_root / "artifacts"
    read_root.mkdir()
    return PathPolicy.from_environment({
        MODEL_READ_ROOTS_ENV: str(read_root),
        ARTIFACT_WRITE_ROOT_ENV: str(write_root),
    }), read_root, write_root


def _selection(name):
    return ProfileSelection(
        name=name,
        environment_variable="COMSOL_MCP_PROFILE",
        default_used=False,
        source="path-policy-test",
    )


def test_model_reads_require_exact_containment_and_existing_file(tmp_path, ascii_root):
    policy, read_root, _ = _policy(tmp_path, ascii_root)
    model = read_root / "source.mph"
    model.write_bytes(b"fixture")
    external = tmp_path / "external.mph"
    external.write_bytes(b"external")

    accepted = policy.validate_model_read(str(model), suffixes=(".mph",))
    assert accepted.normalized_path == model.resolve()
    with pytest.raises(ValueError, match="escapes"):
        policy.validate_model_read(str(read_root / ".." / "external.mph"))
    with pytest.raises(ValueError, match="absolute"):
        policy.validate_model_read("source.mph")


def test_symlink_or_junction_escape_fails_closed(tmp_path, ascii_root):
    policy, read_root, _ = _policy(tmp_path, ascii_root)
    external = tmp_path / "external"
    external.mkdir()
    (external / "source.mph").write_bytes(b"external")
    link = read_root / "linked"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(ValueError, match="escapes"):
        policy.validate_model_read(str(link / "source.mph"))


def test_unicode_alias_reserved_name_and_device_paths_are_rejected(
    tmp_path, ascii_root
):
    policy, read_root, _ = _policy(tmp_path, ascii_root)
    decomposed = unicodedata.normalize("NFD", str(read_root / "café.mph"))
    with pytest.raises(ValueError, match="NFC"):
        policy.validate_model_read(decomposed)
    with pytest.raises(ValueError, match="reserved"):
        policy.validate_model_read(str(read_root / "CON.mph"))
    with pytest.raises(ValueError, match="device|extended"):
        policy.validate_model_read(r"\\?\C:\models\source.mph")


def test_artifact_writes_are_ascii_new_and_contained(tmp_path, ascii_root):
    policy, _, write_root = _policy(tmp_path, ascii_root)
    accepted = policy.validate_artifact_write(str(write_root / "result.json"))
    assert accepted.normalized_path == (write_root / "result.json").resolve()
    existing = write_root / "existing.json"
    existing.write_text("{}", encoding="ascii")
    with pytest.raises(ValueError, match="must not already exist"):
        policy.validate_artifact_write(str(existing))
    with pytest.raises(ValueError, match="ASCII-only"):
        policy.validate_artifact_write(str(write_root / "结果.json"))
    with pytest.raises(ValueError, match="escapes"):
        policy.validate_artifact_write(str(write_root / ".." / "outside.json"))


def test_recommended_profile_wrapper_rejects_unconfigured_model_path(
    tmp_path, ascii_root, monkeypatch
):
    monkeypatch.delenv(MODEL_READ_ROOTS_ENV, raising=False)
    monkeypatch.setenv(ARTIFACT_WRITE_ROOT_ENV, str(ascii_root / "artifacts"))
    called = []

    def model_load(file_path: str):
        called.append(file_path)
        return {"success": True}

    guarded = guard_tool_call(
        model_load,
        tool_name="model_load",
        side_effect_class="filesystem_read_model_mutation",
        concurrency_class="comsol_bound",
        profile_name="core",
    )
    result = guarded(str(tmp_path / "outside.mph"))

    assert result["success"] is False
    assert result["path_policy"]["accepted"] is False
    assert called == []


def test_full_profile_visibly_preserves_legacy_path_compatibility(tmp_path):
    called = []

    def model_load(file_path: str):
        called.append(file_path)
        return {"success": True}

    guarded = guard_tool_call(
        model_load,
        tool_name="model_load",
        side_effect_class="filesystem_read_model_mutation",
        concurrency_class="solver_free",
        profile_name="full",
    )
    result = guarded("relative-legacy-model.mph")

    assert result["success"] is True
    assert called == ["relative-legacy-model.mph"]
    assert result["path_policy"]["enforced"] is False
    assert result["path_policy"]["compatibility_mode"] == "legacy_broad_paths"


def test_capabilities_redact_roots_and_report_weaker_compatibility(
    tmp_path, ascii_root, monkeypatch
):
    model_root = tmp_path / "models"
    model_root.mkdir()
    monkeypatch.setenv(MODEL_READ_ROOTS_ENV, str(model_root))
    monkeypatch.setenv(ARTIFACT_WRITE_ROOT_ENV, str(ascii_root / "artifacts"))

    core = get_capabilities(_selection("core"))
    full = get_capabilities(_selection("full"))
    serialized = json.dumps(core, ensure_ascii=False)

    assert core["server_safety"]["path_policy"]["enforced"] is True
    assert core["server_safety"]["path_policy"]["model_read_roots_configured"] == 1
    assert core["server_safety"]["path_policy"]["shared_source_roots_configured"] == 1
    assert core["server_safety"]["path_policy"]["shared_snapshot_root_owned"] is True
    assert core["server_safety"]["path_policy"]["shared_snapshot_root_ascii"] is True
    assert full["server_safety"]["path_policy"]["enforced"] is False
    assert full["server_safety"]["compatibility_profile_weaker_guarantees"] is True
    assert str(tmp_path) not in serialized


def test_shared_source_and_fixed_snapshot_root_reuse_containment(tmp_path, ascii_root):
    policy, read_root, write_root = _policy(tmp_path, ascii_root)
    source = read_root / "shared.mph"
    source.write_bytes(b"immutable")
    snapshot = write_root / "shared_snapshots" / "copy.mph"

    source_decision = policy.validate_shared_source(str(source))
    snapshot_decision = policy.validate_shared_snapshot_write(str(snapshot))

    assert source_decision.kind == "shared_source_read"
    assert source_decision.normalized_path == source.resolve()
    assert snapshot_decision.kind == "shared_snapshot_write"
    assert snapshot_decision.normalized_path == snapshot.resolve()
    assert policy.shared_snapshot_root == write_root / "shared_snapshots"


def test_shared_model_lock_wrapper_normalizes_immutable_source(
    tmp_path, ascii_root, monkeypatch
):
    _policy_value, read_root, write_root = _policy(tmp_path, ascii_root)
    source = read_root / "shared.mph"
    source.write_bytes(b"immutable")
    outside = tmp_path / "outside.mph"
    outside.write_bytes(b"outside")
    monkeypatch.setenv(MODEL_READ_ROOTS_ENV, str(read_root))
    monkeypatch.setenv(ARTIFACT_WRITE_ROOT_ENV, str(write_root))
    called = []

    def shared_model_lock(
        collaboration_mode: str,
        immutable_source_path: str | None = None,
        immutable_source_sha256: str | None = None,
    ):
        called.append(immutable_source_path)
        return {"success": True}

    guarded = guard_tool_call(
        shared_model_lock,
        tool_name="shared_model_lock",
        side_effect_class="shared_model_guard",
        concurrency_class="solver_free",
        profile_name="desktop_shared",
    )
    accepted = guarded("interactive_inspection", str(source), "a" * 64)
    rejected = guarded("interactive_inspection", str(outside), "a" * 64)

    assert accepted["success"] is True
    assert accepted["path_policy"]["validated_kinds"] == ["shared_source_read"]
    assert called == [str(source.resolve())]
    assert rejected["success"] is False
    assert rejected["path_policy"]["accepted"] is False


def test_shared_snapshot_rejects_external_alias_and_overwrite(tmp_path, ascii_root):
    policy, _, write_root = _policy(tmp_path, ascii_root)
    outside = ascii_root / "outside.mph"
    outside.write_bytes(b"sentinel")

    with pytest.raises(ValueError, match="escapes"):
        policy.validate_shared_snapshot_write(str(outside))

    snapshot = write_root / "shared_snapshots" / "existing.mph"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_bytes(b"first")
    with pytest.raises(ValueError, match="must not already exist"):
        policy.validate_shared_snapshot_write(str(snapshot))
    assert outside.read_bytes() == b"sentinel"
    assert snapshot.read_bytes() == b"first"


def test_configured_reparse_root_is_rejected(tmp_path, ascii_root):
    real_root = tmp_path / "real_models"
    real_root.mkdir()
    linked_root = tmp_path / "linked_models"
    try:
        linked_root.symlink_to(real_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(ValueError, match="symlink or junction"):
        PathPolicy.from_environment({
            MODEL_READ_ROOTS_ENV: str(linked_root),
            ARTIFACT_WRITE_ROOT_ENV: str(ascii_root / "artifacts"),
        })
