"""Shared project settings defaults, validation, and error-reporting contracts."""

from __future__ import annotations

import json
from pathlib import Path

from src.settings import (
    SETTINGS_PATH_ENV,
    SETTINGS_SCHEMA,
    SETTINGS_VERSION,
    load_settings,
    settings_environment,
    settings_status,
)


def _settings_path(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_project_settings_is_grouped_and_contains_no_embedded_comments():
    root = Path(__file__).parents[2]
    document = json.loads((root / "settings.json").read_text(encoding="utf-8"))

    assert document["schema_name"] == SETTINGS_SCHEMA
    assert document["schema_version"] == SETTINGS_VERSION
    assert document["profile"]["name"] == "core"
    assert document["shared_server"]["enabled"] is False
    assert all(document["evidence_integrity"]["checks"].values())
    assert all(not key.startswith("_comment") for key in _walk_keys(document))


def _walk_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def test_deleted_entries_use_safe_defaults_without_an_error(tmp_path):
    path = _settings_path(
        tmp_path,
        {
            "schema_name": SETTINGS_SCHEMA,
            "profile": {},
            "evidence_integrity": {"checks": {}},
        },
    )
    environment = {SETTINGS_PATH_ENV: str(path)}

    settings = load_settings(environment)
    status = settings_status(environment)

    assert settings["profile"]["name"] == "core"
    assert settings["runtime"] == {"directory": None, "jobs_directory": None}
    assert settings["paths"]["model_read_roots"] == []
    assert settings["shared_server"]["enabled"] is False
    assert all(settings["evidence_integrity"]["checks"].values())
    assert status["configuration_state"] == "valid"
    assert status["settings_errors"] == []


def test_invalid_value_keeps_only_that_setting_at_default_and_reports_it(tmp_path):
    path = _settings_path(
        tmp_path,
        {
            "profile": {"name": "wave\u0000optics"},
            "runtime": {"directory": "D:/bad\npath"},
            "shared_server": {"enabled": "true"},
        },
    )
    environment = {SETTINGS_PATH_ENV: str(path)}

    settings = load_settings(environment)
    status = settings_status(environment)

    assert settings["profile"]["name"] == "core"
    assert settings["runtime"]["directory"] is None
    assert settings["shared_server"]["enabled"] is False
    assert status["configuration_state"] == "degraded"
    assert status["defaults_used_for_invalid_or_missing_entries"] is True
    assert {item["path"] for item in status["settings_errors"]} >= {
        "settings.profile.name",
        "settings.runtime.directory",
        "settings.shared_server.enabled",
    }
    serialized = json.dumps(status, ensure_ascii=False)
    assert str(path) not in serialized


def test_malformed_json_falls_back_to_the_complete_safe_defaults(tmp_path):
    path = tmp_path / "malformed.json"
    path.write_text('{"profile":', encoding="utf-8")
    status = settings_status({SETTINGS_PATH_ENV: str(path)})
    settings = load_settings({SETTINGS_PATH_ENV: str(path)})

    assert settings["profile"]["name"] == "core"
    assert status["configuration_state"] == "degraded"
    assert status["reason_code"] == "settings_json_invalid"
    assert status["settings_errors"][0]["path"] == "settings"


def test_project_settings_fill_legacy_runtime_shape_for_existing_callers(tmp_path):
    path = _settings_path(
        tmp_path,
        {
            "runtime": {
                "directory": "D:/comsol_runtime",
                "jobs_directory": "D:/comsol_runtime/jobs",
            },
            "shared_server": {"enabled": True},
        },
    )
    effective = settings_environment({SETTINGS_PATH_ENV: str(path)})

    assert effective["COMSOL_MCP_RUNTIME_DIR"] == str(Path("D:/comsol_runtime"))
    assert effective["COMSOL_MCP_JOBS_DIR"] == str(Path("D:/comsol_runtime/jobs"))
    assert effective["COMSOL_MCP_ENABLE_SHARED_SERVER"] == "true"
