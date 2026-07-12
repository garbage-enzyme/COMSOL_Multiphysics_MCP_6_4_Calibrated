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
    "TOOL_REGISTRARS",
    "register_tool_modules",
]
