"""Configured model-read and owned-artifact path containment tests."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unicodedata
from pathlib import Path

import pytest
from src.operation_arbiter import guard_tool_call
from src.path_policy import (
    ARTIFACT_WRITE_ROOT_ENV,
    MODEL_READ_ROOTS_ENV,
    PathPolicy,
    ReadPinError,
    pin_validated_reads,
    pin_validated_writes,
)
from src.tools.capabilities import get_capabilities
from src.tools.profiles import ProfileSelection

from development_kit.tests.conftest import _create_ascii_temp_dir


@pytest.fixture
def ascii_root():
    base = (
        Path("D:/comsol_runtime")
        if Path("D:/").exists()
        else Path(os.environ.get("SystemRoot", "C:/Windows")) / "Temp"
    )
    root = Path(tempfile.mkdtemp(prefix="comsol_mcp_path_policy_", dir=base))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _policy(tmp_path, ascii_root):
    read_root = tmp_path / "models"
    write_root = ascii_root / "artifacts"
    read_root.mkdir()
    return (
        PathPolicy.from_environment(
            {
                MODEL_READ_ROOTS_ENV: str(read_root),
                ARTIFACT_WRITE_ROOT_ENV: str(write_root),
            }
        ),
        read_root,
        write_root,
    )


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


def test_unicode_alias_reserved_name_and_device_paths_are_rejected(tmp_path, ascii_root):
    policy, read_root, _ = _policy(tmp_path, ascii_root)
    decomposed = unicodedata.normalize("NFD", str(read_root / "café.mph"))
    with pytest.raises(ValueError, match="NFC"):
        policy.validate_model_read(decomposed)
    with pytest.raises(ValueError, match="reserved"):
        policy.validate_model_read(str(read_root / "CON.mph"))
    with pytest.raises(ValueError, match="device|extended"):
        policy.validate_model_read(r"\\?\C:\models\source.mph")


@pytest.mark.parametrize(
    "value,match",
    [
        (r"\\?\/C:\models\source.mph", "device|extended"),
        (r"//?\\C:\models\source.mph", "device|extended"),
        (r"\models\source.mph", "absolute"),
        (r"C:models\source.mph", "absolute"),
        (r"C:\models\.\source.mph", "dot aliases"),
        (r"C:\models\\source.mph", "ambiguous empty"),
    ],
)
def test_mixed_device_root_relative_and_ambiguous_paths_are_rejected(
    tmp_path, ascii_root, value, match
):
    policy, _read_root, _write_root = _policy(tmp_path, ascii_root)

    with pytest.raises(ValueError, match=match):
        policy.validate_model_read(value)


def test_model_read_rejects_missing_wrong_suffix_and_directory_inputs(tmp_path, ascii_root):
    policy, read_root, _write_root = _policy(tmp_path, ascii_root)
    wrong_suffix = read_root / "source.txt"
    wrong_suffix.write_bytes(b"not-mph")
    directory = read_root / "directory.mph"
    directory.mkdir()

    with pytest.raises(ValueError, match="cannot be resolved"):
        policy.validate_model_read(str(read_root / "missing.mph"), suffixes=(".mph",))
    with pytest.raises(ValueError, match="unsupported file extension"):
        policy.validate_model_read(str(wrong_suffix), suffixes=(".mph",))
    with pytest.raises(ValueError, match="regular file"):
        policy.validate_model_read(str(directory), suffixes=(".mph",))


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


def test_ascii_temp_directory_skips_unusable_candidate(ascii_tmp_path):
    blocked = ascii_tmp_path / "not-a-directory"
    blocked.write_text("blocked", encoding="utf-8")
    fallback = ascii_tmp_path / "fallback"

    created = _create_ascii_temp_dir(candidates=(blocked, fallback))

    assert created.parent == fallback
    assert str(created).isascii()


def test_evidence_artifact_roots_are_existing_owned_directories(tmp_path, ascii_root):
    policy, _, write_root = _policy(tmp_path, ascii_root)
    evidence_root = write_root / "formal-evidence"
    evidence_root.mkdir(parents=True)
    outside = ascii_root / "outside-evidence"
    outside.mkdir()

    accepted = policy.validate_artifact_read_root(str(evidence_root))

    assert accepted.kind == "artifact_read_root"
    assert accepted.normalized_path == evidence_root.resolve()
    with pytest.raises(ValueError, match="escapes"):
        policy.validate_artifact_read_root(str(outside))
    with pytest.raises(ValueError, match="cannot be resolved"):
        policy.validate_artifact_read_root(str(write_root / "missing"))


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


@pytest.mark.parametrize(
    "tool_name,parameter,suffix",
    [
        ("wave_optics_preflight", "expected_source_path", ".mph"),
        ("wave_optics_point_audit", "air_reference_artifact_path", ".json"),
    ],
)
def test_wave_optics_read_arguments_are_enforced_by_recommended_profiles(
    tmp_path, ascii_root, monkeypatch, tool_name, parameter, suffix
):
    _policy_value, read_root, write_root = _policy(tmp_path, ascii_root)
    root = read_root if suffix == ".mph" else write_root
    source = root / f"source{suffix}"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"fixture")
    outside = tmp_path / f"outside{suffix}"
    outside.write_bytes(b"outside")
    monkeypatch.setenv(MODEL_READ_ROOTS_ENV, str(read_root))
    monkeypatch.setenv(ARTIFACT_WRITE_ROOT_ENV, str(write_root))
    calls = []

    def tool(**kwargs):
        calls.append(kwargs[parameter])
        return {"success": True}

    tool.__signature__ = __import__("inspect").Signature(
        [__import__("inspect").Parameter(parameter, __import__("inspect").Parameter.KEYWORD_ONLY)]
    )
    guarded = guard_tool_call(
        tool,
        tool_name=tool_name,
        side_effect_class="filesystem_read",
        concurrency_class="solver_free",
        profile_name="wave_optics",
    )

    accepted = guarded(**{parameter: str(source)})
    rejected = guarded(**{parameter: str(outside)})

    assert accepted["success"] is True
    assert accepted["path_policy"]["validated_input_count"] == 1
    assert calls == [str(source.resolve())]
    assert rejected["success"] is False
    assert rejected["path_policy"]["accepted"] is False


def test_guarded_model_read_pins_file_and_ancestors_until_consumer_returns(
    tmp_path, ascii_root, monkeypatch
):
    _policy_value, read_root, write_root = _policy(tmp_path, ascii_root)
    source = read_root / "source.mph"
    source.write_bytes(b"validated")
    replacement = tmp_path / "replacement.mph"
    replacement.write_bytes(b"replacement")
    moved_root = tmp_path / "moved-models"
    monkeypatch.setenv(MODEL_READ_ROOTS_ENV, str(read_root))
    monkeypatch.setenv(ARTIFACT_WRITE_ROOT_ENV, str(write_root))

    def model_load(file_path: str):
        with pytest.raises(OSError):
            os.replace(replacement, file_path)
        with pytest.raises(OSError):
            read_root.rename(moved_root)
        return {"success": True, "bytes": Path(file_path).read_bytes()}

    guarded = guard_tool_call(
        model_load,
        tool_name="model_load",
        side_effect_class="filesystem_read_model_mutation",
        concurrency_class="solver_free",
        profile_name="core",
    )

    result = guarded(str(source))

    assert result["success"] is True
    assert result["bytes"] == b"validated"
    os.replace(replacement, source)
    read_root.rename(moved_root)


def test_read_pin_rejects_replacement_between_validation_and_acquisition(tmp_path, ascii_root):
    policy, read_root, _write_root = _policy(tmp_path, ascii_root)
    source = read_root / "source.mph"
    source.write_bytes(b"validated")
    decision = policy.validate_model_read(str(source), suffixes=(".mph",))
    assert decision.read_pin is not None
    replacement = tmp_path / "replacement.mph"
    replacement.write_bytes(b"replacement")
    os.replace(replacement, source)

    with pytest.raises(ReadPinError, match="identity changed"):
        with pin_validated_reads((decision.read_pin,)):
            pytest.fail("replacement identity must not reach the consumer")


def test_guarded_write_pins_ancestors_but_allows_file_creation(tmp_path, ascii_root, monkeypatch):
    _policy_value, read_root, write_root = _policy(tmp_path, ascii_root)
    target = write_root / "saved.mph"
    moved_root = ascii_root / "moved-artifacts"
    monkeypatch.setenv(MODEL_READ_ROOTS_ENV, str(read_root))
    monkeypatch.setenv(ARTIFACT_WRITE_ROOT_ENV, str(write_root))

    def model_save(file_path: str):
        with pytest.raises(OSError):
            write_root.rename(moved_root)
        Path(file_path).write_bytes(b"staged-write")
        return {"success": True}

    guarded = guard_tool_call(
        model_save,
        tool_name="model_save",
        side_effect_class="filesystem_write",
        concurrency_class="solver_free",
        profile_name="core",
    )

    result = guarded(file_path=str(target))

    assert result["success"] is True
    assert target.read_bytes() == b"staged-write"
    target.unlink()
    write_root.rename(moved_root)


def test_write_pin_rejects_ancestor_replacement_before_acquisition(tmp_path, ascii_root):
    policy, _read_root, write_root = _policy(tmp_path, ascii_root)
    decision = policy.validate_artifact_write(str(write_root / "saved.mph"))
    assert decision.write_pin is not None
    moved_root = ascii_root / "moved-artifacts"
    write_root.rename(moved_root)
    write_root.mkdir()

    with pytest.raises(ReadPinError, match="identity changed"):
        with pin_validated_writes((decision.write_pin,)):
            pytest.fail("replacement ancestor must not reach the writer")


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


def test_shared_model_lock_wrapper_normalizes_immutable_source(tmp_path, ascii_root, monkeypatch):
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
        PathPolicy.from_environment(
            {
                MODEL_READ_ROOTS_ENV: str(linked_root),
                ARTIFACT_WRITE_ROOT_ENV: str(ascii_root / "artifacts"),
            }
        )
