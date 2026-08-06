"""Solver-free tests for the isolated research-adapter template probe."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from development_kit.scripts import research_adapter_template_probe as probe


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "template.mph"
    path.write_bytes(b"immutable-template")
    return path


def _args(root: Path, source: Path, **changes) -> argparse.Namespace:
    values = {
        "test_root": root,
        "source_model": source,
        "cores": 2,
        "version": "6.4",
        "dry_run": True,
    }
    values.update(changes)
    return argparse.Namespace(**values)


def test_dry_run_contract_is_path_redacted_and_solver_free(tmp_path: Path):
    source = _source(tmp_path)
    root = Path("D:/mcp_tests/a70p1") if os.name == "nt" else tmp_path / "a70p1"
    spec = probe._normalized_spec(_args(root, source))

    result = probe._dry_run_receipt(spec)

    assert result["success"] is True
    assert result["solver_started"] is False
    assert result["filesystem_modified"] is False
    assert result["source"]["sha256"] == probe._sha256(source)
    assert result["source"]["path_included"] is False
    assert result["isolation"] == {
        "profile": "full",
        "runtime_inside_test_root": True,
        "artifacts_inside_test_root": True,
        "model_read_root_count": 1,
        "shared_server_enabled": False,
        "strict_evidence_checks_requested": True,
        "settings_sha256": result["isolation"]["settings_sha256"],
        "paths_included": False,
    }
    assert not root.exists()


def test_settings_bind_only_candidate_roots_and_strict_checks(tmp_path: Path):
    source = _source(tmp_path)
    root = Path("D:/mcp_tests/a70p2") if os.name == "nt" else tmp_path / "a70p2"
    settings = probe._settings_document(probe._normalized_spec(_args(root, source)))

    assert settings["profile"] == {"name": "full"}
    assert settings["runtime"]["directory"] == str(root / "runtime")
    assert settings["paths"] == {
        "model_read_roots": [str(source.parent.resolve())],
        "artifact_write_root": str(root / "artifacts"),
    }
    assert settings["shared_server"] == {"enabled": False}
    assert all(settings["evidence_integrity"]["checks"].values())
    assert settings["lexical_docs"]["enabled"] is False
    assert settings["semantic_docs"]["enabled"] is False


def test_stdio_environment_preserves_proxy_and_user_configuration(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HTTPS_PROXY", "http://organization-proxy.invalid")
    monkeypatch.setenv("OPENAI_CONFIG_FILE", "organization-user-config.json")
    monkeypatch.setenv("PYTHONPATH", "untrusted-source")
    monkeypatch.setenv("COMSOL_MCP_RUNTIME_DIR", "stale-runtime")
    monkeypatch.setenv("COMSOL_MCP_UNREVIEWED", "stale")
    settings = tmp_path / "settings.json"

    environment = probe._stdio_environment(settings)

    assert environment["HTTPS_PROXY"] == "http://organization-proxy.invalid"
    assert environment["OPENAI_CONFIG_FILE"] == "organization-user-config.json"
    assert environment["COMSOL_MCP_SETTINGS_PATH"] == str(settings)
    assert "PYTHONPATH" not in environment
    assert "COMSOL_MCP_RUNTIME_DIR" not in environment
    assert "COMSOL_MCP_UNREVIEWED" not in environment


@pytest.mark.parametrize(
    ("root", "message"),
    [
        (Path("D:/outside/a70p"), "direct child"),
        (Path("D:/mcp_tests/leaf-is-too-long"), "leaf <= 12"),
    ],
)
def test_windows_root_policy_rejects_escape_or_long_leaf(tmp_path: Path, root: Path, message: str):
    if os.name != "nt":
        pytest.skip("Windows path contract")
    source = _source(tmp_path)

    with pytest.raises(ValueError, match=message):
        probe._normalized_spec(_args(root, source))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"cores": 0}, "cores"),
        ({"cores": True}, "cores"),
        ({"cores": 65}, "cores"),
        ({"version": "7.0"}, "calibrated only"),
    ],
)
def test_probe_rejects_unbounded_or_uncalibrated_start_inputs(
    tmp_path: Path, changes: dict, message: str
):
    source = _source(tmp_path)
    root = Path("D:/mcp_tests/a70p3") if os.name == "nt" else tmp_path / "a70p3"

    with pytest.raises(ValueError, match=message):
        probe._normalized_spec(_args(root, source, **changes))


def test_tool_payload_accepts_structured_and_text_wrappers():
    structured = SimpleNamespace(
        isError=False,
        structuredContent={"result": {"success": True, "value": 1}},
        content=[],
    )
    text = SimpleNamespace(
        isError=False,
        structuredContent=None,
        content=[SimpleNamespace(text=json.dumps({"result": {"success": True, "value": 2}}))],
    )

    assert probe._tool_payload(structured)["value"] == 1
    assert probe._tool_payload(text)["value"] == 2


def test_public_call_is_bounded_and_contains_only_digest_metadata(monkeypatch):
    payload = {"success": True, "private_path": "C:/private/template.mph", "count": 3}

    result = probe._public_call("model_inspect", payload, 0.25)

    assert result["tool"] == "model_inspect"
    assert result["success"] is True
    assert result["count"] == 3
    assert len(result["response_sha256"]) == 64
    assert "private_path" not in result
    monkeypatch.setattr(probe, "MAX_RESPONSE_BYTES", 1)
    with pytest.raises(ValueError, match="response exceeds"):
        probe._public_call("model_inspect", payload, 0.25)


def test_atomic_json_write_cleans_temporary_residue_on_failure(tmp_path: Path, monkeypatch):
    destination = tmp_path / "receipt.json"
    monkeypatch.setattr(probe.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError()))

    with pytest.raises(OSError):
        probe._atomic_write_json(destination, {"success": True}, maximum_bytes=1024)

    assert not destination.exists()
    assert list(tmp_path.glob("*.tmp")) == []
