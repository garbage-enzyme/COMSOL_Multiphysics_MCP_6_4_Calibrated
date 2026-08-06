"""Conservative MCP SDK 2.0 and legacy-protocol compatibility tests."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer

from comsol_mcp import __version__
from comsol_mcp.server import SERVER_INSTRUCTIONS, create_server

ROOT = Path(__file__).resolve().parents[2]
LEGACY_PROTOCOL_VERSIONS = (
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
)
CLIENT_CONFIGS = (
    ROOT / "config" / "claude-code-mcp.example.json",
    ROOT / "config" / "codex-mcp.example.toml",
    ROOT / "config" / "hermes-mcp.example.yaml",
    ROOT / "config" / "opencode-mcp.example.json",
)


def _runtime_dependencies() -> list[str]:
    document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return document["project"]["dependencies"]


def _response_payload(result: dict[str, Any]) -> dict[str, Any]:
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        nested = structured.get("result")
        return nested if isinstance(nested, dict) else structured
    candidates = []
    for block in result.get("content", []):
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            continue
        try:
            value = json.loads(block["text"])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            nested = value.get("result")
            candidates.append(nested if isinstance(nested, dict) else value)
    if len(candidates) != 1:
        raise AssertionError("legacy tool result did not contain one JSON object")
    return candidates[0]


async def _read_response(process: asyncio.subprocess.Process, request_id: int) -> dict[str, Any]:
    assert process.stdout is not None
    while True:
        raw = await asyncio.wait_for(process.stdout.readline(), timeout=15)
        if not raw:
            raise AssertionError(f"MCP server closed before response {request_id}")
        message = json.loads(raw)
        if message.get("id") == request_id:
            return message


async def _write_message(process: asyncio.subprocess.Process, message: dict[str, Any]) -> None:
    assert process.stdin is not None
    process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
    await process.stdin.drain()


async def _legacy_stdio_exchange(protocol_version: str, runtime_root: Path) -> dict[str, Any]:
    environment = os.environ.copy()
    environment.pop("CODEX_MCP_PROTOCOL_VERSION", None)
    environment.pop("PYTHONPATH", None)
    environment.update(
        {
            "COMSOL_MCP_PROFILE": "core",
            "COMSOL_MCP_RUNTIME_DIR": str(runtime_root),
            "COMSOL_MCP_SETTINGS_PATH": str((ROOT / "settings.json").resolve()),
        }
    )
    server_code = (
        "from comsol_mcp.server import create_server; "
        "create_server('COMSOL MCP legacy compatibility', profile='core').run()"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        server_code,
        cwd=str(ROOT),
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=8 * 1024 * 1024,
    )
    assert process.stderr is not None
    stderr_task = asyncio.create_task(process.stderr.read())
    try:
        await _write_message(
            process,
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": protocol_version,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "comsol-mcp-legacy-fixture",
                        "version": "1.0.0",
                    },
                },
            },
        )
        initialized = await _read_response(process, 0)
        await _write_message(
            process,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        await _write_message(
            process,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        listed = await _read_response(process, 1)
        await _write_message(
            process,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "capabilities", "arguments": {}},
            },
        )
        called = await _read_response(process, 2)
        await _write_message(
            process,
            {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}},
        )
        resources = await _read_response(process, 3)
    finally:
        active_error = sys.exception()
        if process.stdin is not None:
            process.stdin.close()
            await process.stdin.wait_closed()
        assert process.stdout is not None
        stdout_tail_task = asyncio.create_task(process.stdout.read())
        try:
            await asyncio.wait_for(process.wait(), timeout=15)
        except TimeoutError:
            process.kill()
            await process.wait()
            if active_error is None:
                raise
        await stdout_tail_task
    stderr = (await stderr_task).decode(errors="replace")
    assert process.returncode == 0, stderr
    assert "Traceback" not in stderr
    return {
        "initialized": initialized,
        "listed": listed,
        "called": called,
        "resources": resources,
    }


def test_mcp_dependency_and_package_identity_are_the_conservative_2_0_lane() -> None:
    assert "mcp>=2.0.0,<2.1" in _runtime_dependencies()
    assert __version__ == "0.6.4"


def test_server_uses_official_mcpserver_and_preserves_wire_schema_aliases() -> None:
    server = create_server("MCP SDK 2 compatibility", profile="core")
    assert isinstance(server, MCPServer)
    tools = asyncio.run(server.list_tools())
    assert len(tools) == 47
    capabilities = next(tool for tool in tools if tool.name == "capabilities")
    serialized = capabilities.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert serialized["inputSchema"] == capabilities.input_schema
    assert "input_schema" not in serialized


def test_shipped_client_configuration_does_not_enable_modern_protocol() -> None:
    for path in CLIENT_CONFIGS:
        contents = path.read_text(encoding="utf-8")
        assert "CODEX_MCP_PROTOCOL_VERSION" not in contents
        assert "mcp_2026_07_28" not in contents


@pytest.mark.parametrize("protocol_version", LEGACY_PROTOCOL_VERSIONS)
def test_sdk2_server_preserves_legacy_stdio_protocols(
    protocol_version: str,
    ascii_tmp_path: Path,
) -> None:
    exchange = asyncio.run(
        _legacy_stdio_exchange(protocol_version, ascii_tmp_path / protocol_version)
    )
    initialized = exchange["initialized"]["result"]
    assert initialized["protocolVersion"] == protocol_version
    assert initialized["serverInfo"] == {
        "name": "COMSOL MCP legacy compatibility",
        "version": "0.6.4",
    }
    assert initialized["instructions"] == SERVER_INSTRUCTIONS

    listed = exchange["listed"]["result"]
    assert len(listed["tools"]) == 47
    assert {tool["name"] for tool in listed["tools"]} >= {
        "capabilities",
        "solver_preflight",
        "spectral_characterize",
    }
    assert all("inputSchema" in tool for tool in listed["tools"])
    assert "resultType" not in listed

    called = exchange["called"]["result"]
    assert called.get("isError", False) is False
    assert "resultType" not in called
    capabilities = _response_payload(called)
    assert capabilities["profile"] == "core"
    assert capabilities["tool_count"] == 47
    assert capabilities["session"] == {"connected": False, "starting": False}

    resources = exchange["resources"]["result"]
    assert resources["resources"]
    assert "resultType" not in resources
