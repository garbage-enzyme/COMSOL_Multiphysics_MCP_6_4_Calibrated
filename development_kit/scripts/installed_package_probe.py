"""Verify discovery from a non-editable installed wheel without starting COMSOL."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
from importlib.metadata import entry_points, requires, version
from importlib.resources import files
from importlib.util import find_spec
from pathlib import Path

HEAVY_SEMANTIC_MODULES = {"chromadb", "sentence_transformers", "torch"}
SETTINGS_GUI_ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
FORBIDDEN_PROCESS_NAMES = frozenset(
    {
        "comsol-mcp.exe",
        "comsol-mcp-settings.exe",
        "comsol-mcp-settings-gui.exe",
        "comsol.exe",
        "comsolmphserver.exe",
        "java.exe",
        "javaw.exe",
    }
)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _ico_sizes(raw: bytes) -> tuple[int, ...]:
    if len(raw) < 6:
        raise AssertionError("installed Settings GUI icon is truncated")
    reserved, kind, count = struct.unpack_from("<HHH", raw)
    if reserved != 0 or kind != 1 or len(raw) < 6 + 16 * count:
        raise AssertionError("installed Settings GUI icon header is invalid")
    sizes = []
    for index in range(count):
        width, height = struct.unpack_from("<BB", raw, 6 + 16 * index)
        width, height = width or 256, height or 256
        if width != height:
            raise AssertionError("installed Settings GUI icon contains a non-square frame")
        sizes.append(width)
    return tuple(sizes)


def _windows_pe_subsystem(path: Path) -> int:
    """Read the PE optional-header subsystem without executing the launcher."""
    with path.open("rb") as stream:
        dos_header = stream.read(64)
        if len(dos_header) != 64 or dos_header[:2] != b"MZ":
            raise AssertionError("installed entry point is not a Windows PE executable")
        pe_offset = struct.unpack_from("<I", dos_header, 0x3C)[0]
        if pe_offset < 64 or pe_offset > 16 * 1024 * 1024:
            raise AssertionError("installed entry point has an invalid PE header offset")
        stream.seek(pe_offset)
        optional_prefix = stream.read(24 + 70)
    if len(optional_prefix) != 94 or optional_prefix[:4] != b"PE\0\0":
        raise AssertionError("installed entry point has an invalid PE header")
    optional_header = optional_prefix[24:]
    if struct.unpack_from("<H", optional_header)[0] not in {0x10B, 0x20B}:
        raise AssertionError("installed entry point has an unsupported PE optional header")
    return struct.unpack_from("<H", optional_header, 68)[0]


def _release_inventory(capabilities: dict) -> dict:
    return {
        "schema_registry_sha256": capabilities["schema_registry"]["registry_sha256"],
        "schema_entry_count": capabilities["schema_registry"]["entry_count"],
        "catalog_contract_sha256": capabilities["deployment_identity"]["catalog_contract_sha256"],
        "full_tool_schemas_sha256": capabilities["deployment_identity"]["full_tool_schemas_sha256"],
        "profile_tool_names_sha256": capabilities["deployment_identity"][
            "profile_tool_names_sha256"
        ],
        "feature_tool_names_sha256": capabilities["deployment_identity"][
            "feature_tool_names_sha256"
        ],
        "build_identity_sha256": capabilities["deployment_identity"]["build_identity"][
            "build_identity_sha256"
        ],
    }


def _bind_release_inventory(
    baseline: dict | None,
    observed: dict,
    *,
    profile: str,
) -> dict:
    if baseline is not None and observed != baseline:
        raise AssertionError(f"installed {profile} release inventory differs from earlier profiles")
    return observed if baseline is None else baseline


def _consistent_deployment_identity(identities: list[dict]) -> dict:
    if not identities:
        raise AssertionError("installed discovery produced no deployment identities")
    first = identities[0]
    if any(identity != first for identity in identities[1:]):
        raise AssertionError("installed profiles disagree on deployment identity")
    return first


def _shortcut_bytes_identity(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise AssertionError("pre-existing Desktop shortcut is not a bounded regular file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _forbidden_process_snapshot() -> dict[int, str]:
    import psutil

    result: dict[int, str] = {}
    for process in psutil.process_iter(("pid", "name")):
        try:
            name = str(process.info.get("name") or "").casefold()
            if name in FORBIDDEN_PROCESS_NAMES:
                result[int(process.info["pid"])] = name
        except OSError, psutil.Error, TypeError, ValueError:
            continue
    return result


def _probe_direct_settings_entry(output_parent: Path) -> dict:
    from settings_gui.desktop_shortcut import (
        SHORTCUT_NAME,
        installed_entry_executable,
        known_desktop_path,
    )

    executable = installed_entry_executable()
    probe_root = output_parent / "settings-gui-direct-entry-probe"
    probe_root.mkdir(parents=True, exist_ok=False)
    try:
        target = probe_root / "settings.json"
        shortcut = known_desktop_path() / SHORTCUT_NAME
        shortcut_before = _shortcut_bytes_identity(shortcut)
        processes_before = _forbidden_process_snapshot()
        completed = subprocess.run(  # noqa: S603
            [str(executable), "--settings-path", str(target), "--validate-only"],
            cwd=probe_root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            raise AssertionError(
                "installed Settings GUI validate-only entry failed: " + completed.stderr[:512]
            )
        try:
            receipt = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError("installed Settings GUI validate-only output is invalid") from exc
        if (
            receipt.get("ready") is not True
            or receipt.get("settings_path_override") is not True
            or receipt.get("settings_path_included") is not False
            or receipt.get("contains_local_path") is not False
            or receipt.get("tkinter_imported") is not False
            or receipt.get("mcp_started") is not False
            or receipt.get("solver_started") is not False
            or str(target) in completed.stdout
        ):
            raise AssertionError("installed Settings GUI validate-only contract failed")
        if target.exists() or any(probe_root.iterdir()):
            raise AssertionError("validate-only created a settings or temporary file")
        if _shortcut_bytes_identity(shortcut) != shortcut_before:
            raise AssertionError("validate-only changed the Desktop shortcut")
        processes_after = _forbidden_process_snapshot()
        new_processes = sorted(set(processes_after) - set(processes_before))
        if new_processes:
            raise AssertionError("validate-only started a forbidden process")
    finally:
        shutil.rmtree(probe_root)
    return {
        "ready": True,
        "settings_path_override": True,
        "settings_path_included": False,
        "tkinter_imported": False,
        "shortcut_unchanged": True,
        "process_inventory_unchanged": True,
        "mcp_started": False,
        "solver_started": False,
    }


def _probe_owned_shortcut(output_parent: Path) -> dict:
    from settings_gui.desktop_shortcut import (
        SHORTCUT_NAME,
        create_desktop_shortcut,
        inspect_windows_shortcut,
        installed_gui_entry_executable,
        remove_desktop_shortcut,
    )

    desktop = output_parent / "settings-gui-shortcut-probe"
    desktop.mkdir(parents=True, exist_ok=False)
    settings = output_parent / "settings-gui-shortcut-settings.json"
    gui_entry = installed_gui_entry_executable()
    try:
        created = create_desktop_shortcut(settings_path=settings, desktop_path=desktop)
        if created.get("success") is not True or created.get("state") != "created":
            raise AssertionError("installed Settings GUI shortcut creation failed")
        observed = inspect_windows_shortcut(desktop / SHORTCUT_NAME)
        if os.path.normcase(os.path.abspath(observed.target)) != os.path.normcase(
            os.path.abspath(gui_entry)
        ):
            raise AssertionError("installed shortcut does not target the GUI-subsystem entry")
        removed = remove_desktop_shortcut(settings_path=settings, desktop_path=desktop)
        if removed.get("success") is not True or removed.get("state") != "removed":
            raise AssertionError("installed Settings GUI shortcut cleanup failed")
        if any(desktop.iterdir()):
            raise AssertionError("installed Settings GUI shortcut probe left an artifact")
    finally:
        for child in desktop.iterdir():
            child.unlink(missing_ok=True)
        desktop.rmdir()
    return {
        "created": True,
        "target_is_gui_entry": True,
        "removed": True,
        "contains_local_path": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    import mph

    mph.Client = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("installed-package discovery must not start COMSOL")
    )

    import comsol_mcp
    import settings_gui
    from comsol_mcp.server import create_server
    from comsol_mcp.settings import SEMANTIC_ENABLED_ENV
    from comsol_mcp.shared_session.contracts import SHARED_SERVER_FEATURE_ENV
    from comsol_mcp.tools.capabilities import get_capabilities
    from comsol_mcp.tools.catalog import FEATURE_NAMES, PROFILE_NAMES, snapshot_tool_schemas
    from comsol_mcp.tools.profiles import resolve_profile
    from settings_gui.i18n import Translator

    expected_names = _load_json(args.snapshot_dir / "profile_tool_names.json")
    expected_features = _load_json(args.snapshot_dir / "feature_tool_names.json")
    expected_schemas = _load_json(args.snapshot_dir / "full_tool_schemas.json")
    actual_counts: dict[str, int] = {}
    feature_counts: dict[str, int] = {}
    deployment_identities: list[dict] = []
    release_inventories: dict | None = None

    if tuple(expected_names) != PROFILE_NAMES:
        raise AssertionError("installed profile order differs from the frozen snapshot")
    if tuple(expected_features) != FEATURE_NAMES:
        raise AssertionError("installed feature order differs from the frozen snapshot")

    for profile in PROFILE_NAMES:
        selection = resolve_profile(profile, environ={})
        server = create_server(f"installed-{profile}", profile=selection)
        schemas = asyncio.run(snapshot_tool_schemas(server))
        names = expected_names[profile]
        if sorted(schemas) != names:
            raise AssertionError(f"installed {profile} membership differs from snapshot")
        expected_profile_schemas = {name: expected_schemas[name] for name in names}
        if schemas != expected_profile_schemas:
            raise AssertionError(f"installed {profile} schemas differ from snapshot")
        actual_counts[profile] = len(schemas)
        capabilities = get_capabilities(selection)
        deployment_identities.append(capabilities["deployment_identity"])
        release_inventories = _bind_release_inventory(
            release_inventories,
            _release_inventory(capabilities),
            profile=profile,
        )

    feature_environments = {
        "semantic_docs": SEMANTIC_ENABLED_ENV,
        "shared_server": SHARED_SERVER_FEATURE_ENV,
    }
    for feature in FEATURE_NAMES:
        selection = resolve_profile(
            "core",
            environ={feature_environments[feature]: "true"},
        )
        server = create_server(f"installed-feature-{feature}", profile=selection)
        schemas = asyncio.run(snapshot_tool_schemas(server))
        names = sorted(set(expected_names["core"]) | set(expected_features[feature]))
        if sorted(schemas) != names:
            raise AssertionError(f"installed {feature} overlay differs from snapshot")
        if schemas != {name: expected_schemas[name] for name in names}:
            raise AssertionError(f"installed {feature} schemas differ from snapshot")
        feature_counts[feature] = len(expected_features[feature])
        capabilities = get_capabilities(selection)
        if capabilities["enabled_features"] != [feature]:
            raise AssertionError(f"installed {feature} capability provenance differs")
        deployment_identities.append(capabilities["deployment_identity"])
        release_inventories = _bind_release_inventory(
            release_inventories,
            _release_inventory(capabilities),
            profile=f"core+{feature}",
        )

    composed = resolve_profile(
        "full",
        environ={
            SEMANTIC_ENABLED_ENV: "true",
            SHARED_SERVER_FEATURE_ENV: "true",
        },
    )
    composed_schemas = asyncio.run(
        snapshot_tool_schemas(create_server("installed-full-with-features", profile=composed))
    )
    if composed_schemas != expected_schemas:
        raise AssertionError("installed composed feature surface differs from full schema snapshot")

    deployment_identity = _consistent_deployment_identity(deployment_identities)
    if release_inventories is None:
        raise AssertionError("installed discovery produced no release inventory")
    if deployment_identity["source_classification"] != "installed_site_package":
        raise AssertionError("installed deployment identity reports source-tree shadowing")
    if deployment_identity.get("contains_local_path") is not False:
        raise AssertionError("installed deployment identity leaks a local path")

    if find_spec("src") is not None:
        raise AssertionError("installed wheel exposes a generic top-level src package")
    if find_spec("settings_gui.tests") is not None:
        raise AssertionError("installed wheel exposes Settings GUI tests")

    locale_members = {}
    locale_directories = {"en": "en", "zh-cn": "zh_CN", "zh-tw": "zh_TW"}
    for language, locale_directory in locale_directories.items():
        member = files("settings_gui").joinpath(
            "locales",
            locale_directory,
            "LC_MESSAGES",
            "settings_gui.mo",
        )
        raw = member.read_bytes()
        if not raw or Translator(language).warning is not None:
            raise AssertionError(f"installed {language} Settings GUI catalog is unavailable")
        locale_members[language] = len(raw)
    icon_raw = files("settings_gui").joinpath("assets", "comsol_mcp.ico").read_bytes()
    if _ico_sizes(icon_raw) != SETTINGS_GUI_ICON_SIZES or len(icon_raw) >= 128 * 1024:
        raise AssertionError("installed Settings GUI icon contract differs from the source")
    scripts = {item.name: item.value for item in entry_points(group="console_scripts")}
    if scripts.get("comsol-mcp-settings") != "settings_gui.__main__:main":
        raise AssertionError("installed Settings GUI console entry point is unavailable")
    gui_scripts = {item.name: item.value for item in entry_points(group="gui_scripts")}
    if gui_scripts.get("comsol-mcp-settings-gui") != "settings_gui.__main__:main":
        raise AssertionError("installed Settings GUI GUI entry point is unavailable")
    from settings_gui.desktop_shortcut import installed_gui_entry_executable

    gui_entry = installed_gui_entry_executable()
    if _windows_pe_subsystem(gui_entry) != 2:
        raise AssertionError("installed Settings GUI entry does not use the Windows GUI subsystem")
    if "tkinter" in sys.modules:
        raise AssertionError("installed solver-free discovery imported tkinter")
    direct_entry = _probe_direct_settings_entry(args.output.parent)
    shortcut_entry = _probe_owned_shortcut(args.output.parent)

    imported_heavy = sorted(HEAVY_SEMANTIC_MODULES.intersection(sys.modules))
    if imported_heavy:
        raise AssertionError(f"discovery imported heavy semantic modules: {imported_heavy}")

    package_requirements = sorted(requires("comsol-mcp") or [])
    result = {
        "schema_version": "1.1.0",
        "installed_package": {
            "name": "comsol-mcp",
            "version": version("comsol-mcp"),
            "module_path_is_site_package": "site-packages"
            in str(Path(comsol_mcp.__file__).resolve()).lower(),
            "requirements": package_requirements,
        },
        "settings_gui": {
            "release": settings_gui.GUI_RELEASE,
            "console_entry": scripts["comsol-mcp-settings"],
            "gui_entry": gui_scripts["comsol-mcp-settings-gui"],
            "gui_entry_subsystem": "windows_gui",
            "locale_bytes": locale_members,
            "icon_bytes": len(icon_raw),
            "icon_sizes": SETTINGS_GUI_ICON_SIZES,
            "tests_excluded": True,
            "tkinter_imported": False,
            "direct_entry": direct_entry,
            "shortcut_entry": shortcut_entry,
        },
        "profile_counts": actual_counts,
        "feature_counts": feature_counts,
        "deployment_identity": deployment_identity,
        "release_inventories": release_inventories,
        "schema_snapshot_match": True,
        "comsol_client_started": False,
        "heavy_semantic_modules_imported": imported_heavy,
    }
    if not result["installed_package"]["module_path_is_site_package"]:
        raise AssertionError(
            f"probe imported source tree instead of installed wheel: {comsol_mcp.__file__}"
        )

    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
