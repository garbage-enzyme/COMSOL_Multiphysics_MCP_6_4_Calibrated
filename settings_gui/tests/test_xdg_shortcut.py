"""XDG launcher lifecycle, actual parser validation and foreign-file protection."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from settings_gui.desktop_shortcut import decode_settings_path_token
from settings_gui.xdg_shortcut import (
    NAME,
    create_desktop_shortcut,
    remove_desktop_shortcut,
    shortcut_status,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="XDG launcher contract")


@pytest.fixture
def inputs(tmp_path):
    executable = tmp_path / 'entry space$"`back\\slash'
    output = tmp_path / "argv.json"
    executable.write_text(
        f"#!{sys.executable}\nimport json,sys\n"
        f"open({str(output)!r},'w').write(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    settings = tmp_path / "设置 space$.json"
    settings.write_bytes(b"{}")
    return dict(
        settings_path=settings, executable=executable, desktop_path=tmp_path / "applications"
    )


def test_owned_launcher_roundtrip_and_idempotence(inputs):
    assert shortcut_status(**inputs)["state"] == "not_found"
    assert create_desktop_shortcut(**inputs)["success"] is True
    assert shortcut_status(**inputs)["state"] == "current"
    path = inputs["desktop_path"] / NAME
    original = path.read_bytes()
    assert create_desktop_shortcut(**inputs)["state"] == "already_current"
    assert path.read_bytes() == original
    assert remove_desktop_shortcut(**inputs)["state"] == "removed"
    assert not path.exists()
    assert remove_desktop_shortcut(**inputs)["state"] == "not_found"
    assert inputs["settings_path"].read_bytes() == b"{}"


def test_freedesktop_validator_and_actual_gio_argv(inputs):
    assert create_desktop_shortcut(**inputs)["success"] is True
    path = inputs["desktop_path"] / NAME
    validator = shutil.which("desktop-file-validate")
    gio = shutil.which("gio")
    assert validator and gio, "Linux GUI gate requires desktop-file-utils and GLib gio"
    validated = subprocess.run([validator, str(path)], capture_output=True, text=True, timeout=10)
    assert validated.returncode == 0, validated.stdout + validated.stderr
    launch = subprocess.run([gio, "launch", str(path)], capture_output=True, text=True, timeout=10)
    assert launch.returncode == 0, launch.stdout + launch.stderr
    output = inputs["executable"].parent / "argv.json"
    import time

    deadline = time.monotonic() + 5
    while not output.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    argv = json.loads(output.read_text(encoding="utf-8"))
    assert len(argv) == 2
    assert argv[0] == "--settings-path-token"
    assert decode_settings_path_token(argv[1]) == str(inputs["settings_path"])


@pytest.mark.parametrize(
    "foreign", [b"caller launcher", b"[Desktop Entry]\nX-COMSOL-MCP-Owner=owned-launcher-v1\n"]
)
def test_foreign_or_modified_launcher_is_never_replaced_or_removed(inputs, foreign):
    parent = inputs["desktop_path"]
    parent.mkdir()
    path = parent / NAME
    path.write_bytes(foreign)
    assert create_desktop_shortcut(**inputs, replace_existing=True)["success"] is False
    assert remove_desktop_shortcut(**inputs)["success"] is False
    assert path.read_bytes() == foreign


def test_owned_stale_launcher_requires_explicit_replacement(inputs):
    assert create_desktop_shortcut(**inputs)["success"] is True
    other = inputs["settings_path"].with_name("other.json")
    other.write_bytes(b"{}")
    changed = dict(inputs, settings_path=other)
    assert shortcut_status(**changed)["state"] == "stale"
    assert create_desktop_shortcut(**changed)["success"] is False
    assert create_desktop_shortcut(**changed, replace_existing=True)["success"] is True
    assert shortcut_status(**changed)["state"] == "current"


def test_symlink_launcher_preserves_external_target(inputs):
    inputs["desktop_path"].mkdir()
    external = inputs["settings_path"].with_name("foreign.desktop")
    external.write_bytes(b"caller")
    (inputs["desktop_path"] / NAME).symlink_to(external)
    with pytest.raises(ValueError, match="non-link"):
        create_desktop_shortcut(**inputs, replace_existing=True)
    assert external.read_bytes() == b"caller"


def test_replacement_collision_does_not_overwrite_new_foreign_file(inputs, monkeypatch):
    import settings_gui.xdg_shortcut as module

    assert create_desktop_shortcut(**inputs)["success"] is True
    other = inputs["settings_path"].with_name("other.json")
    other.write_bytes(b"{}")
    path = inputs["desktop_path"] / NAME
    original = module.unlink_if_content

    def race(target, expected):
        removed = original(target, expected)
        if removed:
            path.write_bytes(b"new caller launcher")
        return removed

    monkeypatch.setattr(module, "unlink_if_content", race)
    result = create_desktop_shortcut(**dict(inputs, settings_path=other), replace_existing=True)
    assert result["success"] is False
    assert path.read_bytes() == b"new caller launcher"
    assert not list(path.parent.glob(f".{NAME}.*")) or list(path.parent.glob(f".{NAME}.*")) == [
        path.with_name(f".{NAME}.lock")
    ]


def test_owned_launcher_is_restored_after_publication_failure(inputs, monkeypatch):
    import settings_gui.xdg_shortcut as module

    assert create_desktop_shortcut(**inputs)["success"] is True
    path = inputs["desktop_path"] / NAME
    baseline = path.read_bytes()
    other = inputs["settings_path"].with_name("other.json")
    other.write_bytes(b"{}")
    original = module.os.link
    failed = False

    def fail_once(source, destination, **kwargs):
        nonlocal failed
        if not failed and Path(destination) == path:
            failed = True
            raise OSError("injected publication failure")
        return original(source, destination, **kwargs)

    monkeypatch.setattr(module.os, "link", fail_once)
    receipt = create_desktop_shortcut(**dict(inputs, settings_path=other), replace_existing=True)
    assert receipt["success"] is False
    assert receipt["owned_launcher_restored"] is True
    assert path.read_bytes() == baseline
