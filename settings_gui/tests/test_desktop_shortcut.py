"""Owned per-user Desktop shortcut lifecycle tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from settings_gui.desktop_shortcut import (
    ICON_PATH,
    OWNERSHIP_DESCRIPTION,
    SHORTCUT_NAME,
    ShortcutSpec,
    _canonical_icon_location,
    create_desktop_shortcut,
    inspect_windows_shortcut,
    remove_desktop_shortcut,
    shortcut_status,
)


def test_shell_icon_location_spacing_is_canonicalized() -> None:
    assert _canonical_icon_location("C:\\Program Files\\COMSOL MCP\\icon.ico, 0") == (
        "C:\\Program Files\\COMSOL MCP\\icon.ico,0"
    )
    assert _canonical_icon_location("C:\\icons,archive\\icon.ico, -1") == (
        "C:\\icons,archive\\icon.ico,-1"
    )
    assert _canonical_icon_location("invalid icon location") == "invalid icon location"


class FakeShortcutBackend:
    def __init__(self) -> None:
        self.specs: dict[Path, ShortcutSpec] = {}
        self.writes: list[tuple[Path, ShortcutSpec]] = []

    def inspect(self, path: Path) -> ShortcutSpec:
        return self.specs[path]

    def write(self, path: Path, spec: ShortcutSpec) -> None:
        path.write_bytes(b"shortcut")
        self.specs[path] = spec
        self.writes.append((path, spec))


def _paths(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    executable = tmp_path / "Scripts" / "comsol-mcp-settings.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"exe")
    icon = tmp_path / "site-packages" / "settings_gui" / "assets" / "comsol_mcp.ico"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"ico")
    settings = tmp_path / "配置 space" / "settings.json"
    settings.parent.mkdir()
    settings.write_text("{}\n", encoding="utf-8")
    return desktop, executable, icon, settings


def _kwargs(tmp_path: Path, backend: FakeShortcutBackend) -> dict:
    desktop, executable, icon, settings = _paths(tmp_path)
    return {
        "settings_path": settings,
        "desktop_path": desktop,
        "executable": executable,
        "icon_path": icon,
        "inspect_shortcut": backend.inspect,
        "write_shortcut": backend.write,
    }


def test_create_is_explicit_exact_idempotent_and_removable(tmp_path: Path) -> None:
    backend = FakeShortcutBackend()
    kwargs = _kwargs(tmp_path, backend)
    shortcut = kwargs["desktop_path"] / SHORTCUT_NAME

    created = create_desktop_shortcut(**kwargs)
    current = create_desktop_shortcut(**kwargs)
    status = shortcut_status(**kwargs)
    removed = remove_desktop_shortcut(**kwargs)
    absent = remove_desktop_shortcut(**kwargs)

    assert created["success"] is True
    assert created["state"] == "created"
    assert current["state"] == "already_current"
    assert status["state"] == "current"
    assert len(backend.writes) == 1
    written_path, spec = backend.writes[0]
    assert written_path.parent == shortcut.parent
    assert spec.target == kwargs["executable"]
    assert spec.arguments == f'--settings-path "{kwargs["settings_path"]}"'
    assert spec.working_directory == kwargs["executable"].parent
    assert spec.icon_location == f"{kwargs['icon_path']},0"
    assert spec.description == OWNERSHIP_DESCRIPTION
    assert removed["state"] == "removed"
    assert absent["state"] == "not_found"
    assert not shortcut.exists()
    for receipt in (created, current, status, removed, absent):
        assert receipt["contains_local_path"] is False
        assert receipt["settings_path_included"] is False
        assert receipt["mcp_started"] is False
        assert receipt["solver_started"] is False


def test_foreign_shortcut_requires_confirmation_and_remove_preserves_it(tmp_path: Path) -> None:
    backend = FakeShortcutBackend()
    kwargs = _kwargs(tmp_path, backend)
    shortcut = kwargs["desktop_path"] / SHORTCUT_NAME
    shortcut.write_bytes(b"foreign")
    backend.specs[shortcut] = ShortcutSpec(
        target=Path("C:/foreign.exe"),
        arguments="--foreign",
        working_directory=Path("C:/"),
        icon_location="C:/foreign.ico,0",
        description="Foreign shortcut",
    )

    declined = create_desktop_shortcut(**kwargs)
    preserved = remove_desktop_shortcut(**kwargs)

    assert declined["success"] is False
    assert declined["state"] == "confirmation_required"
    assert declined["existing_kind"] == "foreign"
    assert preserved["state"] == "foreign_preserved"
    assert shortcut.read_bytes() == b"foreign"
    assert backend.writes == []

    replaced = create_desktop_shortcut(**kwargs, replace_existing=True)
    assert replaced["success"] is True
    assert replaced["state"] == "replaced"
    assert backend.inspect(shortcut).description == OWNERSHIP_DESCRIPTION


def test_stale_owned_shortcut_is_reported_and_never_silently_retargeted(tmp_path: Path) -> None:
    backend = FakeShortcutBackend()
    kwargs = _kwargs(tmp_path, backend)
    shortcut = kwargs["desktop_path"] / SHORTCUT_NAME
    shortcut.write_bytes(b"stale")
    backend.specs[shortcut] = ShortcutSpec(
        target=kwargs["executable"],
        arguments='--settings-path "C:\\old\\settings.json"',
        working_directory=kwargs["executable"].parent,
        icon_location=f"{kwargs['icon_path']},0",
        description=OWNERSHIP_DESCRIPTION,
    )

    status = shortcut_status(**kwargs)
    create = create_desktop_shortcut(**kwargs)

    assert status["state"] == "stale"
    assert create["state"] == "confirmation_required"
    assert create["existing_kind"] == "owned_stale"
    assert shortcut.read_bytes() == b"stale"
    assert backend.writes == []


def test_replacement_refuses_a_shortcut_changed_during_inspection(tmp_path: Path) -> None:
    backend = FakeShortcutBackend()
    kwargs = _kwargs(tmp_path, backend)
    shortcut = kwargs["desktop_path"] / SHORTCUT_NAME
    shortcut.write_bytes(b"stale")
    stale = ShortcutSpec(
        target=kwargs["executable"],
        arguments='--settings-path "C:\\old\\settings.json"',
        working_directory=kwargs["executable"].parent,
        icon_location=f"{kwargs['icon_path']},0",
        description=OWNERSHIP_DESCRIPTION,
    )

    def racing_inspect(_path: Path) -> ShortcutSpec:
        shortcut.write_bytes(b"foreign replacement")
        return stale

    kwargs["inspect_shortcut"] = racing_inspect
    result = create_desktop_shortcut(**kwargs, replace_existing=True)

    assert result["state"] == "conflict"
    assert shortcut.read_bytes() == b"foreign replacement"
    assert backend.writes == []


@pytest.mark.skipif(os.name != "nt", reason="Windows Shell Link acceptance")
def test_real_windows_shell_link_round_trip_is_exact_and_cleaned(tmp_path: Path) -> None:
    desktop = tmp_path / "isolated Desktop"
    desktop.mkdir()
    settings = tmp_path / "用户 settings" / "settings.json"
    settings.parent.mkdir()
    settings.write_text("{}\n", encoding="utf-8")

    created = create_desktop_shortcut(
        settings_path=settings,
        desktop_path=desktop,
        executable=Path(sys.executable),
        icon_path=ICON_PATH,
    )
    shortcut = desktop / SHORTCUT_NAME
    spec = inspect_windows_shortcut(shortcut)

    assert created["state"] == "created"
    assert spec.target == Path(sys.executable)
    assert spec.arguments == f'--settings-path "{settings}"'
    assert spec.description == OWNERSHIP_DESCRIPTION

    removed = remove_desktop_shortcut(
        settings_path=settings,
        desktop_path=desktop,
        executable=Path(sys.executable),
        icon_path=ICON_PATH,
    )
    assert removed["state"] == "removed"
    assert not shortcut.exists()
