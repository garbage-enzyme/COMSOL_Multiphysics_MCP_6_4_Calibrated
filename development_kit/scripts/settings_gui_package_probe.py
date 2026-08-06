"""Verify Settings GUI wheel and source-distribution membership."""

from __future__ import annotations

import argparse
import configparser
import json
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

from packaging.utils import canonicalize_name, parse_wheel_filename

LANGUAGES = ("en", "zh_CN", "zh_TW")
ICON_MEMBER = "settings_gui/assets/comsol_mcp.ico"
ROOT_LAUNCHER_MEMBER = "Open_Settings_GUI.ps1"
SHORTCUT_MEMBER = "settings_gui/desktop_shortcut.py"


def _wheel_members(path: Path) -> tuple[set[str], str]:
    with zipfile.ZipFile(path) as archive:
        names = {name.replace("\\", "/") for name in archive.namelist()}
        entry_points = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        if len(entry_points) != 1:
            raise ValueError("wheel must contain one entry_points.txt")
        return names, archive.read(entry_points[0]).decode("utf-8")


def _sdist_members(path: Path) -> set[str]:
    with tarfile.open(path, "r:gz") as archive:
        names = {member.name.replace("\\", "/") for member in archive.getmembers()}
    roots = {PurePosixPath(name).parts[0] for name in names if PurePosixPath(name).parts}
    if len(roots) != 1:
        raise ValueError("sdist must contain one archive root")
    root = next(iter(roots))
    return {name.removeprefix(root + "/") for name in names}


def _entry_point_target(value: str, section: str, name: str) -> str | None:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read_string(value)
    except configparser.Error as exc:
        raise ValueError("wheel entry_points.txt is invalid") from exc
    if not parser.has_section(section):
        return None
    return parser.get(section, name, fallback=None)


def inspect_settings_gui_distributions(dist: Path) -> dict:
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("expected exactly one wheel and one source distribution")
    wheel, sdist = wheels[0], sdists[0]
    wheel_name, wheel_version, _build, _tags = parse_wheel_filename(wheel.name)
    sdist_root = sdist.name.removesuffix(".tar.gz")
    sdist_parts = sdist_root.rsplit("-", 1)
    if (
        len(sdist_parts) != 2
        or canonicalize_name(sdist_parts[0]) != canonicalize_name(str(wheel_name))
        or sdist_parts[1] != str(wheel_version)
    ):
        raise ValueError("wheel and source distribution package identities differ")
    wheel_names, entry_points = _wheel_members(wheel)
    sdist_names = _sdist_members(sdist)

    expected_mo = {
        f"settings_gui/locales/{language}/LC_MESSAGES/settings_gui.mo" for language in LANGUAGES
    }
    expected_po = {
        f"settings_gui/locales/{language}/LC_MESSAGES/settings_gui.po" for language in LANGUAGES
    }
    if not expected_mo <= wheel_names or not expected_mo <= sdist_names:
        raise ValueError("distribution is missing a compiled Settings GUI catalog")
    if not expected_po <= sdist_names or "settings_gui/locales/settings_gui.pot" not in sdist_names:
        raise ValueError("source distribution is missing translator catalogs")
    if ICON_MEMBER not in wheel_names or ICON_MEMBER not in sdist_names:
        raise ValueError("distribution is missing the Settings GUI application icon")
    if SHORTCUT_MEMBER not in wheel_names or SHORTCUT_MEMBER not in sdist_names:
        raise ValueError("distribution is missing the Settings GUI shortcut adapter")
    if ROOT_LAUNCHER_MEMBER not in sdist_names:
        raise ValueError("source distribution is missing the root launcher")
    if ROOT_LAUNCHER_MEMBER in wheel_names:
        raise ValueError("wheel contains the repository root launcher")
    if any(
        name.startswith("settings_gui/assets/") and name.casefold().endswith(".png")
        for name in wheel_names | sdist_names
    ):
        raise ValueError("distribution contains the private source logo")
    if any(name.startswith("settings_gui/tests/") for name in wheel_names):
        raise ValueError("wheel contains Settings GUI tests")
    if any(name.endswith((".po", ".pot")) for name in wheel_names):
        raise ValueError("wheel contains translator source catalogs")
    if (
        _entry_point_target(entry_points, "console_scripts", "comsol-mcp-settings")
        != "settings_gui.__main__:main"
    ):
        raise ValueError("wheel is missing the Settings GUI console entry point")
    if (
        _entry_point_target(entry_points, "gui_scripts", "comsol-mcp-settings-gui")
        != "settings_gui.__main__:main"
    ):
        raise ValueError("wheel is missing the Settings GUI GUI entry point")
    return {
        "schema_name": "comsol_mcp.settings_gui_package_receipt",
        "schema_version": "1.0.0",
        "wheel_locale_count": len(
            {name for name in wheel_names if name.endswith("/settings_gui.mo")}
        ),
        "sdist_po_count": len({name for name in sdist_names if name.endswith("/settings_gui.po")}),
        "package_name": str(wheel_name),
        "package_version": str(wheel_version),
        "sdist_pot_included": True,
        "wheel_tests_excluded": True,
        "wheel_translation_sources_excluded": True,
        "console_entry_included": True,
        "gui_entry_included": True,
        "wheel_icon_included": True,
        "sdist_icon_included": True,
        "shortcut_adapter_included": True,
        "source_logo_excluded": True,
        "sdist_root_launcher_included": True,
        "wheel_root_launcher_excluded": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = inspect_settings_gui_distributions(args.dist)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
