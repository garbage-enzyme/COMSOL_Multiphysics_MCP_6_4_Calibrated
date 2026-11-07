"""Settings transport field tests (0.8.2 transport selector)."""

from __future__ import annotations

import json

import pytest

from comsol_mcp.settings import (
    SETTINGS_VERSION,
    SettingsError,
    load_settings,
    load_settings_report,
)

pytestmark = pytest.mark.usefixtures("_isolated_settings")


@pytest.fixture
def _isolated_settings(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "schema_name": "comsol_mcp.settings",
                "schema_version": SETTINGS_VERSION,
                "profile": {"name": "core"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("COMSOL_MCP_SETTINGS_PATH", str(settings))
    yield settings


def _write(document: dict, path) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")


def test_default_transport_is_python(_isolated_settings):
    assert load_settings()["transport"] == "python"


def test_default_shim_binary_is_empty(_isolated_settings):
    assert load_settings()["shim_binary"] == {
        "path": None,
        "sha256": None,
        "version": None,
        "platform": None,
    }


def test_transport_python_round_trips(_isolated_settings):
    document = json.loads(_isolated_settings.read_text(encoding="utf-8"))
    document["transport"] = "python"
    _write(document, _isolated_settings)
    assert load_settings()["transport"] == "python"


VALID_BINARY = {
    "path": "D:/shim/comsol-mcp-shim.exe",
    "sha256": "a" * 64,
    "version": "0.8.2-dev",
    "platform": "x86_64-pc-windows-msvc",
}


def _shim_document(settings_path, **overrides) -> dict:
    path = settings_path
    document = json.loads(path.read_text(encoding="utf-8"))
    document["transport"] = "shim"
    document["runtime"] = {"directory": "D:/mcp_tests/runtime-fixture"}
    document["shim_binary"] = dict(VALID_BINARY)
    for key, value in overrides.items():
        if key == "runtime_directory":
            document["runtime"]["directory"] = value
        else:
            document["shim_binary"][key] = value
    return document


def test_transport_shim_with_valid_identity_round_trips(_isolated_settings):
    _write(_shim_document(_isolated_settings), _isolated_settings)
    loaded = load_settings()
    assert loaded["transport"] == "shim"
    assert loaded["shim_binary"] == VALID_BINARY


@pytest.mark.parametrize("missing", ["path", "sha256", "version", "platform"])
def test_transport_shim_requires_every_binary_field(_isolated_settings, missing):
    _write(_shim_document(_isolated_settings, **{missing: None}), _isolated_settings)
    with pytest.raises(SettingsError, match="shim.*requires") as error:
        load_settings()
    assert error.value.reason_code == "transport_setting_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", "latest"),
        ("version", "0.8"),
        ("platform", "windows"),
        ("platform", "X86_64-PC-WINDOWS-MSVC"),
    ],
)
def test_transport_shim_rejects_malformed_identity(_isolated_settings, field, value):
    _write(_shim_document(_isolated_settings, **{field: value}), _isolated_settings)
    with pytest.raises(SettingsError, match="shim.*requires"):
        load_settings()
    report_document = _shim_document(_isolated_settings, **{field: value})
    report_document["transport"] = "python"
    _write(report_document, _isolated_settings)
    report = load_settings_report()
    assert any(error.get("path") == f"settings.shim_binary.{field}" for error in report["errors"])


@pytest.mark.parametrize(
    "directory",
    ["%PROGRAMDATA%/comsol_mcp/runtime", "~/runtime", " D:/runtime", "relative/runtime"],
)
def test_transport_shim_requires_a_literal_runtime_directory(_isolated_settings, directory):
    _write(_shim_document(_isolated_settings, runtime_directory=directory), _isolated_settings)
    with pytest.raises(SettingsError, match="runtime.directory") as error:
        load_settings()
    assert error.value.reason_code == "transport_setting_invalid"


def test_snapshot_validates_the_exact_bytes_it_hashes(_isolated_settings):
    import hashlib

    from comsol_mcp.settings import load_settings_snapshot, read_settings_digest

    _write(_shim_document(_isolated_settings), _isolated_settings)
    raw = _isolated_settings.read_bytes()
    snapshot = load_settings_snapshot()
    assert snapshot.raw == raw
    assert snapshot.sha256 == hashlib.sha256(raw).hexdigest()
    assert snapshot.settings["transport"] == "shim"
    assert snapshot.identity() == {"path": str(snapshot.location.path), "sha256": snapshot.sha256}
    assert read_settings_digest(snapshot.location.path) == snapshot.sha256
    _isolated_settings.write_text("{}", encoding="utf-8")
    assert read_settings_digest(snapshot.location.path) != snapshot.sha256


def test_transport_shim_without_binary_raises(_isolated_settings):
    document = json.loads(_isolated_settings.read_text(encoding="utf-8"))
    document["transport"] = "shim"
    _write(document, _isolated_settings)
    with pytest.raises(SettingsError, match="shim.*requires.*path.*sha256.*version.*platform"):
        load_settings()


def test_transport_shim_with_degraded_path_still_requires_identity(_isolated_settings):
    """A bad path degrades to None; the cross-field check then raises."""
    document = json.loads(_isolated_settings.read_text(encoding="utf-8"))
    document["transport"] = "shim"
    document["shim_binary"] = {**VALID_BINARY, "path": "relative/shim.exe"}
    _write(document, _isolated_settings)
    with pytest.raises(SettingsError, match="shim.*requires"):
        load_settings()


def test_transport_shim_with_degraded_sha256_still_requires_identity(_isolated_settings):
    """A bad sha256 degrades to None; the cross-field check then raises."""
    document = json.loads(_isolated_settings.read_text(encoding="utf-8"))
    document["transport"] = "shim"
    document["shim_binary"] = {**VALID_BINARY, "sha256": "xyz"}
    _write(document, _isolated_settings)
    with pytest.raises(SettingsError, match="shim.*requires"):
        load_settings()


def test_unknown_transport_degrades_to_python_with_reported_error(_isolated_settings):
    document = json.loads(_isolated_settings.read_text(encoding="utf-8"))
    document["transport"] = "grpc"
    _write(document, _isolated_settings)
    assert load_settings()["transport"] == "python"
    report = load_settings_report()
    assert any(error.get("path") == "settings.transport" for error in report["errors"])


def test_v130_settings_without_transport_stay_readable(_isolated_settings):
    document = json.loads(_isolated_settings.read_text(encoding="utf-8"))
    document["schema_version"] = "1.3.0"
    _write(document, _isolated_settings)
    loaded = load_settings()
    assert loaded["schema_version"] == SETTINGS_VERSION
    assert loaded["transport"] == "python"
