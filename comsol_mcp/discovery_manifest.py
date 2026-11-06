"""Discovery manifests for Python-free discovery (0.8.2 P03).

A discovery manifest records the exact JSON-RPC ``result`` bytes that the
Python server writes for its static discovery methods under one startup
configuration. The module captures those bytes through the MCP SDK's own
in-memory transport and the same serializer that ``stdio_server`` uses.

A manifest also binds every raw input that decides those bytes: package
content, interpreter, MCP SDK and pydantic versions, the effective settings
file bytes, and every settings-related environment variable. A consumer may
reuse a manifest only when every binding still matches byte for byte.
Otherwise the consumer forwards the request to a Python server. A manifest
never carries live model, solver, job, or environment status.

Generation refuses rather than guesses: two independent server instances
with different client identities must produce identical bytes for every
recorded response.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA_NAME = "comsol_mcp.discovery_manifest"
MANIFEST_SCHEMA_VERSION = "1.0.0"
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_RESULT_BYTES = 4 * 1024 * 1024
MAX_TOOLS_LIST_PAGES = 64
MAX_SETTINGS_BYTES = 4 * 1024 * 1024
CAPTURE_TIMEOUT_SECONDS = 60.0
UNLISTED_PROTOCOL_VERSION = "1970-01-01"

# Variables that change settings-file resolution or path expansion but are
# not exported as *_ENV names by ``comsol_mcp.settings``.
RESOLUTION_VARIABLES = (
    "HOME",
    "USERPROFILE",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)
_BOUND_DISTRIBUTION_MODULES = ("mcp", "mcp_types", "pydantic", "pydantic_core")
_CLIENT_IDENTITIES = (
    {"name": "comsol-mcp-manifest-a", "version": "1"},
    {"name": "comsol-mcp-manifest-b", "version": "2"},
)
_PROTOCOL_META_KEY = "io.modelcontextprotocol/protocolVersion"
_CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"
_CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"

Send = Callable[[dict[str, Any]], Awaitable[str | None]]


class ManifestCaptureError(RuntimeError):
    """The server surface cannot be recorded without guessing."""


class ManifestStaleError(ValueError):
    """A manifest does not match the current startup identity."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("discovery manifest is not fresh: " + ", ".join(problems))
        self.problems = problems


def binding_variables() -> tuple[str, ...]:
    """Return every environment variable that a manifest binds."""
    from comsol_mcp import settings

    names = {
        value
        for key, value in vars(settings).items()
        if key.endswith("_ENV") and isinstance(value, str)
    }
    names.update(RESOLUTION_VARIABLES)
    return tuple(sorted(names))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _environment_binding(environ: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    return {
        name: {"present": name in environ, "value": environ.get(name)}
        for name in binding_variables()
    }


def _settings_binding(environ: Mapping[str, str]) -> dict[str, Any]:
    from comsol_mcp.durable.io import read_file_bytes_bounded
    from comsol_mcp.settings import resolve_settings_location

    location = resolve_settings_location(environ)
    path = Path(location.path)
    digest = None
    if path.is_file():
        digest = hashlib.sha256(
            read_file_bytes_bounded(path, max_bytes=MAX_SETTINGS_BYTES)
        ).hexdigest()
    return {"source": location.source, "path": str(path), "sha256": digest}


def _dependency_binding() -> dict[str, str]:
    from importlib.metadata import packages_distributions, version

    owners = packages_distributions()
    bound: dict[str, str] = {}
    for module in _BOUND_DISTRIBUTION_MODULES:
        distributions = owners.get(module)
        if not distributions:
            raise ManifestCaptureError(f"no installed distribution provides {module}")
        for distribution in distributions:
            bound[distribution] = version(distribution)
    return dict(sorted(bound.items()))


def _interpreter_binding() -> dict[str, str]:
    return {
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
        "machine": platform.machine(),
        "os_name": os.name,
        "sys_platform": sys.platform,
    }


def current_binding(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return the startup identity that decides the discovery bytes."""
    from comsol_mcp.build_identity import get_build_identity

    environment = os.environ if environ is None else environ
    return {
        "package": get_build_identity(),
        "interpreter": _interpreter_binding(),
        "dependencies": _dependency_binding(),
        "settings": _settings_binding(environment),
        "environment": _environment_binding(environment),
    }


def current_selection(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return the resolved profile, features, and pagination mode.

    The pagination mode comes from the settings document because the server
    installs its pager from ``load_settings`` and ignores the exported
    environment alias.
    """
    from comsol_mcp.settings import load_settings
    from comsol_mcp.tools.profiles import resolve_profile

    selection = resolve_profile(environ=environ)
    pagination = load_settings(environ)["discovery"]["pagination_enabled"]
    return {
        "profile": selection.name,
        "enabled_features": list(selection.enabled_features),
        "pagination_enabled": bool(pagination),
    }


@asynccontextmanager
async def _session(server: Any) -> AsyncIterator[Send]:
    """Run one in-memory connection and serialize replies like stdio."""
    import anyio
    from mcp import types as sdk_types
    from mcp.shared.memory import create_client_server_memory_streams
    from mcp.shared.message import SessionMessage

    lowlevel = server._lowlevel_server
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        async with anyio.create_task_group() as group:
            group.start_soon(
                lowlevel.run,
                server_read,
                server_write,
                lowlevel.create_initialization_options(),
            )

            async def send(payload: dict[str, Any]) -> str | None:
                line = json.dumps(payload, separators=(",", ":"))
                message = sdk_types.jsonrpc_message_adapter.validate_json(line, by_name=False)
                await client_write.send(SessionMessage(message))
                if "id" not in payload:
                    return None
                with anyio.fail_after(CAPTURE_TIMEOUT_SECONDS):
                    reply = await client_read.receive()
                if isinstance(reply, Exception):
                    raise ManifestCaptureError(f"server transport raised {type(reply).__name__}")
                wire: str = reply.message.model_dump_json(by_alias=True, exclude_unset=True)
                return wire

            try:
                yield send
            finally:
                group.cancel_scope.cancel()


def _result_text(wire: str | None, request_id: int, method: str) -> str:
    """Extract the exact result bytes from one serialized response."""
    prefix = f'{{"jsonrpc":"2.0","id":{request_id},"result":'
    if wire is None or not wire.startswith(prefix) or not wire.endswith("}"):
        raise ManifestCaptureError(f"{method} did not return a result envelope")
    text = wire[len(prefix) : -1]
    if not isinstance(json.loads(text), dict):
        raise ManifestCaptureError(f"{method} result is not a JSON object")
    if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
        raise ManifestCaptureError(f"{method} result exceeds {MAX_RESULT_BYTES} bytes")
    return text


async def _tools_pages(
    send: Send, next_id: int, meta: dict[str, Any] | None
) -> tuple[list[tuple[str | None, str]], int]:
    """Walk ``tools/list`` pages until the server returns no cursor."""
    pages: list[tuple[str | None, str]] = []
    cursor: str | None = None
    while True:
        if len(pages) >= MAX_TOOLS_LIST_PAGES:
            raise ManifestCaptureError(f"tools/list exceeds {MAX_TOOLS_LIST_PAGES} pages")
        params: dict[str, Any] = {}
        if cursor is not None:
            params["cursor"] = cursor
        if meta is not None:
            params["_meta"] = meta
        request: dict[str, Any] = {"jsonrpc": "2.0", "id": next_id, "method": "tools/list"}
        if params:
            request["params"] = params
        text = _result_text(await send(request), next_id, "tools/list")
        next_id += 1
        pages.append((cursor, text))
        following = json.loads(text).get("nextCursor")
        if following is None:
            return pages, next_id
        if not isinstance(following, str):
            raise ManifestCaptureError("tools/list returned a non-string cursor")
        cursor = following


async def _handshake_surface(
    server: Any, protocol_version: str, client: dict[str, str]
) -> list[dict[str, Any]]:
    """Record initialize, paged tools/list, and ping for one handshake era."""
    rows: list[dict[str, Any]] = []
    async with _session(server) as send:
        initialize = await send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": protocol_version,
                    "capabilities": {},
                    "clientInfo": client,
                },
            }
        )
        rows.append(
            {
                "method": "initialize",
                "cursor": None,
                "text": _result_text(initialize, 1, "initialize"),
            }
        )
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        pages, next_id = await _tools_pages(send, 2, None)
        rows.extend({"method": "tools/list", "cursor": c, "text": t} for c, t in pages)
        ping = await send({"jsonrpc": "2.0", "id": next_id, "method": "ping"})
        rows.append({"method": "ping", "cursor": None, "text": _result_text(ping, next_id, "ping")})
    return rows


async def _modern_surface(
    server: Any, protocol_version: str, client: dict[str, str]
) -> list[dict[str, Any]]:
    """Record server/discover and paged tools/list for one modern revision."""
    meta = {
        _PROTOCOL_META_KEY: protocol_version,
        _CLIENT_INFO_META_KEY: client,
        _CLIENT_CAPABILITIES_META_KEY: {},
    }
    rows: list[dict[str, Any]] = []
    async with _session(server) as send:
        discover = await send(
            {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": meta}}
        )
        rows.append(
            {
                "method": "server/discover",
                "cursor": None,
                "text": _result_text(discover, 1, "server/discover"),
            }
        )
        pages, _ = await _tools_pages(send, 2, meta)
        rows.extend({"method": "tools/list", "cursor": c, "text": t} for c, t in pages)
    return rows


async def _capture_once(client: dict[str, str]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Capture every era on fresh server instances for one client identity."""
    from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS

    from comsol_mcp.server import create_server
    from comsol_mcp.tools.profiles import resolve_profile

    captured: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for version in (*HANDSHAKE_PROTOCOL_VERSIONS, UNLISTED_PROTOCOL_VERSION):
        server = create_server(profile=resolve_profile())
        captured[("handshake", version)] = await _handshake_surface(server, version, client)
    for version in MODERN_PROTOCOL_VERSIONS:
        server = create_server(profile=resolve_profile())
        captured[("modern", version)] = await _modern_surface(server, version, client)
    return captured


def _canonical_hash(document: Mapping[str, Any]) -> str:
    body = {key: value for key, value in document.items() if key != "manifest_sha256"}
    return _sha256_text(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    """Serialize one manifest in its canonical file form."""
    text = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def build_manifest() -> dict[str, Any]:
    """Generate the manifest for this process's startup configuration."""
    import anyio
    from mcp_types.version import (
        HANDSHAKE_PROTOCOL_VERSIONS,
        LATEST_HANDSHAKE_VERSION,
        MODERN_PROTOCOL_VERSIONS,
    )

    first = anyio.run(_capture_once, _CLIENT_IDENTITIES[0])
    second = anyio.run(_capture_once, _CLIENT_IDENTITIES[1])
    if first != second:
        differing = sorted(
            f"{era}:{version}"
            for era, version in first
            if first[era, version] != second.get((era, version))
        )
        raise ManifestCaptureError(
            "discovery bytes differ between instances: " + ", ".join(differing)
        )
    unlisted = first.pop(("handshake", UNLISTED_PROTOCOL_VERSION))
    latest = first[("handshake", LATEST_HANDSHAKE_VERSION)]
    if unlisted != latest:
        raise ManifestCaptureError(
            "unlisted protocol version does not fall back to the latest handshake"
        )

    payloads: dict[str, str] = {}
    responses: list[dict[str, Any]] = []
    for (era, version), rows in sorted(first.items()):
        for row in rows:
            digest = _sha256_text(row["text"])
            payloads[digest] = row["text"]
            responses.append(
                {
                    "era": era,
                    "protocol_version": version,
                    "method": row["method"],
                    "cursor": row["cursor"],
                    "result_sha256": digest,
                }
            )

    tool_names = [
        tool["name"]
        for row in first[("handshake", LATEST_HANDSHAKE_VERSION)]
        if row["method"] == "tools/list"
        for tool in json.loads(row["text"])["tools"]
    ]
    selection = current_selection()
    served_paged = any(
        (json.loads(row["text"]).get("_meta") or {}).get("adapter")
        == "comsol_mcp.tools_list_pagination"
        for rows in first.values()
        for row in rows
        if row["method"] == "tools/list"
    )
    if tool_names and served_paged != selection["pagination_enabled"]:
        raise ManifestCaptureError("pagination setting disagrees with the served tools/list bytes")
    selection["tool_count"] = len(tool_names)
    selection["tool_names_sha256"] = _sha256_text(
        json.dumps(sorted(tool_names), separators=(",", ":"))
    )
    manifest: dict[str, Any] = {
        "schema_name": MANIFEST_SCHEMA_NAME,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "binding": current_binding(),
        "selection": selection,
        "wire": {
            "envelope": '{"jsonrpc":"2.0","id":<request id JSON>,"result":<result bytes>}',
            "stdio_line_terminator": "\r\n" if os.name == "nt" else "\n",
            "encoding": "utf-8",
        },
        "negotiation": {
            "handshake_versions": list(HANDSHAKE_PROTOCOL_VERSIONS),
            "modern_versions": list(MODERN_PROTOCOL_VERSIONS),
            "unlisted_handshake_version_answers_as": LATEST_HANDSHAKE_VERSION,
        },
        "bounds": {
            "max_manifest_bytes": MAX_MANIFEST_BYTES,
            "max_result_bytes": MAX_RESULT_BYTES,
            "max_tools_list_pages": MAX_TOOLS_LIST_PAGES,
        },
        "responses": responses,
        "payloads": dict(sorted(payloads.items())),
    }
    manifest["manifest_sha256"] = _canonical_hash(manifest)
    if len(manifest_bytes(manifest)) > MAX_MANIFEST_BYTES:
        raise ManifestCaptureError(f"manifest exceeds {MAX_MANIFEST_BYTES} bytes")
    return manifest


def verify_manifest(manifest: Any, environ: Mapping[str, str] | None = None) -> list[str]:
    """Return stable problem codes. An empty list means the manifest is fresh."""
    if not isinstance(manifest, dict):
        return ["manifest_not_object"]
    problems: list[str] = []
    if manifest.get("schema_name") != MANIFEST_SCHEMA_NAME:
        problems.append("schema_name_unsupported")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        problems.append("schema_version_unsupported")
    required = ("binding", "selection", "responses", "payloads", "manifest_sha256")
    missing = [key for key in required if key not in manifest]
    problems.extend(f"field_missing:{key}" for key in missing)
    if problems:
        return problems
    if _canonical_hash(manifest) != manifest["manifest_sha256"]:
        problems.append("manifest_hash_mismatch")
    payloads = manifest["payloads"]
    if not isinstance(payloads, dict):
        return [*problems, "payloads_not_object"]
    for digest, text in payloads.items():
        if not isinstance(text, str) or _sha256_text(text) != digest:
            problems.append(f"payload_hash_mismatch:{digest}")
    responses = manifest["responses"]
    if not isinstance(responses, list):
        return [*problems, "responses_not_list"]
    for row in responses:
        if not isinstance(row, dict) or row.get("result_sha256") not in payloads:
            problems.append("response_payload_missing")
            break
    environment = os.environ if environ is None else environ
    recorded = manifest["binding"]
    try:
        current = current_binding(environment)
    except Exception as exc:
        return [*problems, f"binding_unavailable:{type(exc).__name__}"]
    for section in ("package", "interpreter", "dependencies", "settings"):
        if not isinstance(recorded, dict) or recorded.get(section) != current[section]:
            problems.append(f"binding_changed:{section}")
    recorded_environment = recorded.get("environment") if isinstance(recorded, dict) else None
    if not isinstance(recorded_environment, dict):
        problems.append("binding_changed:environment")
    else:
        if sorted(recorded_environment) != list(binding_variables()):
            problems.append("binding_variables_changed")
        for name, value in current["environment"].items():
            if recorded_environment.get(name) != value:
                problems.append(f"binding_changed:environment:{name}")
    selection = manifest["selection"]
    try:
        resolved = current_selection(environment)
    except Exception as exc:
        return [*problems, f"selection_unavailable:{type(exc).__name__}"]
    if not isinstance(selection, dict):
        problems.append("selection_changed")
    else:
        for key, value in resolved.items():
            if selection.get(key) != value:
                problems.append(f"selection_changed:{key}")
    return problems


def load_manifest(path: str | Path, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Read one manifest with a byte bound and refuse a stale one."""
    from comsol_mcp.durable.io import read_file_bytes_bounded

    document: dict[str, Any] = json.loads(
        read_file_bytes_bounded(Path(path), max_bytes=MAX_MANIFEST_BYTES)
    )
    problems = verify_manifest(document, environ)
    if problems:
        raise ManifestStaleError(problems)
    return document


def write_manifest(path: str | Path, manifest: Mapping[str, Any]) -> None:
    """Publish one manifest atomically."""
    from comsol_mcp.durable.io import atomic_write_bytes

    atomic_write_bytes(Path(path), manifest_bytes(manifest))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate or verify a discovery manifest.")
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate")
    generate.add_argument("--output", required=True, type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    if args.command == "generate":
        manifest = build_manifest()
        write_manifest(args.output, manifest)
        summary = {
            "output": str(args.output),
            "manifest_sha256": manifest["manifest_sha256"],
            "bytes": len(manifest_bytes(manifest)),
            "profile": manifest["selection"]["profile"],
            "enabled_features": manifest["selection"]["enabled_features"],
            "pagination_enabled": manifest["selection"]["pagination_enabled"],
            "tool_count": manifest["selection"]["tool_count"],
            "responses": len(manifest["responses"]),
        }
        print(json.dumps(summary, sort_keys=True))
        return 0
    try:
        load_manifest(args.path)
    except ManifestStaleError as exc:
        print(json.dumps({"fresh": False, "problems": exc.problems}))
        return 1
    print(json.dumps({"fresh": True, "problems": []}))
    return 0


__all__ = [
    "MANIFEST_SCHEMA_NAME",
    "MANIFEST_SCHEMA_VERSION",
    "MAX_MANIFEST_BYTES",
    "MAX_RESULT_BYTES",
    "MAX_TOOLS_LIST_PAGES",
    "ManifestCaptureError",
    "ManifestStaleError",
    "binding_variables",
    "build_manifest",
    "current_binding",
    "current_selection",
    "load_manifest",
    "manifest_bytes",
    "verify_manifest",
    "write_manifest",
]


if __name__ == "__main__":
    raise SystemExit(main())
