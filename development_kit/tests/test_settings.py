"""Shared project settings defaults, validation, and error-reporting contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.settings import (
    MAX_SETTINGS_BYTES,
    SETTINGS_PATH_ENV,
    SETTINGS_SCHEMA,
    SETTINGS_VERSION,
    default_settings_document,
    load_settings,
    normalize_settings_document,
    settings_environment,
    settings_status,
)


def _safe_defaults(environ=None) -> dict:
    return default_settings_document(environ=environ)


def _settings_path(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_project_settings_is_grouped_and_contains_no_embedded_comments():
    root = Path(__file__).parents[2]
    path = root / "settings.json"
    document = json.loads(path.read_text(encoding="utf-8"))

    assert document["schema_name"] == SETTINGS_SCHEMA
    assert document["schema_version"] == SETTINGS_VERSION
    assert document["profile"]["name"] == "core"
    assert document["shared_server"]["enabled"] is False
    assert (
        document["evidence_integrity"]["checks"] == _safe_defaults()["evidence_integrity"]["checks"]
    )
    assert all(document["evidence_integrity"]["checks"].values())
    loaded = load_settings({SETTINGS_PATH_ENV: str(path)})
    assert loaded == load_settings({})
    assert loaded.keys() == document.keys()
    assert all(
        loaded[section].keys() == value.keys()
        for section, value in document.items()
        if isinstance(value, dict)
    )
    status = settings_status({SETTINGS_PATH_ENV: str(path)})
    assert status["configuration_state"] == "valid"
    assert status["settings_errors"] == []
    assert all(not key.startswith("_comment") for key in _walk_keys(document))
    serialized = json.dumps(document, sort_keys=True)
    assert len(serialized.encode("utf-8")) <= 64 * 1024
    assert str(Path.home()).casefold() not in serialized.casefold()
    assert not any(
        marker in key.casefold()
        for key in _walk_keys(document)
        for marker in ("password", "secret", "api_key", "access_token")
    )


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
    defaults = default_settings_document(environ=environment)

    assert settings["profile"]["name"] == "core"
    assert settings["runtime"] == defaults["runtime"]
    assert settings["paths"] == defaults["paths"]
    assert settings["manuals"] == {"root": None}
    assert settings["lexical_docs"] == {"enabled": False, "index_path": None}
    assert settings["semantic_docs"] == {
        "enabled": False,
        "root": None,
        "model_path": None,
    }
    assert settings["shared_server"]["enabled"] is False
    assert all(settings["evidence_integrity"]["checks"].values())
    assert status["configuration_state"] == "valid"
    assert status["settings_errors"] == []


def test_invalid_value_keeps_only_that_setting_at_default_and_reports_it(tmp_path):
    path = _settings_path(
        tmp_path,
        {
            "profile": {"name": "wave\u0000optics"},
            "runtime": {
                "directory": "D:/bad\npath",
                "jobs_directory": "D:/valid/jobs",
            },
            "shared_server": {"enabled": "true"},
        },
    )
    environment = {SETTINGS_PATH_ENV: str(path)}

    settings = load_settings(environment)
    status = settings_status(environment)

    assert settings["profile"]["name"] == "core"
    assert (
        settings["runtime"]["directory"]
        == default_settings_document(environ=environment)["runtime"]["directory"]
    )
    assert settings["runtime"]["jobs_directory"] == str(Path("D:/valid/jobs"))
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
    environment = {SETTINGS_PATH_ENV: str(path)}
    status = settings_status(environment)
    settings = load_settings(environment)

    assert settings == _safe_defaults(environment)
    assert status["configuration_state"] == "degraded"
    assert status["reason_code"] == "settings_json_invalid"
    assert status["settings_errors"][0]["path"] == "settings"


def test_deeply_nested_json_falls_back_without_recursion_escape(tmp_path):
    path = tmp_path / "deeply-nested.json"
    path.write_text(
        '{"unknown":' + "[" * 1500 + "0" + "]" * 1500 + "}",
        encoding="utf-8",
    )

    environment = {SETTINGS_PATH_ENV: str(path)}
    status = settings_status(environment)

    assert load_settings(environment) == _safe_defaults(environment)
    assert status["configuration_state"] == "degraded"
    assert status["settings_errors"][0]["error_type"] == "RecursionError"


def test_expanduser_runtime_error_isolated_to_the_invalid_path(tmp_path, monkeypatch):
    path = _settings_path(
        tmp_path,
        {
            "profile": {"name": "wave_optics"},
            "runtime": {"directory": "D:/trigger"},
            "shared_server": {"enabled": True},
        },
    )
    original_expanduser = Path.expanduser

    def selective_expanduser(value):
        if value.as_posix() == "D:/trigger":
            raise RuntimeError("synthetic missing home")
        return original_expanduser(value)

    monkeypatch.setattr(Path, "expanduser", selective_expanduser)
    environment = {SETTINGS_PATH_ENV: str(path)}

    settings = load_settings(environment)
    status = settings_status(environment)

    assert settings["profile"]["name"] == "wave_optics"
    assert (
        settings["runtime"]["directory"]
        == default_settings_document(environ=environment)["runtime"]["directory"]
    )
    assert settings["shared_server"]["enabled"] is True
    assert [item["path"] for item in status["settings_errors"]] == ["settings.runtime.directory"]


def test_oversized_settings_fall_back_without_unbounded_read(tmp_path):
    path = tmp_path / "oversized.json"
    path.write_bytes(b"{" + b" " * MAX_SETTINGS_BYTES + b"}")

    status = settings_status({SETTINGS_PATH_ENV: str(path)})

    assert status["configuration_state"] == "degraded"
    assert status["reason_code"] == "settings_size_invalid"


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


def test_split_defaults_preserve_unicode_models_and_ascii_runtime(
    tmp_path, ascii_tmp_path, monkeypatch
):
    local_appdata = tmp_path / "用户" / "AppData" / "Local"
    local_appdata.mkdir(parents=True)
    program_data = ascii_tmp_path / "ProgramData"
    program_data.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    monkeypatch.setenv("PROGRAMDATA", str(program_data))
    path = _settings_path(
        tmp_path,
        {
            "runtime": {"directory": "%PROGRAMDATA%/comsol_mcp/runtime"},
            "paths": {
                "model_read_roots": ["%LOCALAPPDATA%/comsol_mcp/models"],
                "artifact_write_root": "%PROGRAMDATA%/comsol_mcp/artifacts",
            },
        },
    )

    settings = load_settings(
        {
            SETTINGS_PATH_ENV: str(path),
            "LOCALAPPDATA": str(local_appdata),
            "PROGRAMDATA": str(program_data),
        }
    )
    user_root = local_appdata / "comsol_mcp"
    machine_root = program_data / "comsol_mcp"

    assert Path(settings["runtime"]["directory"]).resolve() == (machine_root / "runtime").resolve()
    assert [Path(item).resolve() for item in settings["paths"]["model_read_roots"]] == [
        (user_root / "models").resolve()
    ]
    assert (
        Path(settings["paths"]["artifact_write_root"]).resolve()
        == (machine_root / "artifacts").resolve()
    )
    assert settings["paths"]["model_read_roots"][0].isascii() is False
    assert settings["runtime"]["directory"].isascii() is True
    assert settings["paths"]["artifact_write_root"].isascii() is True


def test_load_settings_expands_tokens_from_the_supplied_environment(tmp_path):
    local_appdata = tmp_path / "injected-local"
    program_data = tmp_path / "injected-program"
    path = _settings_path(
        tmp_path,
        {
            "runtime": {"directory": "%PROGRAMDATA%/runtime"},
            "paths": {"model_read_roots": ["%LOCALAPPDATA%/models"]},
        },
    )
    environment = {
        SETTINGS_PATH_ENV: str(path),
        "LOCALAPPDATA": str(local_appdata),
        "PROGRAMDATA": str(program_data),
    }

    settings = load_settings(environment)

    assert Path(settings["runtime"]["directory"]) == program_data / "runtime"
    assert [Path(item) for item in settings["paths"]["model_read_roots"]] == [
        local_appdata / "models"
    ]


def test_non_ascii_durable_paths_fall_back_without_rejecting_unicode_models(tmp_path):
    path = _settings_path(
        tmp_path,
        {
            "runtime": {
                "directory": str(tmp_path / "运行"),
                "jobs_directory": str(tmp_path / "任务"),
            },
            "paths": {
                "model_read_roots": [str(tmp_path / "模型")],
                "artifact_write_root": str(tmp_path / "产物"),
            },
        },
    )

    status = settings_status({SETTINGS_PATH_ENV: str(path)})
    settings = load_settings({SETTINGS_PATH_ENV: str(path)})

    assert settings["paths"]["model_read_roots"] == [str(tmp_path / "模型")]
    assert settings["runtime"]["directory"].isascii()
    assert settings["runtime"]["jobs_directory"] is None
    assert settings["paths"]["artifact_write_root"].isascii()
    assert {item["path"] for item in status["settings_errors"]} == {
        "settings.runtime.directory",
        "settings.runtime.jobs_directory",
        "settings.paths.artifact_write_root",
    }


def test_legacy_override_preserves_itself_without_suppressing_project_defaults(tmp_path):
    path = _settings_path(
        tmp_path,
        {
            "profile": {"name": "wave_optics"},
            "runtime": {
                "directory": "D:/project-runtime",
                "jobs_directory": "D:/project-runtime/jobs",
            },
            "shared_server": {"enabled": True},
        },
    )
    effective = settings_environment(
        {
            SETTINGS_PATH_ENV: str(path),
            "COMSOL_MCP_RUNTIME_DIR": "E:/explicit-runtime",
        }
    )

    assert effective["COMSOL_MCP_RUNTIME_DIR"] == "E:/explicit-runtime"
    assert effective["COMSOL_MCP_JOBS_DIR"] == str(Path("D:/project-runtime/jobs"))
    assert effective["COMSOL_MCP_PROFILE"] == "wave_optics"
    assert effective["COMSOL_MCP_ENABLE_SHARED_SERVER"] == "true"


def test_explicit_empty_legacy_value_is_preserved(tmp_path):
    path = _settings_path(tmp_path, {"profile": {"name": "wave_optics"}})

    effective = settings_environment({SETTINGS_PATH_ENV: str(path), "COMSOL_MCP_PROFILE": ""})

    assert effective["COMSOL_MCP_PROFILE"] == ""


def test_legacy_synthetic_profiles_migrate_to_independent_feature_gates() -> None:
    semantic = normalize_settings_document(
        {
            "schema_name": SETTINGS_SCHEMA,
            "schema_version": "1.1.0",
            "profile": {"name": "semantic_docs"},
        }
    )
    shared = normalize_settings_document(
        {
            "schema_name": SETTINGS_SCHEMA,
            "schema_version": "1.1.0",
            "profile": {"name": "desktop_shared"},
            "shared_server": {"enabled": False},
        }
    )
    full = normalize_settings_document(
        {
            "schema_name": SETTINGS_SCHEMA,
            "schema_version": "1.1.0",
            "profile": {"name": "full"},
        }
    )

    assert semantic["errors"] == []
    assert semantic["settings"]["schema_version"] == SETTINGS_VERSION
    assert semantic["settings"]["profile"]["name"] == "core"
    assert semantic["settings"]["semantic_docs"]["enabled"] is True
    assert shared["errors"] == []
    assert shared["settings"]["profile"]["name"] == "core"
    assert shared["settings"]["shared_server"]["enabled"] is True
    assert full["errors"] == []
    assert full["settings"]["profile"]["name"] == "full"
    assert full["settings"]["semantic_docs"]["enabled"] is True


def test_settings_1_2_moves_lexical_index_out_of_semantic_group() -> None:
    report = normalize_settings_document(
        {
            "schema_name": SETTINGS_SCHEMA,
            "schema_version": "1.2.0",
            "profile": {"name": "core"},
            "manuals": {
                "enabled": True,
                "pdf_root": "D:/manuals",
                "lexical_index": "D:/legacy.sqlite3",
            },
            "semantic_docs": {
                "enabled": False,
                "root": "D:/semantic",
                "lexical_index": "D:/manuals.sqlite3",
                "model_path": None,
            },
        }
    )

    assert report["errors"] == []
    assert report["settings"]["schema_version"] == SETTINGS_VERSION
    assert report["settings"]["manuals"] == {"root": str(Path("D:/manuals"))}
    assert report["settings"]["lexical_docs"] == {
        "enabled": True,
        "index_path": str(Path("D:/manuals.sqlite3")),
    }
    assert report["settings"]["semantic_docs"] == {
        "enabled": False,
        "root": str(Path("D:/semantic")),
        "model_path": None,
    }


@pytest.mark.parametrize("schema_version", [[], {}])
def test_malformed_legacy_schema_version_falls_back_without_type_error(schema_version) -> None:
    report = normalize_settings_document(
        {
            "schema_name": SETTINGS_SCHEMA,
            "schema_version": schema_version,
            "profile": {"name": "full"},
        }
    )

    assert report["errors"]
    assert report["settings"]["schema_version"] == SETTINGS_VERSION


def test_current_feature_gates_are_boolean_composable_and_environment_visible(tmp_path) -> None:
    path = _settings_path(
        tmp_path,
        {
            "schema_name": SETTINGS_SCHEMA,
            "schema_version": SETTINGS_VERSION,
            "profile": {"name": "wave_optics"},
            "shared_server": {"enabled": True},
            "manuals": {"root": "D:/manuals"},
            "lexical_docs": {"enabled": True, "index_path": "D:/manuals.sqlite3"},
            "semantic_docs": {
                "enabled": True,
                "root": None,
                "model_path": None,
            },
        },
    )

    effective = settings_environment({SETTINGS_PATH_ENV: str(path)})
    loaded = load_settings({SETTINGS_PATH_ENV: str(path)})

    assert loaded["profile"]["name"] == "wave_optics"
    assert loaded["shared_server"]["enabled"] is True
    assert loaded["lexical_docs"]["enabled"] is True
    assert loaded["semantic_docs"]["enabled"] is True
    assert effective["COMSOL_MCP_ENABLE_SHARED_SERVER"] == "true"
    assert effective["COMSOL_MCP_ENABLE_LEXICAL_DOCS"] == "true"
    assert effective["COMSOL_MCP_ENABLE_SEMANTIC_DOCS"] == "true"
    assert effective["COMSOL_MANUALS_ROOT"] == str(Path("D:/manuals"))
    assert effective["COMSOL_LEXICAL_DOCS_INDEX_PATH"] == str(Path("D:/manuals.sqlite3"))
    assert effective["COMSOL_SEMANTIC_LEXICAL_INDEX"] == str(Path("D:/manuals.sqlite3"))
