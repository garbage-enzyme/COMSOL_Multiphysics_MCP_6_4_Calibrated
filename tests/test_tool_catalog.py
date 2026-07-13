"""Compatibility gates for the pre-H3 MCP discovery surface."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys

from mcp.server.fastmcp import FastMCP

from src.knowledge.embedded import register_knowledge_tools
from src.knowledge.lexical_manual import register_lexical_manual_tools
from src.server import create_server
from src.tools import TOOL_REGISTRARS
from src.tools.catalog import (
    PROFILE_NAMES,
    TOOL_METADATA,
    get_tool_metadata,
    snapshot_tool_schemas,
)


SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "full_tool_schemas.json"
PRE_H3_SNAPSHOT_PATH = (
    Path(__file__).parent / "snapshots" / "pre_h3_tool_schemas.json"
)


def test_full_tool_schema_snapshot_is_stable():
    server = create_server("full-schema-snapshot-test", profile="full")
    actual = asyncio.run(snapshot_tool_schemas(server))
    expected = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))

    assert len(actual) == 108
    assert actual == expected


def test_pre_h3_compatibility_snapshot_is_preserved():
    legacy = json.loads(PRE_H3_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    current = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))

    assert len(legacy) == 96
    assert set(legacy) <= set(current)
    assert legacy["geometry_add_feature"]["properties"]["kwargs"]["type"] == "string"
    assert "kwargs" not in current["geometry_add_feature"]["properties"]
    assert current["geometry_add_feature"]["properties"]["properties"]["anyOf"]


def test_registered_tool_names_are_unique():
    server = create_server("unique-tool-name-test", profile="full")
    tools = asyncio.run(server.list_tools())
    names = [tool.name for tool in tools]

    assert len(names) == len(set(names))


def test_every_registered_tool_has_complete_canonical_metadata():
    expected_names = set(json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8")))

    assert set(TOOL_METADATA) == expected_names
    assert len(TOOL_METADATA) == 108
    for name, metadata in TOOL_METADATA.items():
        assert metadata.name == name
        assert metadata.registrar.startswith("src.")
        assert metadata.group
        assert metadata.maturity in {"verified", "experimental"}
        assert metadata.side_effect_class
        assert isinstance(metadata.starts_solver, bool)
        assert metadata.intended_profiles
        assert set(metadata.intended_profiles) <= set(PROFILE_NAMES)
        assert "full" in metadata.intended_profiles


def test_unknown_tool_metadata_fails_closed():
    try:
        get_tool_metadata("not_a_registered_tool")
    except KeyError as exc:
        assert "No canonical metadata" in str(exc)
    else:
        raise AssertionError("unknown tools must not receive implicit metadata")


def test_metadata_registrars_match_actual_registration():
    registrars = (*TOOL_REGISTRARS, register_knowledge_tools, register_lexical_manual_tools)

    for registrar in registrars:
        server = FastMCP(f"metadata-{registrar.__name__}")
        registrar(server)
        registrar_name = f"{registrar.__module__}.{registrar.__name__}"
        expected = {
            name for name, metadata in TOOL_METADATA.items()
            if metadata.registrar == registrar_name
        }
        assert set(server._tool_manager._tools) == expected


def test_catalog_import_cannot_start_comsol():
    code = """
import mph
mph.Client = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('Client called'))
from src.tools.catalog import TOOL_METADATA
assert len(TOOL_METADATA) == 108
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
