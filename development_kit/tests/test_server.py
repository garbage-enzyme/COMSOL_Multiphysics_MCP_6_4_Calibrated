"""Tests for MCP server construction without starting a transport."""

import ast
import asyncio
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import src.server as server_module
from mcp.server.mcpserver import MCPServer
from src.server import (
    SERVER_INSTRUCTIONS,
    create_server,
    register_all_resources,
    register_all_tools,
)
from src.tools.capabilities import get_capabilities, startup_capability_summary


def _public_tool_names(server) -> set[str]:
    return {tool.name for tool in asyncio.run(server.list_tools())}


def _public_resource_uris(server) -> set[str]:
    return {str(resource.uri) for resource in asyncio.run(server.list_resources())}


def test_server_advertises_bounded_legacy_compatible_safety_instructions():
    server = create_server("instructions-test")

    assert server.instructions == SERVER_INSTRUCTIONS
    assert len(SERVER_INSTRUCTIONS.encode("utf-8")) < 512
    assert "capabilities and solver_preflight" in SERVER_INSTRUCTIONS
    assert "unless the user explicitly requests it" in SERVER_INSTRUCTIONS
    assert "source models as read-only" in SERVER_INSTRUCTIONS
    assert "scientific validation as separate outcomes" in SERVER_INSTRUCTIONS


def test_server_module_configures_logging_only_in_main():
    source = (Path(__file__).parents[2] / "comsol_mcp" / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert not any(
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "basicConfig"
        for node in tree.body
    )
    main = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "basicConfig"
        for node in ast.walk(main)
    )


def test_server_registration_is_idempotent():
    server = create_server("registration-test")
    tool_names = _public_tool_names(server)
    resource_uris = _public_resource_uris(server)

    assert "comsol_start" in tool_names
    assert "capabilities" in tool_names
    assert "session_clear_models" not in tool_names
    assert "session_reset" in tool_names
    assert "solver_status" in tool_names
    assert "solver_preflight" in tool_names
    assert "solver_recover_stale_lease" in tool_names
    assert {"job_submit", "job_status", "job_tail", "job_cancel", "job_resume"} <= tool_names
    assert "model_load" in tool_names
    assert "study_solve" in tool_names
    assert "manual_search" in tool_names
    assert "manual_read_pages" in tool_names
    assert "model_create" not in tool_names
    assert "docs_get" not in tool_names
    assert "wave_optics_preflight" not in tool_names
    assert "wave_optics_point_audit" not in tool_names
    assert resource_uris

    register_all_tools(server)
    register_all_resources(server)

    assert _public_tool_names(server) == tool_names
    assert _public_resource_uris(server) == resource_uris


def test_partial_tool_registration_rolls_back_and_can_be_retried(monkeypatch):
    import src.knowledge.embedded as embedded_module
    import src.knowledge.lexical_manual as lexical_module
    import src.tools as tools_module

    server = MCPServer("transactional-registration")

    @server.tool(name="existing_tool")
    def existing_tool() -> dict:
        return {"success": True}

    original = _public_tool_names(server)

    def fail_after_partial_registration(target, _selection):
        @target.tool(name="partial_tool")
        def partial_tool() -> dict:
            return {"success": True}

        raise RuntimeError("injected registrar failure")

    monkeypatch.setattr(tools_module, "register_tool_modules", fail_after_partial_registration)
    monkeypatch.setattr(embedded_module, "register_knowledge_tools", lambda _server: None)
    monkeypatch.setattr(lexical_module, "register_lexical_manual_tools", lambda _server: None)

    with pytest.raises(RuntimeError, match="registrar failure"):
        register_all_tools(server, "core")

    assert _public_tool_names(server) == original

    def complete_registration(target, _selection):
        @target.tool(name="completed_tool")
        def completed_tool() -> dict:
            return {"success": True}

    monkeypatch.setattr(tools_module, "register_tool_modules", complete_registration)

    selection = register_all_tools(server, "core")

    assert selection.name == "core"
    assert _public_tool_names(server) == {"existing_tool", "completed_tool"}


def test_partial_resource_registration_rolls_back_and_can_be_retried(monkeypatch):
    import src.resources.model_resources as resources_module

    server = MCPServer("transactional-resource-registration")

    @server.resource("fixture://existing")
    def existing_resource() -> str:
        return "existing"

    original = _public_resource_uris(server)

    def fail_after_partial_registration(target):
        @target.resource("fixture://partial")
        def partial_resource() -> str:
            return "partial"

        raise RuntimeError("injected resource registrar failure")

    monkeypatch.setattr(
        resources_module, "register_model_resources", fail_after_partial_registration
    )
    with pytest.raises(RuntimeError, match="registrar failure"):
        register_all_resources(server)
    assert _public_resource_uris(server) == original

    def complete_registration(target):
        @target.resource("fixture://complete")
        def complete_resource() -> str:
            return "complete"

    monkeypatch.setattr(resources_module, "register_model_resources", complete_registration)
    register_all_resources(server)
    assert _public_resource_uris(server) == original | {"fixture://complete"}


def test_model_resources_escape_untrusted_markdown(monkeypatch):
    import src.resources.model_resources as resources_module

    malicious = "node|name\n## Injected *bold* `tick` ~~strike~~"

    class Model:
        def name(self):
            return malicious

        def file(self):
            return malicious

        def version(self):
            return malicious

        def parameters(self):
            return {malicious: "1|2 `value`"}

        def descriptions(self):
            return {malicious: malicious}

        def problems(self):
            return [{"node": malicious, "message": malicious}]

        def physics(self):
            return [malicious]

        def multiphysics(self):
            return [malicious]

        def __getattr__(self, name):
            if name in {
                "components",
                "datasets",
                "exports",
                "functions",
                "geometries",
                "materials",
                "meshes",
                "plots",
                "selections",
                "solutions",
                "studies",
            }:
                return lambda: [malicious]
            raise AttributeError(name)

    monkeypatch.setattr(resources_module.session_manager, "get_model", lambda _name: Model())
    monkeypatch.setattr(
        resources_module.session_manager,
        "get_status",
        lambda: {
            "connected": True,
            "version": malicious,
            "cores": malicious,
            "standalone": True,
            "models": [{"name": malicious, "file": malicious}],
            "current_model": malicious,
        },
    )
    server = MCPServer("escaped-resources")
    resources_module.register_model_resources(server)

    async def read(uri: str) -> str:
        contents = await server.read_resource(uri)
        return "".join(item.content for item in contents)

    session = asyncio.run(read("comsol://session/info"))
    tree = asyncio.run(read("comsol://model/model/tree"))
    parameters = asyncio.run(read("comsol://model/model/parameters"))
    physics = asyncio.run(read("comsol://model/model/physics"))

    for document in (session, tree, parameters, physics):
        assert "\n## Injected" not in document
        assert "\\|" in document
        assert "\\*bold\\*" in document
        assert "\\`tick\\`" in document
        assert "\\~\\~strike\\~\\~" in document
    assert "`` 1\\|2 `value` ``" in parameters


def test_markdown_code_preserves_boundary_backticks():
    import src.resources.model_resources as resources_module

    assert resources_module._markdown_code("`value`") == "`` `value` ``"


def test_default_registration_does_not_import_semantic_stack():
    code = """
import json
import sys
from comsol_mcp.server import create_server

create_server('no-semantic-import-test')
print(json.dumps(sorted(
    name for name in ('chromadb', 'sentence_transformers', 'torch') if name in sys.modules
)))
"""
    environment = os.environ.copy()
    environment.pop("COMSOL_MCP_SETTINGS_PATH", None)
    environment.update(
        {
            "COMSOL_MCP_ENABLE_SEMANTIC_DOCS": "false",
            "COMSOL_MCP_ENABLE_SHARED_SERVER": "false",
            "COMSOL_MCP_PROFILE": "core",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == []


def test_capabilities_report_risky_operations_without_starting_comsol(monkeypatch):
    import src.tools.capabilities as capability_module

    monkeypatch.delenv("COMSOL_MCP_PROFILE", raising=False)
    monkeypatch.setattr(
        capability_module.session_manager,
        "get_status",
        lambda: {"connected": False, "starting": False},
    )

    result = get_capabilities()

    assert result["profile"] == "core"
    assert result["active_profile"] == "core"
    assert result["tool_count"] == 47
    assert result["profile_source"]["default_used"] is True
    assert [item["name"] for item in result["available_profiles"]] == [
        "core",
        "basic_fem",
        "wave_optics",
        "experimental",
        "full",
    ]
    assert result["enabled_features"] == []
    assert [item["name"] for item in result["available_features"]] == [
        "semantic_docs",
        "shared_server",
    ]
    assert result["session"] == {"connected": False, "starting": False}
    assert result["long_jobs"]["real_cancellation"] is True
    assert result["long_jobs"]["durable_background_jobs"] is True
    assert "exact-identity owned-process fallback" in result["long_jobs"]["cancellation_strategy"]
    assert result["long_jobs"]["cross_host_cancellation"] is False
    assert "semantic_search" in result["disabled_by_default"]
    assert result["profile_guidance"]["default_profile"] == "core"
    assert result["profile_guidance"]["wave_optics_recommended_profile"] == "wave_optics"
    assert result["profile_guidance"]["independent_feature_gates"] == {
        "semantic_docs": "COMSOL_MCP_ENABLE_SEMANTIC_DOCS",
        "shared_server": "COMSOL_MCP_ENABLE_SHARED_SERVER",
    }
    assert result["semantic_search"]["feature_enabled"] is False
    assert result["semantic_search"]["available"] is False
    assert result["wave_optics_audit"]["default_assessment"] == "evidence_only"
    assert result["physical_evidence_contract"] == {
        "schema_name": "comsol_mcp.physical_evidence",
        "schema_version": "1.1.0",
        "evidence_states": [
            "derived_from_declared_convention",
            "label_only",
            "measured",
            "not_applicable",
            "not_requested",
            "unknown",
        ],
        "policy_schema_name": "comsol_mcp.validation_policy",
        "policy_schema_version": "1.0.0",
        "portable_example_policies": [
            "declared_flux_closure",
            "mesh_evidence_presence",
            "passive_rta_bounds",
            "reference_air_polarization_ratio",
            "wavelength_synchronization",
        ],
        "legacy_point_audit_semantics": "preserved_without_reinterpretation",
    }
    assert result["visual_review_contract"] == {
        "schema_version": "1.0.0",
        "capability_schema": "comsol_mcp.visual_reviewer_capability",
        "request_schema": "comsol_mcp.visual_review_request",
        "receipt_schema": "comsol_mcp.visual_review_receipt",
        "tools": [
            "visual_review_capability_normalize",
            "visual_review_request_create",
            "visual_review_receipt_create",
            "visual_review_dual_evaluate",
        ],
        "host_delivery_required": True,
        "known_answer_calibration_required": True,
        "numerical_policy_authority": False,
    }


def test_startup_capability_summary_is_compact_and_truthful(monkeypatch):
    import src.tools.capabilities as capability_module

    monkeypatch.delenv("COMSOL_MCP_PROFILE", raising=False)
    monkeypatch.setattr(
        capability_module.session_manager,
        "get_status",
        lambda: {"connected": False},
    )

    summary = startup_capability_summary()

    assert "profile=core" in summary
    assert "tools=47" in summary
    assert "semantic_docs=disabled" in summary
    assert "lexical_manual=enabled" in summary
    assert "durable_jobs=staged_sweep" in summary
    assert "convergence_campaign" in summary
    assert "durable_job_cancellation=verified" in summary


def test_spawn_child_is_not_a_server_transport_entrypoint(monkeypatch):
    monkeypatch.setattr(server_module, "__name__", "__main__")
    monkeypatch.setattr(
        server_module.mp,
        "current_process",
        lambda: type("Process", (), {"name": "SpawnProcess-1"})(),
    )

    assert server_module._is_transport_entrypoint() is False


def test_job_read_tools_are_solver_free(monkeypatch):
    import mph
    import src.tools.jobs as jobs_module
    from src.jobs.manager import JobManager

    root = Path("D:/comsol_runtime_test/jobs") / uuid.uuid4().hex
    try:
        monkeypatch.setattr(jobs_module, "job_manager", JobManager(root))
        monkeypatch.setattr(
            mph,
            "Client",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not start")),
        )
        server = MCPServer("job-read-test")
        jobs_module.register_job_tools(server)

        status = server._tool_manager._tools["job_status"].fn("missing")
        tail = server._tool_manager._tools["job_tail"].fn("missing", 5)

        assert status["success"] is False
        assert tail["success"] is False
    finally:
        shutil.rmtree(root, ignore_errors=True)
