"""Named artifact schema registry and support-resolution tests."""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path
import re

from src import __version__
from src.schema_registry import check_schema_support, get_schema_registry
from src.tools.capabilities import get_capabilities
from src.tools.profiles import ProfileSelection


ROOT = Path(__file__).parents[2]


def _named_schemas_in_source() -> set[str]:
    names: set[str] = set()
    for path in (ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.startswith("comsol_mcp.")
            ):
                names.add(node.value)
    return names


def _selection() -> ProfileSelection:
    return ProfileSelection(
        name="core",
        source="schema-registry-test",
        environment_variable="COMSOL_MCP_PROFILE",
        default_used=False,
    )


def test_registry_is_complete_sorted_and_snapshot_stable():
    registry = get_schema_registry()
    entries = registry["entries"]
    names = [item["schema_name"] for item in entries]

    assert registry["schema_name"] == "comsol_mcp.schema_registry"
    assert registry["schema_version"] == "1.0.0"
    assert registry["producer"] == {"package": "comsol-mcp", "version": __version__}
    assert registry["entry_count"] == len(entries) == 46
    assert names == sorted(names)
    assert len(names) == len(set(names))
    assert set(names) == _named_schemas_in_source()
    assert re.fullmatch(r"[0-9a-f]{64}", registry["registry_sha256"])
    assert registry["registry_sha256"] == get_schema_registry()["registry_sha256"]


def test_every_entry_declares_read_write_and_non_mutating_migration_policy():
    for entry in get_schema_registry()["entries"]:
        assert entry["producer_version"] == __version__
        assert entry["readable_versions"]
        assert len(entry["readable_versions"]) == len(set(entry["readable_versions"]))
        assert entry["writable_version"] is None or entry["writable_version"] in entry["readable_versions"]
        assert entry["migration"]["rewrites_source_in_place"] is False
        assert entry["migration"]["available"] == bool(
            entry["migration"]["source_schema_names"]
        )
    physical = next(
        item
        for item in get_schema_registry()["entries"]
        if item["schema_name"] == "comsol_mcp.physical_evidence"
    )
    assert physical["readable_versions"] == ["1.0.0", "1.1.0"]
    assert physical["writable_version"] == "1.1.0"
    deployment = next(
        item
        for item in get_schema_registry()["entries"]
        if item["schema_name"] == "comsol_mcp.deployment_identity"
    )
    assert deployment["readable_versions"] == ["1.0.0", "1.1.0"]
    assert deployment["writable_version"] == "1.1.0"


def test_support_resolution_accepts_current_and_rejects_future_without_mutation():
    artifact = {
        "schema_name": "comsol_mcp.physical_evidence",
        "schema_version": "1.0.0",
        "payload": {"sentinel": [1, 2, 3]},
    }
    original = deepcopy(artifact)

    accepted = check_schema_support(artifact["schema_name"], artifact["schema_version"])
    future = check_schema_support(artifact["schema_name"], "99.0.0")
    unknown = check_schema_support("comsol_mcp.unknown_artifact", "1.0.0")

    assert accepted["supported"] is True
    assert accepted["reason_code"] == "supported"
    assert future == {
        "supported": False,
        "reason_code": "unsupported_schema_version",
        "schema_name": "comsol_mcp.physical_evidence",
        "schema_version": "99.0.0",
        "supported_versions": ["1.0.0", "1.1.0"],
        "migration_available": True,
    }
    assert unknown["supported"] is False
    assert unknown["reason_code"] == "unknown_schema_name"
    assert artifact == original


def test_capabilities_embed_the_complete_schema_registry():
    capabilities = get_capabilities(_selection())
    assert capabilities["schema_registry"] == get_schema_registry()
