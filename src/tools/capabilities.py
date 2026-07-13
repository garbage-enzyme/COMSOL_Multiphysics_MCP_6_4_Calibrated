"""Machine-readable capability reporting for a static MCP profile."""

from mcp.server.fastmcp import FastMCP

from src.evidence.contracts import (
    EVIDENCE_STATES,
    PHYSICAL_EVIDENCE_SCHEMA_NAME,
    PHYSICAL_EVIDENCE_SCHEMA_VERSION,
    VALIDATION_POLICY_SCHEMA_NAME,
    VALIDATION_POLICY_SCHEMA_VERSION,
    example_validation_policies,
)
from .catalog import PROFILE_NAMES, TOOL_METADATA
from .profiles import (
    DEFAULT_PROFILE,
    PROFILE_DESCRIPTIONS,
    PROFILE_MATURITY,
    ProfileSelection,
    resolve_profile,
    tool_names_for_profile,
)
from .session import session_manager
from src.knowledge.semantic_runtime import semantic_capability_status


def _profile_inventory(selection: ProfileSelection) -> dict:
    enabled_names = tool_names_for_profile(selection.name)
    all_groups = {metadata.group for metadata in TOOL_METADATA.values()}
    enabled_groups = {
        TOOL_METADATA[name].group for name in enabled_names
    }
    available_profiles = []
    for profile in PROFILE_NAMES:
        names = tool_names_for_profile(profile)
        available_profiles.append({
            "name": profile,
            "maturity": PROFILE_MATURITY[profile],
            "tool_count": len(names),
            "starts_solver": any(TOOL_METADATA[name].starts_solver for name in names),
            "description": PROFILE_DESCRIPTIONS[profile],
        })
    return {
        "active_profile": selection.name,
        "available_profiles": available_profiles,
        "enabled_tool_groups": sorted(enabled_groups),
        "disabled_tool_groups": sorted(all_groups - enabled_groups),
        "tool_count": len(enabled_names),
        "profile_source": {
            "environment_variable": selection.environment_variable,
            "default_used": selection.default_used,
            "source": selection.source,
        },
        "profile_restart_required": True,
    }


def get_capabilities(selection: ProfileSelection | None = None) -> dict:
    """Describe supported, experimental, and disabled behavior without startup."""
    active_selection = selection or resolve_profile()
    status = session_manager.get_status()
    semantic_profile_active = active_selection.name in {"semantic_docs", "full"}
    semantic = semantic_capability_status(profile_active=semantic_profile_active)
    result = {
        "success": True,
        "profile": active_selection.name,
        "targets": {
            "comsol": "6.4+",
            "mph": "1.3.1 standalone clientapi",
        },
        "session": {
            "connected": bool(status.get("connected")),
            "starting": bool(status.get("starting")),
        },
        "verified": [
            "session_status_and_idempotent_start",
            "model_load_create_clone_save",
            "parameters",
            "geometry",
            "physics_and_multiphysics",
            "mesh",
            "study",
            "results_transport",
            "staged_csv_workflows",
            "bounded_lexical_manual_search",
            "solver_ownership_and_preflight",
            "durable_background_staged_sweep_jobs",
            "durable_job_real_cancellation_and_resume",
            "wave_optics_read_only_preflight",
            "wave_optics_one_point_policy_separated_audit",
            "versioned_physical_evidence_contract",
            "solver_free_material_expression_preview",
        ],
        "experimental": {
            "async_solver": {
                "progress": "synthetic checkpoints, not COMSOL solver percentage",
                "cancellation": (
                    "cooperative Python flag; does not interrupt a blocking "
                    "COMSOL study.run()"
                ),
            },
            "semantic_manual_search": semantic,
        },
        "disabled_by_default": [
            "pdf_search",
            "pdf_search_status",
            "pdf_list_modules",
            *([] if semantic_profile_active else [
                "semantic_search", "semantic_status", "semantic_worker_reset",
            ]),
        ],
        "profile_guidance": {
            "default_profile": DEFAULT_PROFILE,
            "wave_optics_recommended_profile": "wave_optics",
            "semantic_docs_opt_in_profile": "semantic_docs",
            "backward_compatibility_profile": "full",
            "selection_environment_variable": "COMSOL_MCP_PROFILE",
            "restart_required": True,
        },
        "wave_optics_audit": {
            "preflight_tool": "wave_optics_preflight",
            "point_tool": "wave_optics_point_audit",
            "material_expression_tool": "wave_optics_material_expression_preview",
            "profiles": ["wave_optics", "full"],
            "default_assessment": "evidence_only",
            "explicit_policy_supported": True,
            "source_immutability": True,
            "durable_one_row_artifacts": True,
        },
        "physical_evidence_contract": {
            "schema_name": PHYSICAL_EVIDENCE_SCHEMA_NAME,
            "schema_version": PHYSICAL_EVIDENCE_SCHEMA_VERSION,
            "evidence_states": sorted(EVIDENCE_STATES),
            "policy_schema_name": VALIDATION_POLICY_SCHEMA_NAME,
            "policy_schema_version": VALIDATION_POLICY_SCHEMA_VERSION,
            "portable_example_policies": sorted(example_validation_policies()),
            "legacy_point_audit_semantics": "preserved_without_reinterpretation",
        },
        "manual_search": {
            "backend": "sqlite_fts5_bm25",
            "isolated_worker": True,
            "hard_deadline": True,
            "semantic_embeddings": False,
        },
        "semantic_search": semantic,
        "long_jobs": {
            "durable_background_jobs": True,
            "job_types": ["staged_sweep"],
            "control_tools": ["job_submit", "job_status", "job_tail", "job_cancel", "job_resume"],
            "cancellation_scope": "same-host durable staged_sweep jobs owned by this runtime root",
            "cancellation_strategy": (
                "attempt-bound native cancellation on the verified COMSOL 6.4.0.293 profile; "
                "exact-identity owned-process fallback elsewhere; cancelled is committed only after "
                "worker/descendant/port/lease cleanup verification"
            ),
            "external_solver_ownership": True,
            "real_cancellation": True,
            "native_cancel_profile": "comsol-6.4.0.293-progress-context-20260712",
            "cross_host_cancellation": False,
            "staged_csv_resume": True,
        },
        "restart_required_after_source_changes": True,
    }
    result.update(_profile_inventory(active_selection))
    return result


def startup_capability_summary(selection: ProfileSelection | None = None) -> str:
    """Return a compact startup summary without initializing external services."""
    capabilities = get_capabilities(selection)
    targets = capabilities["targets"]
    return (
        f"profile={capabilities['profile']}; "
        f"tools={capabilities['tool_count']}; "
        f"target=COMSOL {targets['comsol']} / MPh {targets['mph']}; "
        f"lexical_manual=enabled; semantic_docs={'active' if capabilities['semantic_search']['profile_active'] else 'disabled'}; "
        "durable_jobs=staged_sweep; "
        "solver_ownership=enforced; durable_job_cancellation=verified"
    )


def register_capability_tools(mcp: FastMCP) -> None:
    """Register dependency-free server capability tools."""
    selection = getattr(mcp, "profile_selection", None) or resolve_profile()

    @mcp.tool()
    def capabilities() -> dict:
        """
        Report the active static tool profile and the maturity of risky operations.

        This read-only call does not start COMSOL or initialize PDF/ML services.
        """
        return get_capabilities(selection)
