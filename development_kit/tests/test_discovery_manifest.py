"""Discovery-manifest generation and freshness tests (0.8.2 P03)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from comsol_mcp import discovery_manifest
from comsol_mcp.discovery_manifest import (
    MANIFEST_SCHEMA_NAME,
    MANIFEST_SCHEMA_VERSION,
    ManifestCaptureError,
    ManifestStaleError,
    binding_variables,
    build_manifest,
    current_binding,
    current_selection,
    load_manifest,
    manifest_bytes,
    verify_manifest,
    write_manifest,
)
from comsol_mcp.settings import SETTINGS_PATH_ENV

ROOT = Path(__file__).parents[2]


def _settings_document(*, paginated: bool) -> str:
    return json.dumps(
        {
            "schema_name": "comsol_mcp.settings",
            "schema_version": "1.3.0",
            "profile": {"name": "core"},
            "discovery": {"pagination_enabled": paginated},
        }
    )


@pytest.fixture(scope="module")
def manifest_settings(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("discovery") / "settings.json"
    path.write_text(_settings_document(paginated=False), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _manifest_environment(manifest_settings, monkeypatch):
    """Run every test under the environment the module manifest bound."""
    monkeypatch.setenv(SETTINGS_PATH_ENV, str(manifest_settings))
    monkeypatch.delenv("COMSOL_MCP_PROFILE", raising=False)


@pytest.fixture(scope="module")
def core_manifest(manifest_settings) -> dict:
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(SETTINGS_PATH_ENV, str(manifest_settings))
        patch.delenv("COMSOL_MCP_PROFILE", raising=False)
        return build_manifest()


def _payload(manifest: dict, era: str, version: str, method: str, cursor=None) -> str:
    for row in manifest["responses"]:
        if (row["era"], row["protocol_version"], row["method"], row["cursor"]) == (
            era,
            version,
            method,
            cursor,
        ):
            return manifest["payloads"][row["result_sha256"]]
    raise AssertionError(f"no recorded response for {era} {version} {method} {cursor}")


def test_manifest_binds_schema_and_core_profile(core_manifest) -> None:
    assert core_manifest["schema_name"] == MANIFEST_SCHEMA_NAME
    assert core_manifest["schema_version"] == MANIFEST_SCHEMA_VERSION
    assert core_manifest["selection"]["profile"] == "core"
    assert core_manifest["selection"]["enabled_features"] == []
    assert core_manifest["selection"]["pagination_enabled"] is False
    assert core_manifest["selection"]["tool_count"] > 0


def test_manifest_records_every_static_discovery_method(core_manifest) -> None:
    negotiation = core_manifest["negotiation"]
    recorded = {(r["era"], r["protocol_version"], r["method"]) for r in core_manifest["responses"]}
    for version in negotiation["handshake_versions"]:
        for method in ("initialize", "tools/list", "ping"):
            assert ("handshake", version, method) in recorded
    for version in negotiation["modern_versions"]:
        for method in ("server/discover", "tools/list"):
            assert ("modern", version, method) in recorded


def test_manifest_is_deterministic_across_server_instances(core_manifest) -> None:
    second = build_manifest()
    assert second["payloads"] == core_manifest["payloads"]
    assert second["responses"] == core_manifest["responses"]
    assert second["manifest_sha256"] == core_manifest["manifest_sha256"]


def test_instance_disagreement_refuses_generation(monkeypatch) -> None:
    calls: list[str] = []

    async def diverging(client):
        calls.append(client["name"])
        return {
            ("handshake", "2025-11-25"): [
                {"method": "ping", "cursor": None, "text": client["name"]}
            ]
        }

    monkeypatch.setattr(discovery_manifest, "_capture_once", diverging)
    with pytest.raises(ManifestCaptureError, match="differ between instances"):
        build_manifest()
    assert len(calls) == 2


def test_wire_envelope_splices_request_id(core_manifest) -> None:
    prefix, suffix = core_manifest["wire"]["envelope"].split("<request id JSON>")
    infix, closing = suffix.split("<result bytes>")
    text = _payload(core_manifest, "handshake", "2025-11-25", "ping")
    rebuilt = f"{prefix}42{infix}{text}{closing}"
    assert json.loads(rebuilt) == {"jsonrpc": "2.0", "id": 42, "result": json.loads(text)}


def test_fresh_manifest_round_trips(core_manifest, tmp_path) -> None:
    path = tmp_path / "manifest.json"
    write_manifest(path, core_manifest)
    assert verify_manifest(core_manifest) == []
    assert load_manifest(path)["manifest_sha256"] == core_manifest["manifest_sha256"]


def test_profile_change_makes_manifest_stale(core_manifest, monkeypatch) -> None:
    monkeypatch.setenv("COMSOL_MCP_PROFILE", "wave_optics")
    problems = verify_manifest(core_manifest)
    assert "selection_changed:profile" in problems
    assert "binding_changed:environment:COMSOL_MCP_PROFILE" in problems


def test_unrelated_settings_variable_change_makes_manifest_stale(
    core_manifest, monkeypatch
) -> None:
    monkeypatch.setenv("JAVA_HOME", "D:/other/jre")
    assert "binding_changed:environment:JAVA_HOME" in verify_manifest(core_manifest)


def test_settings_bytes_change_makes_manifest_stale(core_manifest, manifest_settings) -> None:
    original = manifest_settings.read_text(encoding="utf-8")
    try:
        manifest_settings.write_text(original + "\n", encoding="utf-8")
        assert "binding_changed:settings" in verify_manifest(core_manifest)
    finally:
        manifest_settings.write_text(original, encoding="utf-8")


def test_missing_settings_file_is_stale_not_a_crash(
    core_manifest, manifest_settings, monkeypatch
) -> None:
    monkeypatch.setenv(SETTINGS_PATH_ENV, str(manifest_settings) + ".missing")
    problems = verify_manifest(core_manifest)
    assert problems
    assert any(code.startswith(("binding_changed", "selection_unavailable")) for code in problems)


def test_tampered_payload_is_refused(core_manifest) -> None:
    tampered = json.loads(json.dumps(core_manifest))
    digest = sorted(tampered["payloads"])[0]
    tampered["payloads"][digest] += " "
    problems = verify_manifest(tampered)
    assert "manifest_hash_mismatch" in problems
    assert f"payload_hash_mismatch:{digest}" in problems


def test_unknown_schema_version_is_refused(core_manifest) -> None:
    changed = {**core_manifest, "schema_version": "9.9.9"}
    assert "schema_version_unsupported" in verify_manifest(changed)


def test_load_refuses_a_stale_file(core_manifest, tmp_path, monkeypatch) -> None:
    path = tmp_path / "manifest.json"
    path.write_bytes(manifest_bytes(core_manifest))
    monkeypatch.setenv("JAVA_HOME", "D:/other/jre")
    with pytest.raises(ManifestStaleError) as raised:
        load_manifest(path)
    assert "binding_changed:environment:JAVA_HOME" in raised.value.problems


def test_load_refuses_an_oversize_file(tmp_path, monkeypatch) -> None:
    path = tmp_path / "huge.json"
    path.write_bytes(b"{" + b" " * 64 + b"}")
    monkeypatch.setattr(discovery_manifest, "MAX_MANIFEST_BYTES", 16)
    with pytest.raises(ValueError):
        load_manifest(path)


def test_binding_variables_cover_settings_surface() -> None:
    from comsol_mcp import settings

    exported = {
        value
        for key, value in vars(settings).items()
        if key.endswith("_ENV") and isinstance(value, str)
    }
    assert exported <= set(binding_variables())


def test_current_binding_sections() -> None:
    binding = current_binding()
    assert binding["package"]["package_content_sha256"]
    assert binding["interpreter"]["version"]
    assert "mcp" in binding["dependencies"]
    assert binding["settings"]["source"] == "explicit_settings"


def test_pagination_mode_comes_from_settings_not_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMSOL_MCP_ENABLE_DISCOVERY_PAGINATION", "true")
    assert current_selection()["pagination_enabled"] is False
    settings = tmp_path / "paginated.json"
    settings.write_text(_settings_document(paginated=True), encoding="utf-8")
    monkeypatch.setenv(SETTINGS_PATH_ENV, str(settings))
    assert current_selection()["pagination_enabled"] is True


def test_paginated_manifest_pages_cover_every_tool(tmp_path, monkeypatch) -> None:
    settings = tmp_path / "paginated.json"
    settings.write_text(_settings_document(paginated=True), encoding="utf-8")
    monkeypatch.setenv(SETTINGS_PATH_ENV, str(settings))
    manifest = build_manifest()
    for era, version in (("handshake", "2025-11-25"), ("modern", "2026-07-28")):
        rows = [
            r
            for r in manifest["responses"]
            if (r["era"], r["protocol_version"], r["method"]) == (era, version, "tools/list")
        ]
        assert len(rows) > 1
        names = [
            tool["name"]
            for row in rows
            for tool in json.loads(manifest["payloads"][row["result_sha256"]])["tools"]
        ]
        assert len(names) == len(set(names)) == manifest["selection"]["tool_count"]


def test_manifest_file_stays_bounded(core_manifest) -> None:
    assert len(manifest_bytes(core_manifest)) <= core_manifest["bounds"]["max_manifest_bytes"]


def _child_environment(settings: Path, **extra: str) -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("COMSOL_MCP_PROFILE", None)
    environment[SETTINGS_PATH_ENV] = str(settings)
    environment.update(extra)
    return environment


def test_payloads_do_not_depend_on_hash_seed(core_manifest, manifest_settings, tmp_path) -> None:
    payloads = []
    for seed in ("1", "2"):
        output = tmp_path / f"seed-{seed}.json"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "comsol_mcp.discovery_manifest",
                "generate",
                "--output",
                str(output),
            ],
            cwd=ROOT,
            env=_child_environment(manifest_settings, PYTHONHASHSEED=seed),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr[-2000:]
        payloads.append(json.loads(output.read_text(encoding="utf-8"))["payloads"])
    assert payloads[0] == payloads[1] == core_manifest["payloads"]


def test_real_stdio_server_writes_the_manifest_bytes(core_manifest, manifest_settings) -> None:
    """Differential check: the stdio server writes exactly the recorded bytes."""
    version = core_manifest["negotiation"]["handshake_versions"][-1]
    modern = core_manifest["negotiation"]["modern_versions"][0]
    meta = {
        "io.modelcontextprotocol/protocolVersion": modern,
        "io.modelcontextprotocol/clientInfo": {"name": "stdio-differential", "version": "0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    exchanges = [
        (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": version,
                    "capabilities": {},
                    "clientInfo": {"name": "stdio-differential", "version": "0"},
                },
            },
            ("handshake", version, "initialize"),
        ),
        ({"jsonrpc": "2.0", "method": "notifications/initialized"}, None),
        ({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, ("handshake", version, "tools/list")),
        ({"jsonrpc": "2.0", "id": 3, "method": "ping"}, ("handshake", version, "ping")),
    ]
    modern_exchanges = [
        (
            {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": meta}},
            ("modern", modern, "server/discover"),
        ),
        (
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": meta}},
            ("modern", modern, "tools/list"),
        ),
    ]
    terminator = core_manifest["wire"]["stdio_line_terminator"].encode("utf-8")
    for script in (exchanges, modern_exchanges):
        server = subprocess.Popen(
            [sys.executable, "-m", "comsol_mcp.server"],
            cwd=ROOT,
            env=_child_environment(manifest_settings),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            for request, key in script:
                server.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
                server.stdin.flush()
                if key is None:
                    continue
                line = server.stdout.readline()
                assert line.endswith(terminator)
                body = line[: -len(terminator)].decode("utf-8")
                prefix = f'{{"jsonrpc":"2.0","id":{request["id"]},"result":'
                assert body.startswith(prefix) and body.endswith("}")
                assert body[len(prefix) : -1] == _payload(core_manifest, *key)
        finally:
            server.stdin.close()
            server.wait(timeout=30)
