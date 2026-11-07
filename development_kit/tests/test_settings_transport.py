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
    assert load_settings()["shim_binary"] == {"path": None, "sha256": None}


def test_transport_python_round_trips(_isolated_settings):
    document = json.loads(_isolated_settings.read_text(encoding="utf-8"))
    document["transport"] = "python"
    _write(document, _isolated_settings)
    assert load_settings()["transport"] == "python"


def test_transport_shim_with_valid_identity_round_trips(_isolated_settings):
    document = json.loads(_isolated_settings.read_text(encoding="utf-8"))
    document["transport"] = "shim"
    document["shim_binary"] = {"path": "D:/shim/comsol-mcp-shim.exe", "sha256": "a" * 64}
    _write(document, _isolated_settings)
    loaded = load_settings()
    assert loaded["transport"] == "shim"
    assert loaded["shim_binary"]["path"] == "D:/shim/comsol-mcp-shim.exe"
    assert loaded["shim_binary"]["sha256"] == "a" * 64


def test_transport_shim_without_binary_raises(_isolated_settings):
    document = json.loads(_isolated_settings.read_text(encoding="utf-8"))
    document["transport"] = "shim"
    _write(document, _isolated_settings)
    with pytest.raises(SettingsError, match="shim.*requires.*path.*sha256"):
        load_settings()


def test_transport_shim_with_degraded_path_still_requires_identity(_isolated_settings):
    """A bad path degrades to None; the cross-field check then raises."""
    document = json.loads(_isolated_settings.read_text(encoding="utf-8"))
    document["transport"] = "shim"
    document["shim_binary"] = {"path": "relative/shim.exe", "sha256": "a" * 64}
    _write(document, _isolated_settings)
    with pytest.raises(SettingsError, match="shim.*requires"):
        load_settings()


def test_transport_shim_with_degraded_sha256_still_requires_identity(_isolated_settings):
    """A bad sha256 degrades to None; the cross-field check then raises."""
    document = json.loads(_isolated_settings.read_text(encoding="utf-8"))
    document["transport"] = "shim"
    document["shim_binary"] = {"path": "D:/shim.exe", "sha256": "xyz"}
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
