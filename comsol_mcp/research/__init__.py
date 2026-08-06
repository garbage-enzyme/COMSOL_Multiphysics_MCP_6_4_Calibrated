"""Solver-free contracts for bounded goal-directed research campaigns."""

from .compiler import (
    CAMPAIGN_MANIFEST_SCHEMA_NAME,
    CAMPAIGN_MANIFEST_SCHEMA_VERSION,
    compile_campaign_manifest,
)
from .contracts import (
    DESIGN_SPACE_SCHEMA_NAME,
    DESIGN_SPACE_SCHEMA_VERSION,
    RESEARCH_GOAL_SCHEMA_NAME,
    RESEARCH_GOAL_SCHEMA_VERSION,
    normalize_design_space,
    normalize_research_goal,
    relative_bounds,
)
from .decisions import (
    DECISION_RECORD_SCHEMA_NAME,
    DECISION_RECORD_SCHEMA_VERSION,
    normalize_decision_record,
)
from .evaluations import (
    EVALUATION_RECORD_SCHEMA_NAME,
    EVALUATION_RECORD_SCHEMA_VERSION,
    normalize_evaluation_record,
)
from .journal import (
    RESEARCH_JOURNAL_RECORD_SCHEMA_NAME,
    RESEARCH_JOURNAL_RECORD_SCHEMA_VERSION,
    append_research_journal_record,
    recover_research_journal,
)
from .materials import (
    MATERIAL_CATALOG_SCHEMA_NAME,
    MATERIAL_CATALOG_SCHEMA_VERSION,
    normalize_material_catalog,
)
from .objectives import (
    OBJECTIVE_SCORE_SCHEMA_NAME,
    OBJECTIVE_SCORE_SCHEMA_VERSION,
    score_objectives,
)
from .records import (
    CANDIDATE_RECORD_SCHEMA_NAME,
    CANDIDATE_RECORD_SCHEMA_VERSION,
    normalize_candidate_record,
)
from .state import (
    OPTIMIZER_CHECKPOINT_SCHEMA_NAME,
    OPTIMIZER_CHECKPOINT_SCHEMA_VERSION,
    PORTFOLIO_SCHEMA_NAME,
    PORTFOLIO_SCHEMA_VERSION,
    normalize_optimizer_checkpoint,
    normalize_portfolio,
)
from .workflow import (
    WORKFLOW_CAPSULE_SCHEMA_NAME,
    WORKFLOW_CAPSULE_SCHEMA_VERSION,
    normalize_workflow_capsule,
)

__all__ = [
    "DESIGN_SPACE_SCHEMA_NAME",
    "DESIGN_SPACE_SCHEMA_VERSION",
    "EVALUATION_RECORD_SCHEMA_NAME",
    "EVALUATION_RECORD_SCHEMA_VERSION",
    "DECISION_RECORD_SCHEMA_NAME",
    "DECISION_RECORD_SCHEMA_VERSION",
    "MATERIAL_CATALOG_SCHEMA_NAME",
    "MATERIAL_CATALOG_SCHEMA_VERSION",
    "OPTIMIZER_CHECKPOINT_SCHEMA_NAME",
    "OPTIMIZER_CHECKPOINT_SCHEMA_VERSION",
    "OBJECTIVE_SCORE_SCHEMA_NAME",
    "OBJECTIVE_SCORE_SCHEMA_VERSION",
    "PORTFOLIO_SCHEMA_NAME",
    "PORTFOLIO_SCHEMA_VERSION",
    "CAMPAIGN_MANIFEST_SCHEMA_NAME",
    "CAMPAIGN_MANIFEST_SCHEMA_VERSION",
    "CANDIDATE_RECORD_SCHEMA_NAME",
    "CANDIDATE_RECORD_SCHEMA_VERSION",
    "RESEARCH_GOAL_SCHEMA_NAME",
    "RESEARCH_GOAL_SCHEMA_VERSION",
    "RESEARCH_JOURNAL_RECORD_SCHEMA_NAME",
    "RESEARCH_JOURNAL_RECORD_SCHEMA_VERSION",
    "WORKFLOW_CAPSULE_SCHEMA_NAME",
    "WORKFLOW_CAPSULE_SCHEMA_VERSION",
    "normalize_design_space",
    "normalize_decision_record",
    "normalize_evaluation_record",
    "normalize_material_catalog",
    "normalize_optimizer_checkpoint",
    "normalize_portfolio",
    "normalize_candidate_record",
    "normalize_research_goal",
    "normalize_workflow_capsule",
    "append_research_journal_record",
    "recover_research_journal",
    "relative_bounds",
    "score_objectives",
    "compile_campaign_manifest",
]
