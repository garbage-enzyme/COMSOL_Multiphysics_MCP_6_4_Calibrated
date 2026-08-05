"""Settings GUI distribution membership tests."""

from __future__ import annotations

import io
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from development_kit.scripts import settings_gui_visual_capture
from development_kit.scripts.settings_gui_package_probe import (
    ICON_MEMBER,
    LANGUAGES,
    ROOT_LAUNCHER_MEMBER,
    SHORTCUT_MEMBER,
    inspect_settings_gui_distributions,
)

ROOT = Path(__file__).parents[2]


def _archives(
    root: Path,
    *,
    include_icon: bool = True,
    include_launcher: bool = True,
    include_wheel_launcher: bool = False,
    include_test: bool = False,
    include_shortcut_adapter: bool = True,
    include_gui_entry: bool = True,
    entry_points_text: str | None = None,
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
        entry_points = entry_points_text
        if entry_points is None:
            entry_points = "[console_scripts]\ncomsol-mcp-settings = settings_gui.__main__:main\n"
        if include_gui_entry and entry_points_text is None:
            entry_points += "[gui_scripts]\ncomsol-mcp-settings-gui = settings_gui.__main__:main\n"
        archive.writestr("comsol_mcp-0.6.0.dist-info/entry_points.txt", entry_points)
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
    assert result["gui_entry_included"] is True
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


def test_distribution_probe_rejects_missing_gui_subsystem_entry(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="GUI entry point"):
        inspect_settings_gui_distributions(_archives(tmp_path, include_gui_entry=False))


@pytest.mark.parametrize(
    "entry_points",
    [
        (
            "[console_scripts]\n"
            "comsol-mcp-settings2 = settings_gui.__main__:main\n"
            "[gui_scripts]\n"
            "comsol-mcp-settings-gui = settings_gui.__main__:main_gui\n"
        ),
        (
            "[console_scripts]\n"
            "comsol-mcp-settings = settings_gui.__main__:main\n"
            "comsol-mcp-settings-gui = settings_gui.__main__:main\n"
        ),
    ],
)
def test_distribution_probe_requires_exact_section_bound_entry_points(
    tmp_path: Path, entry_points: str
) -> None:
    with pytest.raises(ValueError, match="entry point"):
        inspect_settings_gui_distributions(_archives(tmp_path, entry_points_text=entry_points))


def test_visual_capture_direct_script_uses_current_source_tab_contract() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "development_kit" / "scripts" / "settings_gui_visual_capture.py"),
            "--help",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "shared_server" not in completed.stdout
    assert (
        "--tab {general,profile,runtime,comsol_java,evidence,semantic,ownership,about}"
        in completed.stdout
    )


def test_visual_capture_matrix_covers_feature_tabs_and_about_at_every_scale() -> None:
    scenarios = set(settings_gui_visual_capture._capture_scenarios())
    expected = {
        (language, dpi_percent, state, tab)
        for dpi_percent in (100, 125, 150, 200)
        for language in ("en", "zh-cn", "zh-tw")
        for state, tab in (
            ("valid", "general"),
            ("valid", "profile"),
            ("valid", "semantic"),
            ("about", "about"),
        )
    }
    expected.update(
        (language, dpi_percent, state, tab)
        for language in ("en", "zh-cn", "zh-tw")
        for dpi_percent, state, tab in (
            (200, "invalid", "runtime"),
            (200, "long_paths", "comsol_java"),
            (150, "evidence", "evidence"),
        )
    )
    assert scenarios == expected


def test_visual_capture_one_mode_creates_fresh_output_root(tmp_path, monkeypatch) -> None:
    output_root = tmp_path / "fresh" / "capture"

    def fake_capture(output, **_kwargs):
        assert output_root.is_dir()
        return {"file": output.name}

    monkeypatch.setattr(settings_gui_visual_capture, "_capture_one", fake_capture)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "settings_gui_visual_capture",
            "--output-root",
            str(output_root),
            "--one",
            "--language",
            "en",
            "--dpi-percent",
            "100",
            "--state",
            "valid",
            "--tab",
            "general",
        ],
    )

    assert settings_gui_visual_capture.main() == 0


def test_visual_capture_matrix_surfaces_bounded_child_diagnostics(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        settings_gui_visual_capture,
        "_capture_scenarios",
        lambda: (("zh-cn", 200, "invalid", "runtime"),),
    )
    monkeypatch.setattr(
        settings_gui_visual_capture.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=7,
            stdout="out-" + "x" * 3000,
            stderr="err-" + "y" * 3000,
        ),
    )

    with pytest.raises(RuntimeError, match="language=zh-cn, dpi=200") as caught:
        settings_gui_visual_capture.capture_matrix(tmp_path / "matrix")

    assert len(str(caught.value)) < 4300
    assert "err-" not in str(caught.value)
    assert "yyyy" in str(caught.value)
