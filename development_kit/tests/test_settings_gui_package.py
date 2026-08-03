"""Settings GUI distribution membership tests."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from development_kit.scripts.settings_gui_package_probe import (
    ICON_MEMBER,
    LANGUAGES,
    ROOT_LAUNCHER_MEMBER,
    SHORTCUT_MEMBER,
    inspect_settings_gui_distributions,
)


def _archives(
    root: Path,
    *,
    include_icon: bool = True,
    include_launcher: bool = True,
    include_wheel_launcher: bool = False,
    include_test: bool = False,
    include_shortcut_adapter: bool = True,
) -> Path:
    dist = root / "dist"
    dist.mkdir()
    wheel = dist / "comsol_mcp-0.6.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("settings_gui/__init__.py", "")
        for language in LANGUAGES:
            archive.writestr(
                f"settings_gui/locales/{language}/LC_MESSAGES/settings_gui.mo",
                b"mo",
            )
        archive.writestr(
            "comsol_mcp-0.6.0.dist-info/entry_points.txt",
            "[console_scripts]\ncomsol-mcp-settings = settings_gui.__main__:main\n",
        )
        if include_icon:
            archive.writestr(ICON_MEMBER, b"ico")
        if include_shortcut_adapter:
            archive.writestr(SHORTCUT_MEMBER, b"adapter")
        if include_wheel_launcher:
            archive.writestr(ROOT_LAUNCHER_MEMBER, b"launcher")
        if include_test:
            archive.writestr("settings_gui/tests/test_app.py", "")
    sdist = dist / "comsol_mcp-0.6.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        members = {"settings_gui/locales/settings_gui.pot": b"pot"}
        if include_shortcut_adapter:
            members[SHORTCUT_MEMBER] = b"adapter"
        for language in LANGUAGES:
            base = f"settings_gui/locales/{language}/LC_MESSAGES/settings_gui"
            members[f"{base}.po"] = b"po"
            members[f"{base}.mo"] = b"mo"
        if include_icon:
            members[ICON_MEMBER] = b"ico"
        if include_launcher:
            members[ROOT_LAUNCHER_MEMBER] = b"launcher"
        for name, raw in members.items():
            info = tarfile.TarInfo(f"comsol_mcp-0.6.0/{name}")
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
    return dist


def test_distribution_probe_accepts_exact_gui_membership(tmp_path: Path) -> None:
    result = inspect_settings_gui_distributions(_archives(tmp_path))

    assert result["wheel_locale_count"] == 3
    assert result["sdist_po_count"] == 3
    assert result["console_entry_included"] is True
    assert result["wheel_icon_included"] is True
    assert result["sdist_icon_included"] is True
    assert result["shortcut_adapter_included"] is True
    assert result["source_logo_excluded"] is True
    assert result["sdist_root_launcher_included"] is True
    assert result["wheel_root_launcher_excluded"] is True


def test_distribution_probe_rejects_gui_tests_in_wheel(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="tests"):
        inspect_settings_gui_distributions(_archives(tmp_path, include_test=True))


def test_distribution_probe_rejects_missing_application_icon(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="icon"):
        inspect_settings_gui_distributions(_archives(tmp_path, include_icon=False))


def test_distribution_probe_rejects_missing_root_launcher(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="root launcher"):
        inspect_settings_gui_distributions(_archives(tmp_path, include_launcher=False))


def test_distribution_probe_rejects_root_launcher_in_wheel(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="root launcher"):
        inspect_settings_gui_distributions(_archives(tmp_path, include_wheel_launcher=True))


def test_distribution_probe_rejects_missing_shortcut_adapter(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="shortcut adapter"):
        inspect_settings_gui_distributions(_archives(tmp_path, include_shortcut_adapter=False))
