"""Machine-readable capability reporting for a static MCP profile."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
import hashlib
import json
from pathlib import Path
import time

from mcp.server.fastmcp import FastMCP

from src.evidence.contracts import (
    EVIDENCE_STATES,
    PHYSICAL_EVIDENCE_SCHEMA_NAME,
    PHYSICAL_EVIDENCE_SCHEMA_VERSION,
    VALIDATION_POLICY_SCHEMA_NAME,
    VALIDATION_POLICY_SCHEMA_VERSION,
    example_validation_policies,
)
from src.evidence.visual_review import (
    VISUAL_CAPABILITY_SCHEMA,
    VISUAL_RECEIPT_SCHEMA,
    VISUAL_REQUEST_SCHEMA,
    VISUAL_REVIEW_SCHEMA_VERSION,
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
from src.utils.control_plane import attach_control_plane_evidence


_DEPLOYMENT_MANIFEST = Path(__file__).resolve().parents[1] / "deployment_manifest.json"


def _catalog_contract_sha256() -> str:
    payload = {
        "profiles": {
            profile: sorted(tool_names_for_profile(profile))
            for profile in PROFILE_NAMES
        },
        "tools": {
            name: TOOL_METADATA[name].to_dict()
            for name in sorted(TOOL_METADATA)
        },
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _deployment_identity() -> dict:
    try:
        manifest = json.loads(_DEPLOYMENT_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "schema_name": "comsol_mcp.deployment_identity",
            "schema_version": "1.0.0",
            "available": False,
            "source_classification": "unknown",
            "error": f"{type(exc).__name__}: deployment manifest unavailable",
        }
    try:
        package_version = version("comsol-mcp")
    except PackageNotFoundError:
        from src import __version__

        package_version = __version__
    module_path = str(Path(__file__).resolve()).replace("\\", "/").casefold()
    source_classification = (
        "installed_site_package"
        if "/site-packages/" in module_path
        else "source_tree"
    )
    return {
        **manifest,
        "available": True,
        "package_version": package_version,
        "source_classification": source_classification,
        "catalog_contract_sha256": _catalog_contract_sha256(),
        "contains_local_path": False,
    }


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
    started = time.perf_counter()
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
        "deployment_identity": _deployment_identity(),
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
            "durable_background_validation_matrix_jobs",
            "durable_job_real_cancellation_and_resume",
            "wave_optics_read_only_preflight",
            "wave_optics_one_point_policy_separated_audit",
            "versioned_physical_evidence_contract",
            "solver_free_material_expression_preview",
            "solver_free_visual_review_contracts",
            "locale_safe_read_only_field_dataset_discovery",
            "bounded_control_plane_latency_and_outcome_evidence",
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
            "reference_tool": "wave_optics_reference_audit",
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
        "visual_review_contract": {
            "schema_version": VISUAL_REVIEW_SCHEMA_VERSION,
            "capability_schema": VISUAL_CAPABILITY_SCHEMA,
            "request_schema": VISUAL_REQUEST_SCHEMA,
            "receipt_schema": VISUAL_RECEIPT_SCHEMA,
            "tools": [
                "visual_review_capability_normalize",
                "visual_review_request_create",
                "visual_review_receipt_create",
                "visual_review_dual_evaluate",
            ],
            "host_delivery_required": True,
            "known_answer_calibration_required": True,
            "numerical_policy_authority": False,
        },
        "field_evidence": {
            "dataset_discovery_tool": "wave_optics_field_datasets",
            "existing_dataset_extraction_tool": "wave_optics_field_extract",
            "profiles": ["wave_optics", "full"],
            "dataset_name_transport": "live_mph_unicode_name",
            "dataset_identity": "exact_clientapi_tag_and_solution_readback",
            "study_run": False,
            "model_mutation": False,
            "artifact_extraction": "owned_ascii_runtime_npz_and_manifest",
            "caller_selected_artifact_path": False,
            "png_rendering": "not_yet_public",
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
            "job_types": ["staged_sweep", "validation_matrix"],
            "control_tools": ["job_submit", "job_status", "job_tail", "job_cancel", "job_resume"],
            "cancellation_scope": (
                "same-host durable staged_sweep and validation_matrix jobs "
                "owned by this runtime root"
            ),
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
            "validation_matrix_exact_identity_resume": True,
            "validation_matrix_max_points": 32,
        },
        "restart_required_after_source_changes": True,
    }
    result.update(_profile_inventory(active_selection))
    return attach_control_plane_evidence("capabilities", started, result)


def startup_capability_summary(selection: ProfileSelection | None = None) -> str:
    """Return a compact startup summary without initializing external services."""
    capabilities = get_capabilities(selection)
    targets = capabilities["targets"]
    return (
        f"profile={capabilities['profile']}; "
        f"tools={capabilities['tool_count']}; "
        f"target=COMSOL {targets['comsol']} / MPh {targets['mph']}; "
        f"lexical_manual=enabled; semantic_docs={'active' if capabilities['semantic_search']['profile_active'] else 'disabled'}; "
        "durable_jobs=staged_sweep,validation_matrix; "
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
