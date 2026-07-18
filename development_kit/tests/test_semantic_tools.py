"""semantic profile static profile, public schema, configuration, and degradation gates."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from mcp.server.fastmcp import FastMCP
import pytest

from src.knowledge.semantic_runtime import SemanticService, semantic_configuration
from src.server import create_server
from src.tools.semantic_docs import register_semantic_doc_tools


@pytest.fixture
def lightweight_deployment():
    root = Path("D:/comsol_semantic_profile_test") / uuid.uuid4().hex
    index = root / "indexes" / "corpus" / "model" / "build-1"
    model = root / "models" / "model" / "r1"
    lexical = root / "lexical" / "manuals.sqlite3"
    index.mkdir(parents=True)
    model.mkdir(parents=True)
    lexical.parent.mkdir(parents=True)
    lexical.write_bytes(b"not-opened-by-lightweight-status")
    manifest = {
        "build_id": "build-1",
        "corpus_fingerprint": "a" * 64,
        "model_id": "test/model",
        "model_revision": "r1",
        "model_fingerprint": "b" * 64,
    }
    (index / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (model / "model_manifest.json").write_text(json.dumps({
        "model_sha256": "b" * 64,
    }), encoding="utf-8")
    (root / "current.json").write_text(json.dumps({
        "index_path": str(index),
        "manifest_sha256": "c" * 64,
        "build_id": "build-1",
        "model_fingerprint": "b" * 64,
    }), encoding="utf-8")
    environment = {
        "COMSOL_SEMANTIC_ROOT": str(root),
        "COMSOL_SEMANTIC_LEXICAL_INDEX": str(lexical),
        "COMSOL_SEMANTIC_MODEL_PATH": str(model),
    }
    try:
        yield environment
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_configuration_is_ascii_static_and_missing_model_degrades(lightweight_deployment):
    configured = semantic_configuration(lightweight_deployment)
    missing = semantic_configuration({
        "COMSOL_SEMANTIC_ROOT": lightweight_deployment["COMSOL_SEMANTIC_ROOT"],
        "COMSOL_SEMANTIC_LEXICAL_INDEX": lightweight_deployment["COMSOL_SEMANTIC_LEXICAL_INDEX"],
    })

    assert configured["configured"] is True
    assert configured["missing"] == []
    assert missing["configured"] is False
    assert "model_path_configuration" in missing["missing"]
    with pytest.raises(ValueError, match="ASCII"):
        semantic_configuration({"COMSOL_SEMANTIC_ROOT": "C:/Users/陆星/semantic"})


def test_cold_status_is_solver_free_and_does_not_start_worker(lightweight_deployment):
    service = SemanticService(lightweight_deployment)
    status = service.status(warm=False)

    assert status["success"] is True
    assert status["configured"] is True
    assert status["deployment"]["lightweight_identity_match"] is True
    assert status["worker"] == {"state": "stopped", "health": None}
    assert status["health_gate_passed"] is False
    assert status["available"] is False
    assert status["solver_free"] is True
    assert service._manager is None


def test_failed_warm_health_degrades_without_leaving_worker(lightweight_deployment):
    service = SemanticService(lightweight_deployment)
    status = service.status(warm=True)

    assert status["success"] is True
    assert status["available"] is False
    assert status["health_gate_passed"] is False
    assert status["worker"]["health"]["success"] is False
    assert status["worker"]["state"] == "stopped"
    assert service._manager is not None
    assert service._manager.status()["state"] == "stopped"


def test_unconfigured_search_returns_explicit_lexical_fallback(tmp_path):
    service = SemanticService({
        "COMSOL_SEMANTIC_ROOT": "D:/missing-semantic-root",
        "COMSOL_SEMANTIC_LEXICAL_INDEX": "D:/missing-semantic-lexical.sqlite3",
    })
    result = service.search("CopyFace")

    assert result["success"] is False
    assert result["error"]["code"] == "semantic_unavailable"
    assert result["fallback_tool"] == "manual_search"
    assert service._manager is None
    assert service.reset()["reset"] is False


def test_public_tool_schemas_expose_queries_filters_and_controls_but_no_paths(monkeypatch):
    calls = []

    class FakeService:
        def search(self, query, **kwargs):
            calls.append(("search", query, kwargs))
            return {"success": True, "results": []}

        def status(self, *, warm=False):
            calls.append(("status", warm))
            return {"success": True, "available": warm}

        def reset(self):
            calls.append(("reset",))
            return {"success": True}

    import src.tools.semantic_docs as module
    monkeypatch.setattr(module, "get_semantic_service", lambda: FakeService())
    server = FastMCP("semantic-tools-test")
    register_semantic_doc_tools(server)
    tools = server._tool_manager._tools

    result = tools["semantic_search"].fn(
        "periodic port", module="Wave_Optics_Module", limit=3,
        source=None, page_start=10, page_end=20,
    )
    status = tools["semantic_status"].fn(warm=True)
    reset = tools["semantic_worker_reset"].fn()
    schemas = {tool.name: tool.inputSchema for tool in asyncio.run(server.list_tools())}
    serialized = json.dumps(schemas, sort_keys=True)

    assert result["success"] is status["success"] is reset["success"] is True
    assert calls[0][0] == "search" and calls[0][2]["page_start"] == 10
    assert set(schemas) == {"semantic_search", "semantic_status", "semantic_worker_reset"}
    assert "deployment_root" not in serialized
    assert "model_path" not in serialized
    assert "index_path" not in serialized
    assert "rebuild" not in serialized
    assert "delete" not in serialized


def test_semantic_profile_discovery_is_static_and_parent_imports_no_ml_stack():
    code = """
import asyncio, json, sys
from src.server import create_server
server = create_server('semantic-profile-subprocess', profile='semantic_docs')
names = sorted(tool.name for tool in asyncio.run(server.list_tools()))
assert len(names) == 44
assert {'semantic_search','semantic_status','semantic_worker_reset'} <= set(names)
for name in ('chromadb','torch','sentence_transformers'):
    assert name not in sys.modules, name
status = server._tool_manager._tools['semantic_status'].fn(False)
assert status['worker']['state'] == 'stopped'
print(json.dumps({'count': len(names), 'configured': status['configured']}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["count"] == 44


def test_default_and_other_research_profiles_remain_unchanged():
    counts = {}
    for profile in ("core", "basic_fem", "wave_optics", "experimental", "full"):
        server = create_server(f"semantic-profile-{profile}", profile=profile)
        counts[profile] = len(asyncio.run(server.list_tools()))

    assert counts == {
        "core": 41,
        "basic_fem": 79,
        "wave_optics": 66,
        "experimental": 67,
        "full": 133,
    }
