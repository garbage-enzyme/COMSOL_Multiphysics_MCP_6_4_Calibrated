"""Windows ownership and atomic storage tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from comsol_mcp.settings import SETTINGS_VERSION, default_settings_document
from settings_gui import storage as storage_module
from settings_gui.storage import (
    DamagedSettings,
    SettingsStore,
    decode_settings_bytes,
    ensure_settings_parent,
)
from settings_gui.windows_lock import SettingsConflict, SettingsOwnership


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\xff",
        b"[]",
        b'{"x":1,"x":2}',
        b'{"broken":',
        (b"[" * 1200) + (b"]" * 1200),
    ],
)
def test_decode_settings_bytes_rejects_untrusted_documents(raw):
    with pytest.raises(DamagedSettings):
        decode_settings_bytes(raw)


def test_store_saves_exact_canonical_bytes_and_updates_baseline(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps(default_settings_document()), encoding="utf-8")
    document = default_settings_document()
    document["profile"]["name"] = "wave_optics"

    with SettingsStore(target) as store:
        digest = store.save(document)
        assert store.load()["profile"]["name"] == "wave_optics"
        assert store.ownership.baseline is not None
        assert store.ownership.baseline.sha256 == digest

    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob("*.gui-owner"))


def test_named_mutex_rejects_a_second_live_editor(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps(default_settings_document()), encoding="utf-8")

    with SettingsOwnership(target):
        with pytest.raises(SettingsConflict, match="another settings editor"):
            SettingsOwnership(target).acquire()


def test_named_mutex_rejects_a_second_process(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps(default_settings_document()), encoding="utf-8")
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from settings_gui.windows_lock import SettingsOwnership\n"
        "with SettingsOwnership(Path(sys.argv[1])):\n"
        " print('READY', flush=True)\n"
        " sys.stdin.readline()\n"
    )
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", script, str(target)],
        cwd=Path(__file__).parents[2],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "READY"
        with pytest.raises(SettingsConflict, match="another settings editor"):
            SettingsOwnership(target).acquire()
    finally:
        output, errors = process.communicate("done\n", timeout=10)
        assert process.returncode == 0, output + errors


def test_target_handle_denies_external_write(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps(default_settings_document()), encoding="utf-8")

    with SettingsOwnership(target):
        with pytest.raises(OSError):
            target.write_text("changed", encoding="utf-8")


def test_compare_before_write_rejects_changed_bytes(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps(default_settings_document()), encoding="utf-8")

    with SettingsStore(target) as store:
        store.ownership.release_target_handle()
        target.write_text('{"schema_name":"changed"}', encoding="utf-8")
        store.ownership.reacquire_target_handle()
        with pytest.raises(SettingsConflict, match="changed outside"):
            store.save(default_settings_document())


def test_hostile_same_byte_handoff_is_detected(tmp_path, monkeypatch):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps(default_settings_document()), encoding="utf-8")
    real_replace = os.replace

    def hostile_replace(source, destination):
        real_replace(source, destination)
        raw = Path(destination).read_bytes()
        foreign = Path(destination).with_name("foreign-settings.tmp")
        foreign.write_bytes(raw)
        real_replace(foreign, destination)

    monkeypatch.setattr(storage_module.os, "replace", hostile_replace)
    with SettingsStore(target) as store:
        with pytest.raises(SettingsConflict, match="saved settings bytes"):
            store.save(default_settings_document())


def test_transient_sharing_error_uses_bounded_retry(tmp_path, monkeypatch):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps(default_settings_document()), encoding="utf-8")
    real_replace = os.replace
    attempts = []

    def delayed_replace(source, destination):
        attempts.append(True)
        if len(attempts) < 3:
            error = OSError("sharing violation")
            error.winerror = 32
            raise error
        real_replace(source, destination)

    monkeypatch.setattr(storage_module.os, "replace", delayed_replace)
    with SettingsStore(target, sleeper=lambda _seconds: None) as store:
        store.save(default_settings_document())

    assert len(attempts) == 3


def test_sharing_retry_rechecks_external_change(tmp_path, monkeypatch):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps(default_settings_document()), encoding="utf-8")
    original_replace = os.replace
    attempts = []

    def foreign_change_during_retry(source, destination):
        attempts.append(True)
        if len(attempts) == 1:
            Path(destination).write_text('{"foreign":true}', encoding="utf-8")
            error = OSError("sharing violation")
            error.winerror = 32
            raise error
        original_replace(source, destination)

    monkeypatch.setattr(storage_module.os, "replace", foreign_change_during_retry)
    with SettingsStore(target, sleeper=lambda _seconds: None) as store:
        with pytest.raises(SettingsConflict, match="changed outside"):
            store.save(default_settings_document())


def test_post_replace_reacquire_failure_keeps_new_baseline(tmp_path, monkeypatch):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps(default_settings_document()), encoding="utf-8")
    document = default_settings_document()
    document["profile"]["name"] = "wave_optics"

    with SettingsStore(target) as store:
        original_reacquire = store.ownership.reacquire_target_handle
        calls = 0

        def fail_once_after_replace():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise SettingsConflict("transient reacquire failure")
            original_reacquire()

        monkeypatch.setattr(store.ownership, "reacquire_target_handle", fail_once_after_replace)
        with pytest.raises(SettingsConflict, match="transient reacquire"):
            store.save(document)
        assert store.ownership.baseline == storage_module.file_identity(target)
        assert store.load()["profile"]["name"] == "wave_optics"


def test_mutex_creation_failure_cleans_process_registry(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps(default_settings_document()), encoding="utf-8")
    owner = SettingsOwnership(target)

    class FailedKernel:
        @staticmethod
        def CreateMutexW(*_args):
            return 0

    owner._kernel32 = FailedKernel()
    owner._configure_kernel32 = lambda: None

    with pytest.raises(OSError, match="CreateMutexW failed"):
        owner.acquire()
    assert owner._registered is False
    with SettingsOwnership(target):
        pass


def test_rebuild_preserves_one_exact_damaged_copy(tmp_path, monkeypatch):
    program_data = tmp_path / "program-data"
    monkeypatch.setenv("PROGRAMDATA", str(program_data))
    target = tmp_path / "settings.json"
    damaged = b'{"profile":'
    target.write_bytes(damaged)

    with SettingsStore(target) as store:
        store.ownership.release_target_handle()
        digest = store.rebuild()
        assert len(digest) == 64
        backups = list(tmp_path.glob("settings.damaged-*.json"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == damaged
        assert store.load()["schema_version"] == SETTINGS_VERSION
        assert (tmp_path / "models").is_dir()
        program_root = program_data / "comsol_mcp"
        assert (program_root / "runtime").is_dir()
        assert (program_root / "artifacts").is_dir()


def test_non_ascii_parent_is_supported(tmp_path):
    parent = tmp_path / "用户配置"
    parent.mkdir()
    target = parent / "settings.json"
    target.write_text(json.dumps(default_settings_document()), encoding="utf-8")

    with SettingsStore(target) as store:
        store.save(default_settings_document())

    assert target.is_file()


def test_non_ascii_user_root_rebuilds_and_reloads_with_ascii_machine_paths(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "program-data"))
    parent = tmp_path / "用户配置" / "comsol_mcp"
    parent.mkdir(parents=True)
    target = parent / "settings.json"
    target.write_bytes(b'{"profile":')

    with SettingsStore(target) as store:
        store.ownership.release_target_handle()
        store.rebuild()
        document = store.load()

    assert document["paths"]["model_read_roots"] == [str(parent / "models")]
    assert document["runtime"]["directory"].isascii()
    assert document["paths"]["artifact_write_root"].isascii()
    assert Path(document["runtime"]["directory"]).is_dir()
    assert Path(document["paths"]["artifact_write_root"]).is_dir()


def test_linked_parent_is_rejected_before_directory_creation(tmp_path, monkeypatch):
    target = tmp_path / "linked" / "child" / "settings.json"
    monkeypatch.setattr(storage_module, "path_has_linked_component", lambda _path: True)

    with pytest.raises(SettingsConflict, match="parent"):
        ensure_settings_parent(target)

    assert target.parent.exists() is False


def test_dangling_target_link_is_rejected_before_missing_check(tmp_path, monkeypatch):
    target = tmp_path / "settings.json"
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == target)
    monkeypatch.setattr(Path, "exists", lambda _path: False)

    with pytest.raises(SettingsConflict, match="link or junction"):
        storage_module.file_identity(target)


def test_owned_sidecar_link_is_rejected_before_open(tmp_path, monkeypatch):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps(default_settings_document()), encoding="utf-8")
    owner = SettingsOwnership(target)
    original_is_symlink = Path.is_symlink

    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == owner.sidecar or original_is_symlink(path),
    )
    monkeypatch.setattr(os.path, "lexists", lambda path: Path(path) == owner.sidecar)

    with pytest.raises(SettingsConflict, match="sidecar"):
        owner.acquire()
