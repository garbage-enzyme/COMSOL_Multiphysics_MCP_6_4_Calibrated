"""Deterministic inspection helpers for the registered MCP tool surface."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any


PROFILE_NAMES = ("core", "basic_fem", "wave_optics", "semantic_docs", "experimental", "full")


@dataclass(frozen=True)
class ToolMetadata:
    """Static classification used by discovery profiles and capabilities."""

    name: str
    registrar: str
    group: str
    maturity: str
    side_effect_class: str
    starts_solver: bool
    intended_profiles: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_TOOLS_BY_REGISTRAR = {
    "src.tools.capabilities.register_capability_tools": (
        "capabilities",
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
    "geometry_derived_clone", "geometry_fin_preview", "geometry_fin_apply",
    "geometry_blocks_preview", "geometry_blocks_apply",
    "wave_optics_incidence_preview",
    "wave_optics_incidence_apply",
    "wave_optics_field_datasets",
    "wave_optics_field_extract",
    "semantic_search", "semantic_status", "semantic_worker_reset",
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
}

_STARTS_SOLVER = frozenset({
    "job_submit", "job_resume", "comsol_start", "study_solve",
    "study_solve_async", "study_staged_parametric_sweep", "mesh_convergence_study",
    "wave_optics_point_audit", "wave_optics_reference_audit",
})

_CORE_TOOLS = frozenset({
    "capabilities",
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
    "study_staged_parametric_sweep",
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


def _build_registry() -> dict[str, ToolMetadata]:
    all_names = {
        name for names in _TOOLS_BY_REGISTRAR.values() for name in names
    }
    profile_tools = {
        "core": _CORE_TOOLS,
        "basic_fem": _CORE_TOOLS | _BASIC_FEM_ADDITIONS,
        "wave_optics": _CORE_TOOLS | _WAVE_OPTICS_ADDITIONS,
        "semantic_docs": _CORE_TOOLS | _SEMANTIC_DOCS_ADDITIONS,
        "experimental": _CORE_TOOLS | _EXPERIMENTAL_ADDITIONS,
        "full": frozenset(all_names),
    }
    registry: dict[str, ToolMetadata] = {}
    for registrar, names in _TOOLS_BY_REGISTRAR.items():
        group = _GROUP_BY_REGISTRAR[registrar.rsplit(".", 1)[-1]]
        for name in names:
            registry[name] = ToolMetadata(
                name=name,
                registrar=registrar,
                group=group,
                maturity="experimental" if name in _EXPERIMENTAL_TOOLS else "verified",
                side_effect_class=_SIDE_EFFECTS.get(name, "read_only"),
                starts_solver=name in _STARTS_SOLVER,
                intended_profiles=tuple(
                    profile for profile in PROFILE_NAMES if name in profile_tools[profile]
                ),
            )
    return registry


TOOL_METADATA = MappingProxyType(_build_registry())


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
    "TOOL_METADATA",
    "ToolMetadata",
    "get_tool_metadata",
    "snapshot_tool_schemas",
]
