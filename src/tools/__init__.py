"""MCP Tools for COMSOL operations."""

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

TOOL_REGISTRARS = (
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
)


def register_tool_modules(mcp) -> None:
    """Register every COMSOL tool module on a FastMCP server."""
    for register in TOOL_REGISTRARS:
        register(mcp)

__all__ = [
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
    "TOOL_REGISTRARS",
    "register_tool_modules",
]
