"""MCP Tools for COMSOL operations."""

from .capabilities import register_capability_tools
from .ownership import register_ownership_tools
from .jobs import register_job_tools
from .session import register_session_tools
from .model import register_model_tools
from .parameters import register_parameter_tools
from .geometry import register_geometry_tools
from .physics import register_physics_tools
from .mesh import register_mesh_tools
from .study import register_study_tools
from .results import register_results_tools
from .mim_patch import register_mim_patch_tools
from .workflow import register_workflow_tools
from .properties import register_property_tools
from .wave_optics_preflight import register_wave_optics_preflight_tools
from .periodic_mesh_audit import register_periodic_mesh_audit_tools
from .derived_geometry import register_derived_geometry_tools
from .incidence_config import register_incidence_config_tools
from .wave_optics_audit import register_wave_optics_audit_tools
from .material_expressions import register_material_expression_tools
from .visual_review import register_visual_review_tools
from .field_evidence import register_field_evidence_tools
from .semantic_docs import register_semantic_doc_tools
from .spectral_characterization import register_spectral_characterization_tools
from .convergence_evaluation import register_convergence_evaluation_tools
from .profiles import ProfileSelection, register_profiled, resolve_profile, tool_names_for_profile

TOOL_REGISTRARS = (
    register_capability_tools,
    register_ownership_tools,
    register_job_tools,
    register_session_tools,
    register_model_tools,
    register_parameter_tools,
    register_geometry_tools,
    register_physics_tools,
    register_mesh_tools,
    register_study_tools,
    register_results_tools,
    register_mim_patch_tools,
    register_workflow_tools,
    register_property_tools,
    register_wave_optics_preflight_tools,
    register_periodic_mesh_audit_tools,
    register_derived_geometry_tools,
    register_incidence_config_tools,
    register_wave_optics_audit_tools,
    register_material_expression_tools,
    register_visual_review_tools,
    register_field_evidence_tools,
    register_semantic_doc_tools,
    register_spectral_characterization_tools,
    register_convergence_evaluation_tools,
)


def register_tool_modules(mcp, profile: str | ProfileSelection = "full") -> None:
    """Register the selected COMSOL tool surface on a FastMCP server."""
    selection = profile if isinstance(profile, ProfileSelection) else resolve_profile(profile)
    enabled_names = tool_names_for_profile(selection.name)
    for register in TOOL_REGISTRARS:
        register_profiled(mcp, register, enabled_names, selection)

__all__ = [
    "register_capability_tools",
    "register_ownership_tools",
    "register_job_tools",
    "register_session_tools",
    "register_model_tools",
    "register_parameter_tools",
    "register_geometry_tools",
    "register_physics_tools",
    "register_mesh_tools",
    "register_study_tools",
    "register_results_tools",
    "register_mim_patch_tools",
    "register_workflow_tools",
    "register_property_tools",
    "register_wave_optics_preflight_tools",
    "register_periodic_mesh_audit_tools",
    "register_derived_geometry_tools",
    "register_incidence_config_tools",
    "register_wave_optics_audit_tools",
    "register_material_expression_tools",
    "register_visual_review_tools",
    "register_field_evidence_tools",
    "register_semantic_doc_tools",
    "register_spectral_characterization_tools",
    "register_convergence_evaluation_tools",
    "TOOL_REGISTRARS",
    "register_tool_modules",
]
