"""POSIX settings, advisory locking and replacement conflict regressions."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from comsol_mcp.posix_lock import LockBusy, PosixFileLock
from comsol_mcp.settings import (
    default_settings_document,
    normalize_settings_document,
    resolve_settings_location,
)
from comsol_mcp.xdg_paths import project_path

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX advisory lock and XDG contract")


def test_xdg_roots_are_user_scoped_and_absolute(tmp_path):
    env = {"HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state")}
    default = default_settings_document(environ=env)
    assert default["runtime"]["directory"] == str(tmp_path / "state/comsol-mcp/runtime")
    assert default["paths"]["artifact_write_root"] == str(
        tmp_path / ".local/share/comsol-mcp/artifacts"
    )
    assert default["paths"]["model_read_roots"] == [
        str(tmp_path / ".local/share/comsol-mcp/models")
    ]
    assert "%PROGRAMDATA%" not in str(default)
    with pytest.raises(ValueError, match="absolute"):
        project_path("config", {"HOME": str(tmp_path), "XDG_CONFIG_HOME": "relative"})


def test_template_defaults_normalize_to_native_roots(tmp_path):
    import json

    template = Path(__file__).resolve().parents[2] / "settings.json"
    raw = json.loads(template.read_text(encoding="utf-8"))
    report = normalize_settings_document(raw, environ={"HOME": str(tmp_path)})
    assert not report["errors"]
    normalized = report["settings"]
    assert normalized["runtime"]["directory"] == str(tmp_path / ".local/state/comsol-mcp/runtime")
    assert Path(normalized["paths"]["artifact_write_root"]).is_absolute()
    assert "%" not in normalized["runtime"]["directory"]


def test_default_settings_locator_uses_xdg_config(tmp_path):
    bundled = tmp_path / "bundled.json"
    bundled.write_text("{}", encoding="utf-8")
    location = resolve_settings_location(
        {"HOME": str(tmp_path)},
        source_settings_path=tmp_path / "absent.json",
        bundled_settings_path=bundled,
    )
    assert location.writable_path == tmp_path / ".config/comsol-mcp/settings.json"
    assert location.source == "bundled_template"


def test_lock_excludes_other_process_and_reopens_after_close(tmp_path):
    lock_path = tmp_path / "ownership.lock"
    lock = PosixFileLock(lock_path)
    lock.acquire()
    second = PosixFileLock(lock_path)
    with pytest.raises(LockBusy):
        second.acquire()
    script = """from pathlib import Path
from comsol_mcp.posix_lock import PosixFileLock, LockBusy
import sys
lock = PosixFileLock(Path(sys.argv[1]))
try:
    lock.acquire()
except LockBusy:
    print("busy")
"""
    # Use a fresh interpreter so exclusion cannot depend on in-process state.
    child = subprocess.run(
        [sys.executable, "-c", script, str(lock_path)], capture_output=True, text=True, timeout=10
    )
    try:
        assert child.returncode == 0, child.stderr
        assert child.stdout.strip() == "busy"
    finally:
        lock.close()
    assert lock_path.exists()  # Stable synchronization inode, not an active lease.
    second.acquire()
    second.close()


def test_lock_refuses_symlink_and_foreign_mode(tmp_path):
    target = tmp_path / "foreign"
    target.write_bytes(b"caller")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(RuntimeError, match="symlink"):
        PosixFileLock(link).acquire()
    assert target.read_bytes() == b"caller"
    os.chmod(target, 0o644)
    with pytest.raises(RuntimeError, match="private owned"):
        PosixFileLock(target).acquire()
    assert target.read_bytes() == b"caller"


def test_settings_save_detects_noncooperating_replacement(tmp_path):
    from settings_gui.storage import SettingsStore
    from settings_gui.windows_lock import SettingsConflict

    target = tmp_path / "settings.json"
    with SettingsStore(target) as store:
        document = default_settings_document(user_root=tmp_path, program_root=tmp_path)
        store.save(document)
        replacement = tmp_path / "external.json"
        replacement.write_bytes(b'{"caller":"external"}')
        os.replace(replacement, target)
        with pytest.raises(SettingsConflict, match="changed outside"):
            store.save(document)
        assert target.read_bytes() == b'{"caller":"external"}'


@pytest.mark.parametrize("mutation", ["replace", "in_place", "ancestor"])
def test_read_pin_detects_changes_after_consumer(tmp_path, mutation):
    from comsol_mcp.path_policy import ReadPinError, pin_validated_reads, validated_read_pin

    root = tmp_path / "root"
    parent = root / "inputs"
    parent.mkdir(parents=True)
    target = parent / "input.json"
    target.write_bytes(b"original")
    pin = validated_read_pin(target, root)
    with pytest.raises(ReadPinError, match="changed"):
        with pin_validated_reads((pin,)):
            if mutation == "replace":
                replacement = parent / "new.json"
                replacement.write_bytes(b"external")
                os.replace(replacement, target)
            elif mutation == "in_place":
                target.write_bytes(b"external")
            else:
                parent.rename(root / "old")
                parent.mkdir()
                target.write_bytes(b"external")
    assert target.read_bytes() == b"external"


def test_write_pin_detects_ancestor_replacement(tmp_path):
    from comsol_mcp.path_policy import ReadPinError, ValidatedWritePin, pin_validated_writes

    parent = tmp_path / "artifacts"
    parent.mkdir()

    def identity(path):
        return path.stat().st_dev, path.stat().st_ino

    pin = ValidatedWritePin(
        parent / "result.json", tmp_path, parent, identity(tmp_path), identity(parent)
    )
    with pytest.raises(ReadPinError, match="changed"):
        with pin_validated_writes((pin,)):
            parent.rename(tmp_path / "old")
            parent.mkdir()


def test_conditional_cleanup_restores_raced_replacement(tmp_path):
    from comsol_mcp.durable.io import _posix_unlink_opened_file_if

    target = tmp_path / "owned"
    target.write_bytes(b"owned")

    def race(_descriptor, _stat):
        external = tmp_path / "external"
        external.write_bytes(b"caller")
        os.replace(external, target)
        return True

    assert not _posix_unlink_opened_file_if(target, race)
    assert target.read_bytes() == b"caller"
    assert not list(tmp_path.glob(".unlink-*"))


def test_conditional_cleanup_restores_after_predicate_error(tmp_path):
    from comsol_mcp.durable.io import _posix_unlink_opened_file_if

    target = tmp_path / "owned"
    target.write_bytes(b"owned")
    calls = 0

    def predicate(_descriptor, _stat):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("validation failed")
        return True

    with pytest.raises(ValueError, match="validation failed"):
        _posix_unlink_opened_file_if(target, predicate)
    assert target.read_bytes() == b"owned"
    assert not list(tmp_path.glob(".unlink-*"))


def test_invalid_xdg_environment_is_reported_without_raising(tmp_path):
    from comsol_mcp.settings import (
        SETTINGS_PATH_ENV,
        load_settings_report,
        normalize_settings_document,
    )

    environment = {"XDG_STATE_HOME": "relative", "XDG_DATA_HOME": str(tmp_path)}
    report = normalize_settings_document({}, environ=environment)
    assert any(error["path"] == "settings.environment" for error in report["errors"])
    path = tmp_path / "settings.json"
    path.write_text("{}", encoding="utf-8")
    report = load_settings_report({**environment, SETTINGS_PATH_ENV: str(path)})
    assert report["errors"]
    assert not (tmp_path / "comsol-mcp").exists()
