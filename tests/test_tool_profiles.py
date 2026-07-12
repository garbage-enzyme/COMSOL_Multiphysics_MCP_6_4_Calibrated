"""Static profile selection and registration compatibility tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src.server import create_server, register_all_tools
from src.tools.catalog import PROFILE_NAMES, snapshot_tool_schemas
from src.tools.profiles import DEFAULT_PROFILE, PROFILE_ENV_VAR, resolve_profile


SNAPSHOT_DIR = Path(__file__).parent / "snapshots"


def _tool_names(server) -> list[str]:
    return sorted(tool.name for tool in asyncio.run(server.list_tools()))


def test_default_profile_remains_full(monkeypatch):
    monkeypatch.delenv(PROFILE_ENV_VAR, raising=False)
    server = create_server("default-full-profile-test")

    assert DEFAULT_PROFILE == "full"
    assert len(_tool_names(server)) == 96


def test_invalid_profile_fails_without_fallback():
    with pytest.raises(ValueError, match="Invalid COMSOL_MCP_PROFILE"):
        create_server("invalid-profile-test", profile="not-real")


def test_environment_profile_is_normalized(monkeypatch):
    monkeypatch.setenv(PROFILE_ENV_VAR, " WAVE_OPTICS ")
    selection = resolve_profile()

    assert selection.name == "wave_optics"
    assert selection.source == "environment"
    assert selection.default_used is False

    server = create_server("environment-wave-profile-test")
    assert len(_tool_names(server)) == 44


def test_profile_name_and_schema_snapshots_are_exact():
    expected_names = json.loads(
        (SNAPSHOT_DIR / "profile_tool_names.json").read_text(encoding="utf-8")
    )
    full_schemas = json.loads(
        (SNAPSHOT_DIR / "full_tool_schemas.json").read_text(encoding="utf-8")
    )

    assert tuple(expected_names) == PROFILE_NAMES
    for profile in PROFILE_NAMES:
        server = create_server(f"{profile}-snapshot-test", profile=profile)
        actual_schemas = asyncio.run(snapshot_tool_schemas(server))
        assert sorted(actual_schemas) == expected_names[profile]
        assert actual_schemas == {
            name: full_schemas[name] for name in expected_names[profile]
        }


def test_profile_registration_has_no_cross_server_leakage():
    core = create_server("isolated-core", profile="core")
    full = create_server("isolated-full", profile="full")
    experimental = create_server("isolated-experimental", profile="experimental")

    assert len(_tool_names(core)) == 38
    assert len(_tool_names(full)) == 96
    assert len(_tool_names(experimental)) == 62
    assert _tool_names(core) != _tool_names(experimental)


def test_registered_server_profile_is_immutable():
    server = create_server("immutable-profile", profile="core")

    register_all_tools(server, "core")
    with pytest.raises(ValueError, match="cannot change"):
        register_all_tools(server, "full")


def test_capabilities_are_bound_to_each_server_profile(monkeypatch):
    monkeypatch.setenv(PROFILE_ENV_VAR, "full")
    core = create_server("core-capabilities", profile="core")
    wave = create_server("wave-capabilities", profile="wave_optics")

    core_result = core._tool_manager._tools["capabilities"].fn()
    wave_result = wave._tool_manager._tools["capabilities"].fn()

    assert core_result["active_profile"] == "core"
    assert core_result["tool_count"] == 38
    assert core_result["profile_source"]["source"] == "explicit_argument"
    assert wave_result["active_profile"] == "wave_optics"
    assert wave_result["tool_count"] == 44
