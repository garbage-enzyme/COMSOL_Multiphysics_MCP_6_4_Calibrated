"""Machine-readable capability reporting for a static MCP profile."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import time
import os

from mcp.server.fastmcp import FastMCP

from src.build_identity import get_build_identity
from src.compatibility import load_runtime_compatibility
from src.environment_identity import get_environment_identity
from src.durable import canonical_sha256_v1
from .catalog import PROFILE_NAMES, TOOL_METADATA
from .profiles import (
    DEFAULT_PROFILE,
    PROFILE_DESCRIPTIONS,
    PROFILE_MATURITY,
    ProfileSelection,
    resolve_profile,
    tool_names_for_profile,
)
from .session_status import get_session_status
from src.utils.control_plane import attach_control_plane_evidence
from src.path_policy import PathPolicy
from src.settings import SETTINGS_PATH_ENV, settings_status
from src.shared_session.contracts import (
    SHARED_SERVER_FEATURE_ENV,
    SHARED_SERVER_PROFILE,
    normalize_shared_server_feature_gate,
)


ARTIFACT_CHAIN_SCHEMA = "comsol_mcp.artifact_chain"
ARTIFACT_CHAIN_SCHEMA_VERSION = "1.0.0"
PHYSICAL_EVIDENCE_SCHEMA_NAME = "comsol_mcp.physical_evidence"
PHYSICAL_EVIDENCE_SCHEMA_VERSION = "1.1.0"
VALIDATION_POLICY_SCHEMA_NAME = "comsol_mcp.validation_policy"
VALIDATION_POLICY_SCHEMA_VERSION = "1.0.0"
EVIDENCE_STATES = (
    "derived_from_declared_convention",
    "label_only",
    "measured",
    "not_applicable",
    "not_requested",
    "unknown",
)
VISUAL_CAPABILITY_SCHEMA = "comsol_mcp.visual_reviewer_capability"
VISUAL_REQUEST_SCHEMA = "comsol_mcp.visual_review_request"
VISUAL_RECEIPT_SCHEMA = "comsol_mcp.visual_review_receipt"
VISUAL_REVIEW_SCHEMA_VERSION = "1.0.0"
MAX_SPECTRAL_POINTS = 1024
MAX_REFINEMENT_STAGES = 8
MAX_WINDOW_EXPANSIONS = 8
MAX_CONVERGENCE_CAMPAIGN_LEVELS = 8
MAX_CONVERGENCE_CAMPAIGN_POINTS = 512
MAX_BRANCH_CONTINUATION_STATES = 16
MAX_BRANCH_CONTINUATION_POINTS = 512
_PORTABLE_POLICY_NAMES = (
    "declared_flux_closure",
    "mesh_evidence_presence",
    "passive_rta_bounds",
    "reference_air_polarization_ratio",
    "wavelength_synchronization",
)


class _LightweightSessionStatus:
    def get_status(self) -> dict[str, bool]:
        return get_session_status()


session_manager = _LightweightSessionStatus()


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
    return canonical_sha256_v1(payload)


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
    module_path = str(Path(__file__).resolve()).replace("\\", "/").casefold()
    source_classification = (
        "installed_site_package"
        if "/site-packages/" in module_path
        else "source_tree"
    )
    if source_classification == "source_tree":
        from src import __version__

        package_version = __version__
    else:
        try:
            package_version = version("comsol-mcp")
        except PackageNotFoundError:
            from src import __version__

            package_version = __version__
    return {
        **manifest,
        "available": True,
        "package_version": package_version,
        "build_identity": get_build_identity(),
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
    from src.evidence.integrity_controls import evidence_integrity_capability
    from src.knowledge.semantic_runtime import semantic_capability_status
    from src.schema_registry import get_schema_registry

    started = time.perf_counter()
    active_selection = selection or resolve_profile()
    status = session_manager.get_status()
    semantic_profile_active = active_selection.name in {"semantic_docs", "full"}
    semantic = semantic_capability_status(profile_active=semantic_profile_active)
    compatibility = load_runtime_compatibility()
    shared_gate = normalize_shared_server_feature_gate(active_selection.name)
    accepted_lane = compatibility["licensed_acceptance"][0]
    result = {
        "success": True,
        "profile": active_selection.name,
        "targets": {
            "comsol": accepted_lane["comsol_build"],
            "mph": accepted_lane["mph_version"],
            "acceptance": "exact_licensed_acceptance",
        },
        "runtime_compatibility": compatibility,
        "environment_identity": get_environment_identity(),
        "schema_registry": get_schema_registry(),
        "artifact_chain_verification": {
            "schema_name": ARTIFACT_CHAIN_SCHEMA,
            "schema_version": ARTIFACT_CHAIN_SCHEMA_VERSION,
            "solver_free": True,
            "content_validation": "schema_identity_and_hash_chain",
            "path_redacted_receipt": True,
        },
        "evidence_integrity": evidence_integrity_capability(),
        "project_settings": settings_status(),
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
            "durable_adaptive_spectral_characterization_jobs",
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
                "profiles": ["experimental", "full"],
                "recommended_profile_exposure": False,
                "durable_alternative": "job_submit/job_status/job_cancel/job_resume",
            },
            "semantic_manual_search": semantic,
        },
        "disabled_by_default": [
            *([] if semantic_profile_active else [
                "semantic_search", "semantic_status", "semantic_worker_reset",
            ]),
        ],
        "profile_guidance": {
            "default_profile": DEFAULT_PROFILE,
            "wave_optics_recommended_profile": "wave_optics",
            "semantic_docs_opt_in_profile": "semantic_docs",
            "backward_compatibility_profile": "full",
            "selection_settings_key": "profile.name",
            "settings_path_environment_variable": SETTINGS_PATH_ENV,
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
            "portable_example_policies": list(_PORTABLE_POLICY_NAMES),
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
            "png_rendering": "isolated_bundle_renderer_available_not_yet_public",
            "paired_png_shared_color_limits_required": True,
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
            "job_types": [
                "staged_sweep",
                "validation_matrix",
                "spectral_characterization",
                "convergence_campaign",
                "branch_continuation_campaign",
            ],
            "control_tools": ["job_submit", "job_status", "job_tail", "job_cancel", "job_resume"],
            "cancellation_scope": (
                "same-host durable staged_sweep, validation_matrix, and "
                "spectral_characterization, convergence_campaign, and "
                "branch_continuation_campaign jobs "
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
            "validation_matrix_collectors": [
                "wave_optics_point_audit",
                "wave_optics_reference_audit",
                "wave_optics_field_evidence",
            ],
            "field_collector_requires_preceding_point_audit": True,
            "spectral_characterization": {
                "one_wavelength_per_solve": True,
                "complete_audit_fsync_before_next_point": True,
                "exact_identity_resume": True,
                "maximum_points_server_cap": MAX_SPECTRAL_POINTS,
                "maximum_refinement_stages_server_cap": MAX_REFINEMENT_STAGES,
                "maximum_expansions_server_cap": MAX_WINDOW_EXPANSIONS,
                "scientific_tolerances": "caller_declared_and_hash_bound",
                "normal_nonacceptance": [
                    "no_candidate",
                    "boundary_high_at_declared_cap",
                    "unbracketed_fwhm",
                    "fit_sensitive",
                ],
            },
            "convergence_campaign": {
                "composes_spectral_characterization": True,
                "composes_offline_convergence_evaluation": True,
                "exact_model_levels_only": True,
                "maximum_levels_server_cap": MAX_CONVERGENCE_CAMPAIGN_LEVELS,
                "maximum_total_points_server_cap": MAX_CONVERGENCE_CAMPAIGN_POINTS,
                "one_solver_owner_per_campaign": True,
                "own_peak_comparison": True,
                "early_acceptance_requires_explicit_policy": True,
                "undeclared_level_creation": False,
            },
            "branch_continuation_campaign": {
                "composes_spectral_characterization": True,
                "composes_offline_branch_continuation_planning": True,
                "exact_model_states_only": True,
                "maximum_states_server_cap": MAX_BRANCH_CONTINUATION_STATES,
                "maximum_total_points_server_cap": MAX_BRANCH_CONTINUATION_POINTS,
                "one_solver_owner_per_campaign": True,
                "own_peak_continuation": True,
                "exact_incidence_readback_required": True,
                "branch_disappearance_claimed": False,
                "undeclared_coordinate_creation": False,
            },
        },
        "restart_required_after_source_changes": True,
        "server_safety": {
            "operation_arbitration": {
                "schema_name": "comsol_mcp.operation_lock",
                "schema_version": "1.0.0",
                "comsol_bound_serialized": True,
                "control_plane_remains_available": True,
                "cross_process_runtime_lock": True,
            },
            "path_policy": PathPolicy.from_environment().capability(
                enforced=active_selection.name != "full"
            ),
            "model_revision_policy": {
                "required_for_verified_mutation_and_solve": (
                    active_selection.name != "full"
                ),
                "checked_after_operation_lock_acquisition": True,
                "successful_mutations_advance_revision": True,
                "compatibility_profile_enforcement": False,
            },
            "compatibility_profile": "full",
            "compatibility_profile_weaker_guarantees": True,
        },
        "shared_session": {
            "profile": SHARED_SERVER_PROFILE,
            "profile_active": active_selection.name == SHARED_SERVER_PROFILE,
            "feature_flag": SHARED_SERVER_FEATURE_ENV,
            "feature_enabled": shared_gate.feature_enabled,
            "gate_open": shared_gate.gate_open,
            "maturity": "experimental",
            "endpoint_scope": "local_loopback_only",
            "server_ownership": "external_user_owned",
            "can_start_comsol": False,
            "model_scope": "one_exact_server_model",
            "durable_execution": {
                "available": bool(
                    active_selection.name == SHARED_SERVER_PROFILE
                    and shared_gate.gate_open
                ),
                "execution_backend": "attached_shared_server",
                "job_types": ["staged_sweep"],
                "control_tools": [
                    "job_submit",
                    "job_status",
                    "job_tail",
                    "job_cancel",
                    "job_resume",
                ],
                "requires_automation_exclusive_handoff": True,
                "requires_immutable_source": True,
                "checkpoint_save_copy": True,
                "exact_durable_revision_resume": True,
                "external_server_is_termination_target": False,
                "terminal_completion_requires_preservation_receipt": True,
            },
            "restart_required_after_change": True,
        },
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
        f"target=COMSOL {targets['comsol']} exact licensed / MPh {targets['mph']}; "
        f"lexical_manual=enabled; semantic_docs={'active' if capabilities['semantic_search']['profile_active'] else 'disabled'}; "
        "durable_jobs=staged_sweep,validation_matrix,spectral_characterization,"
        "convergence_campaign,branch_continuation_campaign; "
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
