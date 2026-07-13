"""Tests for MCP server construction without starting a transport."""

import sys
from pathlib import Path
import shutil
import uuid

from mcp.server.fastmcp import FastMCP

from src.knowledge.embedded import register_knowledge_tools
import src.server as server_module
from src.server import create_server, register_all_resources, register_all_tools
from src.tools.capabilities import get_capabilities, startup_capability_summary


def test_server_registration_is_idempotent():
    server = create_server("registration-test")
    tool_names = set(server._tool_manager._tools)
    resource_names = set(server._resource_manager._resources)

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
    assert "pdf_search" not in tool_names
    assert "pdf_search_status" not in tool_names
    assert "pdf_list_modules" not in tool_names
    assert resource_names

    register_all_tools(server)
    register_all_resources(server)

    assert set(server._tool_manager._tools) == tool_names
    assert set(server._resource_manager._resources) == resource_names


def test_default_registration_does_not_import_semantic_stack():
    create_server("no-semantic-import-test")

    assert "chromadb" not in sys.modules
    assert "sentence_transformers" not in sys.modules
    assert "torch" not in sys.modules


def test_semantic_pdf_tools_require_explicit_opt_in():
    server = FastMCP("pdf-opt-in-test")

    register_knowledge_tools(server, include_pdf_search=True)

    tool_names = set(server._tool_manager._tools)
    assert {"pdf_search", "pdf_search_status", "pdf_list_modules"} <= tool_names


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
    assert result["tool_count"] == 38
    assert result["profile_source"]["default_used"] is True
    assert [item["name"] for item in result["available_profiles"]] == [
        "core", "basic_fem", "wave_optics", "semantic_docs", "experimental", "full"
    ]
    assert result["session"] == {"connected": False, "starting": False}
    assert result["long_jobs"]["real_cancellation"] is True
    assert result["long_jobs"]["durable_background_jobs"] is True
    assert "exact-identity owned-process fallback" in result["long_jobs"]["cancellation_strategy"]
    assert result["long_jobs"]["cross_host_cancellation"] is False
    assert "pdf_search" in result["disabled_by_default"]
    assert result["profile_guidance"]["default_profile"] == "core"
    assert result["profile_guidance"]["wave_optics_recommended_profile"] == "wave_optics"
    assert result["profile_guidance"]["semantic_docs_opt_in_profile"] == "semantic_docs"
    assert result["semantic_search"]["profile_active"] is False
    assert result["semantic_search"]["available"] is False
    assert result["wave_optics_audit"]["default_assessment"] == "evidence_only"
    assert result["physical_evidence_contract"] == {
        "schema_name": "comsol_mcp.physical_evidence",
        "schema_version": "1.0.0",
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
    assert "tools=38" in summary
    assert "semantic_docs=disabled" in summary
    assert "lexical_manual=enabled" in summary
    assert "durable_jobs=staged_sweep" in summary
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
        monkeypatch.setattr(mph, "Client", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not start")))
        server = FastMCP("job-read-test")
        jobs_module.register_job_tools(server)

        status = server._tool_manager._tools["job_status"].fn("missing")
        tail = server._tool_manager._tools["job_tail"].fn("missing", 5)

        assert status["success"] is False
        assert tail["success"] is False
    finally:
        shutil.rmtree(root, ignore_errors=True)
