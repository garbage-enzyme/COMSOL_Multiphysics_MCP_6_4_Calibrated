"""Solver-free contracts for bounded goal-directed research campaigns."""

from .contracts import (
    DESIGN_SPACE_SCHEMA_NAME,
    DESIGN_SPACE_SCHEMA_VERSION,
    RESEARCH_GOAL_SCHEMA_NAME,
    RESEARCH_GOAL_SCHEMA_VERSION,
    normalize_design_space,
    normalize_research_goal,
    relative_bounds,
)

__all__ = [
    "DESIGN_SPACE_SCHEMA_NAME",
    "DESIGN_SPACE_SCHEMA_VERSION",
    "RESEARCH_GOAL_SCHEMA_NAME",
    "RESEARCH_GOAL_SCHEMA_VERSION",
    "normalize_design_space",
    "normalize_research_goal",
    "relative_bounds",
]
