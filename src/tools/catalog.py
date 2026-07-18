"""Deterministic inspection helpers for the registered MCP tool surface."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from types import MappingProxyType
from collections.abc import Iterable
from typing import Any, Mapping


PROFILE_NAMES = (
    "core",
    "basic_fem",
    "wave_optics",
    "semantic_docs",
    "desktop_shared",
    "experimental",
    "full",
)


@dataclass(frozen=True)
class ToolSpec:
    """Immutable public contract used to derive every discovery view."""

    name: str
    registrar: str
    group: str
    maturity: str
    side_effect_class: str
    concurrency_class: str
    requires_model_revision: bool
    advances_model_revision: bool
    starts_solver: bool
    intended_profiles: tuple[str, ...]
    input_contract: str
    output_contract: str
    structural_limits: tuple[tuple[str, int], ...] = ()
    artifact_path_classes: tuple[str, ...] = ()
    required_features: tuple[str, ...] = ()
    replacement_tool: str | None = None
    sunset_release: str | None = None
    deprecation_state: str = "active"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Compatibility name retained for one release while callers adopt ToolSpec.
ToolMetadata = ToolSpec


_TOOLS_BY_REGISTRAR = {
    "src.tools.capabilities.register_capability_tools": (
        "capabilities",
    ),
    "src.tools.evidence_integrity.register_evidence_integrity_tools": (
        "evidence_integrity_status", "evidence_integrity_verify",
    ),
    "src.tools.ownership.register_ownership_tools": (
        "solver_status", "solver_preflight", "solver_recover_stale_lease",
    ),
    "src.tools.jobs.register_job_tools": (
        "job_submit", "job_status", "job_tail", "job_cancel", "job_resume",
    ),
    "src.tools.session.register_session_tools": (
        "comsol_start", "comsol_connect", "comsol_disconnect", "comsol_status",
        "session_clear_models", "session_reset",
    ),
    "src.tools.model.register_model_tools": (
        "model_load", "model_create", "model_create_component",
        "model_list_components", "model_save", "model_save_version", "model_list",
        "model_set_current", "model_clone", "model_remove", "model_inspect",
    ),
    "src.tools.parameters.register_parameter_tools": (
        "param_get", "param_set", "param_list", "param_sweep_setup",
        "param_description",
    ),
    "src.tools.geometry.register_geometry_tools": (
        "geometry_list", "geometry_create", "geometry_add_feature",
        "geometry_add_block", "geometry_add_cylinder", "geometry_add_sphere",
        "geometry_add_rectangle", "geometry_add_circle", "geometry_boolean_union",
        "geometry_boolean_difference", "geometry_import", "geometry_build",
        "geometry_list_features",
    ),
    "src.tools.physics.register_physics_tools": (
        "physics_list", "physics_get_available", "physics_add",
        "physics_add_electrostatics", "physics_add_solid_mechanics",
        "physics_add_heat_transfer", "physics_add_laminar_flow",
        "physics_add_domain_feature", "physics_configure_boundary",
        "physics_set_material", "multiphysics_add", "physics_list_features",
        "physics_remove", "geometry_get_boundaries",
        "physics_interactive_setup_flow", "physics_setup_flow_boundaries",
        "physics_interactive_setup_heat", "physics_setup_heat_boundaries",
        "physics_boundary_selection",
    ),
    "src.tools.mesh.register_mesh_tools": (
        "mesh_list", "mesh_create", "mesh_sequence_create", "mesh_info",
    ),
    "src.tools.study.register_study_tools": (
        "study_list", "study_create", "study_solve", "study_solve_async",
        "study_get_progress", "study_cancel", "study_wait", "solutions_list",
        "datasets_list",
    ),
    "src.tools.results.register_results_tools": (
        "results_evaluate", "results_global_evaluate", "results_inner_values",
        "results_outer_values", "results_export_data", "results_export_image",
        "results_exports_list", "results_plots_list",
    ),
    "src.tools.mim_patch.register_mim_patch_tools": (
        "geometry_probe_domains", "mim_patch_build", "mim_evaluate_spectral",
    ),
    "src.tools.workflow.register_workflow_tools": (
        "study_staged_parametric_sweep", "mesh_convergence_study",
    ),
    "src.tools.properties.register_property_tools": (
        "clientapi_property_get", "clientapi_property_set",
    ),
    "src.tools.wave_optics_preflight.register_wave_optics_preflight_tools": (
        "wave_optics_preflight",
    ),
    "src.tools.periodic_mesh_audit.register_periodic_mesh_audit_tools": (
        "wave_optics_periodic_mesh_audit", "wave_optics_periodic_mesh_smoke",
    ),
    "src.tools.derived_geometry.register_derived_geometry_tools": (
        "geometry_derived_clone", "geometry_fin_preview", "geometry_fin_apply",
        "geometry_blocks_preview", "geometry_blocks_apply",
    ),
    "src.tools.incidence_config.register_incidence_config_tools": (
        "wave_optics_incidence_preview", "wave_optics_incidence_apply",
    ),
    "src.tools.wave_optics_audit.register_wave_optics_audit_tools": (
        "wave_optics_point_audit", "wave_optics_reference_audit",
    ),
    "src.tools.material_expressions.register_material_expression_tools": (
        "wave_optics_material_expression_preview",
    ),
    "src.tools.visual_review.register_visual_review_tools": (
        "visual_review_capability_normalize", "visual_review_request_create",
        "visual_review_receipt_create", "visual_review_dual_evaluate",
    ),
    "src.tools.field_evidence.register_field_evidence_tools": (
        "wave_optics_field_datasets", "wave_optics_field_extract",
    ),
    "src.tools.semantic_docs.register_semantic_doc_tools": (
        "semantic_search", "semantic_status", "semantic_worker_reset",
    ),
    "src.tools.spectral_characterization.register_spectral_characterization_tools": (
        "spectral_characterize",
    ),
    "src.tools.convergence_evaluation.register_convergence_evaluation_tools": (
        "convergence_evaluate",
    ),
    "src.tools.branch_continuation.register_branch_continuation_tools": (
        "branch_continuation_plan",
    ),
    "src.tools.shared_session.register_shared_session_tools": (
        "shared_server_preflight", "shared_server_attach",
        "shared_server_detach", "shared_server_status",
        "shared_server_models", "shared_model_lock",
        "shared_model_verify", "shared_model_unlock", "shared_model_snapshot",
        "shared_model_adopt",
    ),
    "src.knowledge.embedded.register_knowledge_tools": (
        "docs_get", "docs_list", "physics_get_guide", "troubleshoot",
        "modeling_best_practices",
    ),
    "src.knowledge.lexical_manual.register_lexical_manual_tools": (
        "manual_search", "manual_read_pages",
    ),
}

_GROUP_BY_REGISTRAR = {
    "register_capability_tools": "capabilities",
    "register_evidence_integrity_tools": "evidence_integrity",
    "register_ownership_tools": "ownership",
    "register_job_tools": "jobs",
    "register_session_tools": "session",
    "register_model_tools": "model",
    "register_parameter_tools": "parameters",
    "register_geometry_tools": "geometry",
    "register_physics_tools": "physics",
    "register_mesh_tools": "mesh",
    "register_study_tools": "study",
    "register_results_tools": "results",
    "register_mim_patch_tools": "mim_patch",
    "register_workflow_tools": "workflow",
    "register_property_tools": "clientapi_properties",
    "register_wave_optics_preflight_tools": "wave_optics_audit",
    "register_periodic_mesh_audit_tools": "wave_optics_audit",
    "register_derived_geometry_tools": "geometry",
    "register_incidence_config_tools": "wave_optics_incidence",
    "register_wave_optics_audit_tools": "wave_optics_audit",
    "register_material_expression_tools": "wave_optics_materials",
    "register_visual_review_tools": "visual_review",
    "register_field_evidence_tools": "field_evidence",
    "register_semantic_doc_tools": "semantic_docs",
    "register_spectral_characterization_tools": "spectral_evidence",
    "register_convergence_evaluation_tools": "convergence_evidence",
    "register_branch_continuation_tools": "branch_continuation_evidence",
    "register_shared_session_tools": "shared_session",
    "register_knowledge_tools": "embedded_docs",
    "register_lexical_manual_tools": "manuals",
}

_EXPERIMENTAL_TOOLS = frozenset({
    "comsol_connect",
    "geometry_add_feature",
    "physics_add",
    "physics_add_domain_feature",
    "multiphysics_add",
    "physics_interactive_setup_flow",
    "physics_setup_flow_boundaries",
    "physics_interactive_setup_heat",
    "physics_setup_heat_boundaries",
    "physics_boundary_selection",
    "study_solve_async",
    "study_get_progress",
    "study_cancel",
    "study_wait",
    "mim_patch_build",
    "mim_evaluate_spectral",
    "study_staged_parametric_sweep",
    "mesh_convergence_study",
    "clientapi_property_get",
    "clientapi_property_set",
    "wave_optics_preflight",
    "wave_optics_periodic_mesh_audit",
    "wave_optics_periodic_mesh_smoke",
    "wave_optics_point_audit",
    "wave_optics_reference_audit",
    "wave_optics_incidence_preview",
    "wave_optics_incidence_apply",
    "wave_optics_field_datasets",
    "wave_optics_field_extract",
    "semantic_search", "semantic_status", "semantic_worker_reset",
    "shared_server_preflight", "shared_server_attach",
    "shared_server_detach", "shared_server_status",
    "shared_server_models", "shared_model_lock",
    "shared_model_verify", "shared_model_unlock", "shared_model_snapshot",
    "shared_model_adopt",
})

_SIDE_EFFECTS = {
    "solver_recover_stale_lease": "ownership_control",
    "job_submit": "solver_execution",
    "job_cancel": "job_control",
    "job_resume": "solver_execution",
    "comsol_start": "process_lifecycle",
    "comsol_connect": "process_lifecycle",
    "comsol_disconnect": "process_lifecycle",
    "session_clear_models": "destructive_session",
    "session_reset": "destructive_session",
    "model_load": "filesystem_read_model_mutation",
    "model_create": "model_mutation",
    "model_create_component": "model_mutation",
    "model_save": "filesystem_write",
    "model_save_version": "filesystem_write",
    "model_set_current": "session_mutation",
    "model_clone": "filesystem_write_model_mutation",
    "model_remove": "destructive_session",
    "param_set": "model_mutation",
    "param_sweep_setup": "model_mutation",
    "param_description": "model_mutation",
    "geometry_create": "model_mutation",
    "geometry_add_feature": "model_mutation",
    "geometry_add_block": "model_mutation",
    "geometry_add_cylinder": "model_mutation",
    "geometry_add_sphere": "model_mutation",
    "geometry_add_rectangle": "model_mutation",
    "geometry_add_circle": "model_mutation",
    "geometry_boolean_union": "model_mutation",
    "geometry_boolean_difference": "model_mutation",
    "geometry_import": "filesystem_read_model_mutation",
    "geometry_build": "model_mutation",
    "physics_add": "model_mutation",
    "physics_add_electrostatics": "model_mutation",
    "physics_add_solid_mechanics": "model_mutation",
    "physics_add_heat_transfer": "model_mutation",
    "physics_add_laminar_flow": "model_mutation",
    "physics_add_domain_feature": "model_mutation",
    "physics_configure_boundary": "model_mutation",
    "physics_set_material": "model_mutation",
    "multiphysics_add": "model_mutation",
    "physics_remove": "model_mutation",
    "physics_interactive_setup_flow": "model_mutation",
    "physics_setup_flow_boundaries": "model_mutation",
    "physics_interactive_setup_heat": "model_mutation",
    "physics_setup_heat_boundaries": "model_mutation",
    "physics_boundary_selection": "model_mutation",
    "geometry_get_boundaries": "model_mutation",
    "mesh_create": "model_mutation",
    "mesh_sequence_create": "model_mutation",
    "study_create": "model_mutation",
    "study_solve": "solver_execution",
    "study_solve_async": "solver_execution",
    "study_cancel": "solver_control",
    "study_wait": "solver_control",
    "results_export_data": "filesystem_write",
    "results_export_image": "filesystem_write",
    "mim_patch_build": "model_mutation",
    "geometry_probe_domains": "model_mutation",
    "study_staged_parametric_sweep": "solver_execution",
    "mesh_convergence_study": "solver_execution",
    "clientapi_property_set": "model_mutation",
    "wave_optics_point_audit": "solver_execution",
    "wave_optics_reference_audit": "solver_execution",
    "wave_optics_periodic_mesh_smoke": "filesystem_write_model_mutation",
    "geometry_derived_clone": "filesystem_write_model_mutation",
    "geometry_fin_apply": "model_mutation",
    "geometry_blocks_apply": "model_mutation",
    "wave_optics_incidence_apply": "model_mutation",
    "manual_search": "read_only_subprocess",
    "manual_read_pages": "read_only_subprocess",
    "semantic_search": "read_only_subprocess",
    "semantic_status": "read_only_process_status",
    "semantic_worker_reset": "process_control",
    "shared_server_attach": "process_lifecycle",
    "shared_server_detach": "process_lifecycle",
    "shared_model_lock": "shared_model_guard",
    "shared_model_adopt": "shared_model_guard",
    "shared_model_unlock": "shared_model_guard",
    "shared_model_snapshot": "filesystem_write",
}

_STARTS_SOLVER = frozenset({
    "job_submit", "job_resume", "comsol_start", "study_solve",
    "study_solve_async", "study_staged_parametric_sweep", "mesh_convergence_study",
    "wave_optics_point_audit", "wave_optics_reference_audit",
})

_CONTROL_PLANE_TOOLS = frozenset({
    "capabilities", "evidence_integrity_status", "solver_status", "solver_preflight",
    "solver_recover_stale_lease", "job_status", "job_tail", "job_cancel",
    "comsol_status", "study_get_progress", "study_cancel", "study_wait",
    "semantic_status", "semantic_worker_reset",
    "shared_server_preflight", "shared_server_status",
})

_SOLVER_FREE_TOOLS = frozenset({
    "evidence_integrity_verify", "manual_search", "manual_read_pages", "semantic_search",
    "docs_get", "docs_list", "physics_get_guide", "troubleshoot",
    "modeling_best_practices", "wave_optics_material_expression_preview",
    "visual_review_capability_normalize", "visual_review_request_create",
    "visual_review_receipt_create", "visual_review_dual_evaluate",
    "spectral_characterize", "convergence_evaluate", "branch_continuation_plan",
    "geometry_fin_preview", "geometry_blocks_preview",
    "wave_optics_incidence_preview",
})

_MODEL_REVISION_EXCLUSIONS = frozenset({
    "job_submit", "job_resume", "comsol_start", "comsol_connect",
    "comsol_disconnect", "session_clear_models", "session_reset",
    "model_create", "model_load", "model_set_current",
    "mim_patch_build",
    "solver_recover_stale_lease", "semantic_worker_reset",
})

_MODEL_REVISION_REQUIRED_CLASSES = frozenset({
    "model_mutation", "destructive_session", "solver_execution",
    "filesystem_write_model_mutation",
})

_MODEL_REVISION_REQUIRED_ADDITIONS = frozenset({
    "model_save", "model_save_version",
})

_MODEL_REVISION_NONADVANCING = frozenset({
    "model_save", "model_save_version", "model_clone",
    "model_remove",
    "geometry_derived_clone", "wave_optics_periodic_mesh_smoke",
    "wave_optics_point_audit", "wave_optics_reference_audit",
})

_CORE_TOOLS = frozenset({
    "capabilities", "evidence_integrity_status", "evidence_integrity_verify",
    "solver_status", "solver_preflight", "solver_recover_stale_lease",
    "job_submit", "job_status", "job_tail", "job_cancel", "job_resume",
    "comsol_start", "comsol_disconnect", "comsol_status", "session_reset",
    "model_load", "model_list_components", "model_save", "model_list",
    "model_set_current", "model_remove", "model_inspect",
    "param_get", "param_set", "param_list",
    "geometry_list", "geometry_list_features", "geometry_get_boundaries",
    "geometry_probe_domains",
    "physics_list", "physics_list_features",
    "mesh_list", "mesh_info",
    "study_list", "study_solve", "solutions_list", "datasets_list",
    "results_global_evaluate", "manual_search", "manual_read_pages",
    "spectral_characterize",
    "convergence_evaluate",
    "branch_continuation_plan",
})

_BASIC_FEM_ADDITIONS = frozenset({
    "model_create", "model_create_component", "model_save_version", "model_clone",
    "geometry_derived_clone", "geometry_fin_preview", "geometry_fin_apply",
    "geometry_blocks_preview", "geometry_blocks_apply",
    "param_description",
    "geometry_create", "geometry_add_block", "geometry_add_cylinder",
    "geometry_add_sphere", "geometry_add_rectangle", "geometry_add_circle",
    "geometry_boolean_union", "geometry_boolean_difference", "geometry_import",
    "geometry_build", "physics_get_available", "physics_add_electrostatics",
    "physics_add_solid_mechanics", "physics_add_heat_transfer",
    "physics_add_laminar_flow", "physics_configure_boundary",
    "physics_set_material", "physics_remove", "mesh_create",
    "mesh_sequence_create", "study_create", "results_evaluate",
    "results_inner_values", "results_outer_values", "results_export_data",
    "results_export_image", "results_exports_list", "results_plots_list",
})

_WAVE_OPTICS_ADDITIONS = frozenset({
    "param_sweep_setup", "results_evaluate", "results_inner_values",
    "results_outer_values", "mim_evaluate_spectral",
    "wave_optics_preflight",
    "wave_optics_periodic_mesh_audit",
    "wave_optics_periodic_mesh_smoke",
    "geometry_derived_clone", "geometry_fin_preview", "geometry_fin_apply",
    "geometry_blocks_preview", "geometry_blocks_apply",
    "wave_optics_incidence_preview",
    "wave_optics_incidence_apply",
    "wave_optics_point_audit",
    "wave_optics_reference_audit",
    "wave_optics_material_expression_preview",
    "wave_optics_field_datasets",
    "wave_optics_field_extract",
    "visual_review_capability_normalize", "visual_review_request_create",
    "visual_review_receipt_create", "visual_review_dual_evaluate",
})

_EXPERIMENTAL_ADDITIONS = frozenset({
    "comsol_connect", "session_clear_models", "geometry_add_feature",
    "physics_add", "physics_add_domain_feature", "multiphysics_add",
    "physics_interactive_setup_flow", "physics_setup_flow_boundaries",
    "physics_interactive_setup_heat", "physics_setup_heat_boundaries",
    "physics_boundary_selection",
    "study_solve_async", "study_get_progress", "study_cancel", "study_wait",
    "mim_patch_build", "mim_evaluate_spectral", "study_staged_parametric_sweep",
    "mesh_convergence_study", "docs_get", "docs_list", "physics_get_guide",
    "troubleshoot", "modeling_best_practices",
    "clientapi_property_get", "clientapi_property_set",
})

_SEMANTIC_DOCS_ADDITIONS = frozenset({
    "semantic_search", "semantic_status", "semantic_worker_reset",
})

_DESKTOP_SHARED_FOUNDATION = frozenset({
    "capabilities", "evidence_integrity_status", "evidence_integrity_verify",
    "solver_status", "job_submit", "job_status", "job_tail",
    "job_cancel", "job_resume",
    "shared_server_preflight", "shared_server_attach",
    "shared_server_detach", "shared_server_status",
    "shared_server_models", "shared_model_lock",
    "shared_model_verify", "shared_model_unlock", "shared_model_snapshot",
    "shared_model_adopt",
})


def _build_registry() -> dict[str, ToolMetadata]:
    all_names = {
        name for names in _TOOLS_BY_REGISTRAR.values() for name in names
    }
    profile_tools = {
        "core": _CORE_TOOLS,
        "basic_fem": _CORE_TOOLS | _BASIC_FEM_ADDITIONS,
        "wave_optics": _CORE_TOOLS | _WAVE_OPTICS_ADDITIONS,
        "semantic_docs": _CORE_TOOLS | _SEMANTIC_DOCS_ADDITIONS,
        "desktop_shared": _DESKTOP_SHARED_FOUNDATION,
        "experimental": _CORE_TOOLS | _EXPERIMENTAL_ADDITIONS,
        "full": frozenset(all_names),
    }
    registry: dict[str, ToolMetadata] = {}
    for registrar, names in _TOOLS_BY_REGISTRAR.items():
        group = _GROUP_BY_REGISTRAR[registrar.rsplit(".", 1)[-1]]
        for name in names:
            side_effect_class = _SIDE_EFFECTS.get(name, "read_only")
            requires_revision = (
                side_effect_class in _MODEL_REVISION_REQUIRED_CLASSES
                or name in _MODEL_REVISION_REQUIRED_ADDITIONS
            ) and name not in _MODEL_REVISION_EXCLUSIONS
            registry[name] = ToolSpec(
                name=name,
                registrar=registrar,
                group=group,
                maturity="experimental" if name in _EXPERIMENTAL_TOOLS else "verified",
                side_effect_class=side_effect_class,
                concurrency_class=(
                    "control_plane"
                    if name in _CONTROL_PLANE_TOOLS
                    else "solver_free"
                    if name in _SOLVER_FREE_TOOLS
                    else "comsol_bound"
                ),
                requires_model_revision=requires_revision,
                advances_model_revision=(
                    requires_revision and name not in _MODEL_REVISION_NONADVANCING
                ),
                starts_solver=name in _STARTS_SOLVER,
                intended_profiles=tuple(
                    profile for profile in PROFILE_NAMES if name in profile_tools[profile]
                ),
                input_contract=f"tool-input/{name}/1",
                output_contract=f"tool-output/{name}/1",
                structural_limits=(
                    ("request_bytes", 1_048_576),
                    ("response_bytes", 4_194_304),
                ),
                artifact_path_classes=(
                    "owned_artifact"
                    if side_effect_class in {"filesystem_write", "filesystem_write_model_mutation"}
                    else "none"
                ,),
                required_features=("comsol",) if name in _STARTS_SOLVER else (),
                replacement_tool=(
                    "job_submit" if name == "study_staged_parametric_sweep" else None
                ),
                sunset_release=(
                    "next_major" if name == "study_staged_parametric_sweep" else None
                ),
                deprecation_state=(
                    "deprecated" if name == "study_staged_parametric_sweep" else "active"
                ),
            )
    return registry


TOOL_SPECS = MappingProxyType(_build_registry())
TOOL_METADATA = TOOL_SPECS


def validate_tool_specs(
    specs: Mapping[str, ToolSpec] | Iterable[ToolSpec] = TOOL_SPECS,
) -> dict[str, Any]:
    """Validate the import-free invariants of the public tool registry."""
    if isinstance(specs, Mapping):
        entries = tuple(specs.values())
        keys = tuple(specs)
    else:
        entries = tuple(specs)
        keys = tuple(spec.name for spec in entries)
    names = [spec.name for spec in entries]
    if len(names) != len(set(names)):
        raise ValueError("duplicate tool names in ToolSpec registry")
    if not names:
        raise ValueError("ToolSpec registry cannot be empty")
    normalized = {spec.name: spec for spec in entries}
    profile_set = set(PROFILE_NAMES)
    for key, spec in zip(keys, entries, strict=True):
        name = spec.name
        if key != name:
            raise ValueError(f"ToolSpec key/name mismatch for {name!r}")
        if "." not in spec.registrar or not spec.registrar.rsplit(".", 1)[-1]:
            raise ValueError(f"ToolSpec registrar is invalid for {name!r}")
        if not spec.group or not spec.input_contract or not spec.output_contract:
            raise ValueError(f"ToolSpec contract metadata is incomplete for {name!r}")
        if (
            not spec.intended_profiles
            or len(spec.intended_profiles) != len(set(spec.intended_profiles))
            or not set(spec.intended_profiles) <= profile_set
        ):
            raise ValueError(f"ToolSpec profiles are invalid for {name!r}")
        if "full" not in spec.intended_profiles:
            raise ValueError(f"ToolSpec compatibility profile is missing for {name!r}")
        if spec.maturity not in {"verified", "experimental", "deprecated"}:
            raise ValueError(f"ToolSpec maturity is invalid for {name!r}")
        if spec.side_effect_class == "read_only" and spec.requires_model_revision:
            raise ValueError(f"read-only ToolSpec requires a model revision: {name!r}")
        if spec.starts_solver and spec.side_effect_class not in {
            "solver_execution",
            "process_lifecycle",
        }:
            raise ValueError(f"solver-starting ToolSpec has impossible effects: {name!r}")
        if spec.maturity == "experimental" and {
            "core",
            "basic_fem",
        } & set(spec.intended_profiles):
            raise ValueError(f"stable profile contains experimental ToolSpec: {name!r}")
        if spec.advances_model_revision and not spec.requires_model_revision:
            raise ValueError(f"advancing ToolSpec lacks revision requirement: {name!r}")
        if spec.deprecation_state == "deprecated" and not spec.replacement_tool:
            raise ValueError(f"deprecated ToolSpec lacks replacement: {name!r}")
        if spec.replacement_tool and spec.replacement_tool not in normalized:
            raise ValueError(f"ToolSpec replacement is unknown for {name!r}")
    return {
        "valid": True,
        "tool_count": len(entries),
        "profile_count": len(PROFILE_NAMES),
    }


validate_tool_specs()


def registrars_for_profile(profile: str) -> tuple[str, ...]:
    """Return only registrar paths needed by one validated profile."""
    if profile not in PROFILE_NAMES:
        raise ValueError(f"Invalid profile {profile!r}")
    return tuple(
        registrar
        for registrar in _TOOLS_BY_REGISTRAR
        if any(
            spec.registrar == registrar and profile in spec.intended_profiles
            for spec in TOOL_SPECS.values()
        )
    )


def get_tool_metadata(name: str) -> ToolMetadata:
    """Return canonical metadata for one known tool name."""
    try:
        return TOOL_METADATA[name]
    except KeyError as exc:
        raise KeyError(f"No canonical metadata for MCP tool: {name}") from exc


async def snapshot_tool_schemas(server: Any) -> dict[str, dict[str, Any]]:
    """Return name-keyed public input schemas for every registered tool."""
    tools = await server.list_tools()
    return {
        tool.name: tool.inputSchema
        for tool in sorted(tools, key=lambda item: item.name)
    }


__all__ = [
    "PROFILE_NAMES",
    "TOOL_SPECS",
    "TOOL_METADATA",
    "ToolSpec",
    "ToolMetadata",
    "get_tool_metadata",
    "registrars_for_profile",
    "snapshot_tool_schemas",
    "validate_tool_specs",
]
