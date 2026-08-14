"""Solver-free contracts for bounded goal-directed research campaigns."""

from .adapters import (
    STRUCTURE_ADAPTER_APPLICATION_SCHEMA_NAME,
    STRUCTURE_ADAPTER_APPLICATION_SCHEMA_VERSION,
    STRUCTURE_ADAPTER_MANIFEST_SCHEMA_NAME,
    STRUCTURE_ADAPTER_MANIFEST_SCHEMA_VERSION,
    STRUCTURE_TREE_AUDIT_SCHEMA_NAME,
    STRUCTURE_TREE_AUDIT_SCHEMA_VERSION,
    ClientapiPeriodicMimPatchBackend,
    adapter_state_sha256,
    apply_periodic_mim_patch_candidate,
    normalize_structure_adapter_manifest,
    normalize_structure_tree_audit,
)
from .adjoint_adapter import ADJOINT_ADAPTER_ID, ADJOINT_ADAPTER_VERSION, configure_native_adjoint
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
from .derivative_support import (
    DERIVATIVE_CONSTRAINT_SCHEMA_NAME,
    DERIVATIVE_CONSTRAINT_SCHEMA_VERSION,
    DERIVATIVE_OBJECTIVE_SCHEMA_NAME,
    DERIVATIVE_OBJECTIVE_SCHEMA_VERSION,
    DERIVATIVE_SUPPORT_SCHEMA_NAME,
    DERIVATIVE_SUPPORT_SCHEMA_VERSION,
    DERIVATIVE_VARIABLE_SCHEMA_NAME,
    DERIVATIVE_VARIABLE_SCHEMA_VERSION,
    normalize_derivative_constraint,
    normalize_derivative_objective,
    normalize_derivative_support,
    normalize_derivative_variable,
)
from .evaluations import (
    EVALUATION_RECORD_SCHEMA_NAME,
    EVALUATION_RECORD_SCHEMA_VERSION,
    normalize_evaluation_record,
)
from .external_validation import (
    EXTERNAL_VALIDATION_RECEIPT_SCHEMA_NAME,
    EXTERNAL_VALIDATION_RECEIPT_SCHEMA_VERSION,
    normalize_external_validation_receipt,
)
from .gradient_contracts import (
    GRADIENT_RECORD_SCHEMA_NAME,
    GRADIENT_RECORD_SCHEMA_VERSION,
    NATIVE_OPTIMIZER_SCHEMA_NAME,
    NATIVE_OPTIMIZER_SCHEMA_VERSION,
    normalize_gradient_record,
    normalize_native_optimizer_configuration,
)
from .gradient_validation import (
    GRADIENT_CHECK_SCHEMA_NAME,
    GRADIENT_CHECK_SCHEMA_VERSION,
    compare_directional_gradient,
    compare_gradient,
)
from .journal import (
    RESEARCH_JOURNAL_RECORD_SCHEMA_NAME,
    RESEARCH_JOURNAL_RECORD_SCHEMA_VERSION,
    append_research_journal_record,
    recover_research_journal,
)
from .lin2025_pedot_cylinder import (
    ADAPTER_ID as LIN2025_PEDOT_CYLINDER_ADAPTER_ID,
)
from .lin2025_pedot_cylinder import (
    SCHEMA_NAME as LIN2025_PEDOT_CYLINDER_SCHEMA_NAME,
)
from .lin2025_pedot_cylinder import (
    SCHEMA_VERSION as LIN2025_PEDOT_CYLINDER_SCHEMA_VERSION,
)
from .lin2025_pedot_cylinder import (
    compile_lin2025_pedot_cylinder_binding,
    normalize_lin2025_pedot_cylinder_fixture,
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
from .optimizers import (
    OPTIMIZER_EXPLANATION_SCHEMA_NAME,
    OPTIMIZER_EXPLANATION_SCHEMA_VERSION,
    OPTIMIZER_PROPOSAL_SCHEMA_NAME,
    OPTIMIZER_PROPOSAL_SCHEMA_VERSION,
    OPTIMIZER_STATE_SCHEMA_NAME,
    OPTIMIZER_STATE_SCHEMA_VERSION,
    DeterministicGridOptimizer,
    DeterministicLatinHypercubeOptimizer,
    DeterministicRandomOptimizer,
    ResearchOptimizerProtocol,
)
from .records import (
    CANDIDATE_RECORD_SCHEMA_NAME,
    CANDIDATE_RECORD_SCHEMA_VERSION,
    normalize_candidate_record,
)
from .robust_adapter_configuration import (
    SCHEMA_NAME as ROBUST_SHAPE_ADAPTER_CONFIGURATION_SCHEMA_NAME,
)
from .robust_adapter_configuration import (
    SCHEMA_VERSION as ROBUST_SHAPE_ADAPTER_CONFIGURATION_SCHEMA_VERSION,
)
from .robust_adapter_configuration import (
    normalize_robust_shape_adapter_configuration,
)
from .robust_conditions import (
    MATERIAL_STATE_CONFIGURATION_SCHEMA_NAME,
    MATERIAL_STATE_CONFIGURATION_SCHEMA_VERSION,
    OPTIMIZATION_CONDITION_TABLE_SCHEMA_NAME,
    OPTIMIZATION_CONDITION_TABLE_SCHEMA_VERSION,
    normalize_optimization_condition_table,
    normalize_optimization_material_state,
)
from .robust_finalist_evidence import (
    ROBUST_FINALIST_VALIDATION_RECEIPT_SCHEMA_NAME,
    ROBUST_FINALIST_VALIDATION_RECEIPT_SCHEMA_VERSION,
    assess_robust_finalist_validation,
    normalize_robust_finalist_validation_receipt,
)
from .robust_finalist_validation import (
    ROBUST_FINALIST_VALIDATION_POLICY_SCHEMA_NAME,
    ROBUST_FINALIST_VALIDATION_POLICY_SCHEMA_VERSION,
    normalize_robust_finalist_validation_policy,
)
from .robust_gradient_acceptance import (
    ROBUST_GRADIENT_POLICY_SCHEMA_NAME,
    ROBUST_GRADIENT_POLICY_SCHEMA_VERSION,
    ROBUST_GRADIENT_RECEIPT_SCHEMA_NAME,
    ROBUST_GRADIENT_RECEIPT_SCHEMA_VERSION,
    assess_licensed_gradient_ladder,
    assess_robust_gradient_acceptance,
    normalize_robust_gradient_policy,
)
from .robust_material_mapping import (
    OPTICAL_PROPERTY_MAPPING_SCHEMA_NAME,
    OPTICAL_PROPERTY_MAPPING_SCHEMA_VERSION,
    normalize_optical_property_mapping,
)
from .robust_material_tensor_rows import (
    SCHEMA_NAME as ROBUST_MATERIAL_TENSOR_ROWS_SCHEMA_NAME,
)
from .robust_material_tensor_rows import (
    SCHEMA_VERSION as ROBUST_MATERIAL_TENSOR_ROWS_SCHEMA_VERSION,
)
from .robust_material_tensor_rows import (
    normalize_robust_material_tensor_rows,
)
from .robust_objectives import (
    ROBUST_OBJECTIVE_CONFIGURATION_SCHEMA_NAME,
    ROBUST_OBJECTIVE_CONFIGURATION_SCHEMA_VERSION,
    ROBUST_OBJECTIVE_GRADIENT_RECEIPT_SCHEMA_NAME,
    ROBUST_OBJECTIVE_GRADIENT_RECEIPT_SCHEMA_VERSION,
    ROBUST_OBJECTIVE_RECEIPT_SCHEMA_NAME,
    ROBUST_OBJECTIVE_RECEIPT_SCHEMA_VERSION,
    aggregate_robust_absolute_contrast_gradient,
    evaluate_robust_absolute_contrast,
    normalize_robust_objective_configuration,
)
from .robust_optimizer_policy import (
    ROBUST_OPTIMIZER_EXECUTION_RECEIPT_SCHEMA_NAME,
    ROBUST_OPTIMIZER_EXECUTION_RECEIPT_SCHEMA_VERSION,
    ROBUST_OPTIMIZER_POLICY_SCHEMA_NAME,
    ROBUST_OPTIMIZER_POLICY_SCHEMA_VERSION,
    assess_robust_optimizer_execution,
    normalize_robust_optimizer_policy,
)
from .robust_shape_adapter import (
    ROBUST_SHAPE_ADAPTER_BINDING_SCHEMA_NAME,
    ROBUST_SHAPE_ADAPTER_BINDING_SCHEMA_VERSION,
    ROBUST_SHAPE_CONTROL_RECEIPT_SCHEMA_NAME,
    ROBUST_SHAPE_CONTROL_RECEIPT_SCHEMA_VERSION,
    compile_robust_shape_adapter_binding,
    prepare_robust_shape_controls,
)
from .robust_smoothing_selection import (
    ROBUST_SMOOTHING_COMPARISON_SCHEMA_NAME,
    ROBUST_SMOOTHING_COMPARISON_SCHEMA_VERSION,
    compare_robust_smoothing_candidates,
    normalize_robust_smoothing_comparison_receipt,
)
from .shape_support import (
    SHAPE_SUPPORT_POLICY_SCHEMA_NAME,
    SHAPE_SUPPORT_POLICY_SCHEMA_VERSION,
    normalize_shape_support_policy,
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
    "ClientapiPeriodicMimPatchBackend",
    "ADJOINT_ADAPTER_ID",
    "ADJOINT_ADAPTER_VERSION",
    "STRUCTURE_ADAPTER_APPLICATION_SCHEMA_NAME",
    "STRUCTURE_ADAPTER_APPLICATION_SCHEMA_VERSION",
    "STRUCTURE_ADAPTER_MANIFEST_SCHEMA_NAME",
    "STRUCTURE_ADAPTER_MANIFEST_SCHEMA_VERSION",
    "STRUCTURE_TREE_AUDIT_SCHEMA_NAME",
    "STRUCTURE_TREE_AUDIT_SCHEMA_VERSION",
    "DESIGN_SPACE_SCHEMA_NAME",
    "DESIGN_SPACE_SCHEMA_VERSION",
    "EVALUATION_RECORD_SCHEMA_NAME",
    "EVALUATION_RECORD_SCHEMA_VERSION",
    "EXTERNAL_VALIDATION_RECEIPT_SCHEMA_NAME",
    "EXTERNAL_VALIDATION_RECEIPT_SCHEMA_VERSION",
    "DECISION_RECORD_SCHEMA_NAME",
    "DECISION_RECORD_SCHEMA_VERSION",
    "DERIVATIVE_CONSTRAINT_SCHEMA_NAME",
    "DERIVATIVE_CONSTRAINT_SCHEMA_VERSION",
    "DERIVATIVE_OBJECTIVE_SCHEMA_NAME",
    "DERIVATIVE_OBJECTIVE_SCHEMA_VERSION",
    "DERIVATIVE_SUPPORT_SCHEMA_NAME",
    "DERIVATIVE_SUPPORT_SCHEMA_VERSION",
    "DERIVATIVE_VARIABLE_SCHEMA_NAME",
    "DERIVATIVE_VARIABLE_SCHEMA_VERSION",
    "MATERIAL_CATALOG_SCHEMA_NAME",
    "MATERIAL_CATALOG_SCHEMA_VERSION",
    "MATERIAL_STATE_CONFIGURATION_SCHEMA_NAME",
    "MATERIAL_STATE_CONFIGURATION_SCHEMA_VERSION",
    "GRADIENT_RECORD_SCHEMA_NAME",
    "GRADIENT_RECORD_SCHEMA_VERSION",
    "GRADIENT_CHECK_SCHEMA_NAME",
    "GRADIENT_CHECK_SCHEMA_VERSION",
    "NATIVE_OPTIMIZER_SCHEMA_NAME",
    "NATIVE_OPTIMIZER_SCHEMA_VERSION",
    "OPTIMIZER_CHECKPOINT_SCHEMA_NAME",
    "OPTIMIZER_CHECKPOINT_SCHEMA_VERSION",
    "OPTIMIZER_EXPLANATION_SCHEMA_NAME",
    "OPTIMIZER_EXPLANATION_SCHEMA_VERSION",
    "OPTIMIZER_PROPOSAL_SCHEMA_NAME",
    "OPTIMIZER_PROPOSAL_SCHEMA_VERSION",
    "OPTIMIZER_STATE_SCHEMA_NAME",
    "OPTIMIZER_STATE_SCHEMA_VERSION",
    "OBJECTIVE_SCORE_SCHEMA_NAME",
    "OBJECTIVE_SCORE_SCHEMA_VERSION",
    "OPTIMIZATION_CONDITION_TABLE_SCHEMA_NAME",
    "OPTIMIZATION_CONDITION_TABLE_SCHEMA_VERSION",
    "OPTICAL_PROPERTY_MAPPING_SCHEMA_NAME",
    "OPTICAL_PROPERTY_MAPPING_SCHEMA_VERSION",
    "ROBUST_OBJECTIVE_CONFIGURATION_SCHEMA_NAME",
    "ROBUST_OBJECTIVE_CONFIGURATION_SCHEMA_VERSION",
    "ROBUST_OBJECTIVE_RECEIPT_SCHEMA_NAME",
    "ROBUST_OBJECTIVE_RECEIPT_SCHEMA_VERSION",
    "ROBUST_OBJECTIVE_GRADIENT_RECEIPT_SCHEMA_NAME",
    "ROBUST_OBJECTIVE_GRADIENT_RECEIPT_SCHEMA_VERSION",
    "ROBUST_OPTIMIZER_POLICY_SCHEMA_NAME",
    "ROBUST_OPTIMIZER_POLICY_SCHEMA_VERSION",
    "ROBUST_OPTIMIZER_EXECUTION_RECEIPT_SCHEMA_NAME",
    "ROBUST_OPTIMIZER_EXECUTION_RECEIPT_SCHEMA_VERSION",
    "ROBUST_GRADIENT_POLICY_SCHEMA_NAME",
    "ROBUST_GRADIENT_POLICY_SCHEMA_VERSION",
    "ROBUST_GRADIENT_RECEIPT_SCHEMA_NAME",
    "ROBUST_GRADIENT_RECEIPT_SCHEMA_VERSION",
    "ROBUST_FINALIST_VALIDATION_POLICY_SCHEMA_NAME",
    "ROBUST_FINALIST_VALIDATION_POLICY_SCHEMA_VERSION",
    "ROBUST_FINALIST_VALIDATION_RECEIPT_SCHEMA_NAME",
    "ROBUST_FINALIST_VALIDATION_RECEIPT_SCHEMA_VERSION",
    "ROBUST_SHAPE_ADAPTER_BINDING_SCHEMA_NAME",
    "ROBUST_SHAPE_ADAPTER_BINDING_SCHEMA_VERSION",
    "ROBUST_SHAPE_ADAPTER_CONFIGURATION_SCHEMA_NAME",
    "ROBUST_SHAPE_ADAPTER_CONFIGURATION_SCHEMA_VERSION",
    "ROBUST_MATERIAL_TENSOR_ROWS_SCHEMA_NAME",
    "ROBUST_MATERIAL_TENSOR_ROWS_SCHEMA_VERSION",
    "ROBUST_SHAPE_CONTROL_RECEIPT_SCHEMA_NAME",
    "ROBUST_SHAPE_CONTROL_RECEIPT_SCHEMA_VERSION",
    "ROBUST_SMOOTHING_COMPARISON_SCHEMA_NAME",
    "ROBUST_SMOOTHING_COMPARISON_SCHEMA_VERSION",
    "SHAPE_SUPPORT_POLICY_SCHEMA_NAME",
    "SHAPE_SUPPORT_POLICY_SCHEMA_VERSION",
    "PORTFOLIO_SCHEMA_NAME",
    "PORTFOLIO_SCHEMA_VERSION",
    "DeterministicGridOptimizer",
    "DeterministicLatinHypercubeOptimizer",
    "DeterministicRandomOptimizer",
    "ResearchOptimizerProtocol",
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
    "adapter_state_sha256",
    "apply_periodic_mim_patch_candidate",
    "normalize_design_space",
    "normalize_structure_adapter_manifest",
    "normalize_structure_tree_audit",
    "normalize_decision_record",
    "normalize_derivative_constraint",
    "normalize_derivative_objective",
    "normalize_derivative_support",
    "normalize_derivative_variable",
    "normalize_evaluation_record",
    "normalize_external_validation_receipt",
    "normalize_material_catalog",
    "normalize_optimization_condition_table",
    "normalize_optimization_material_state",
    "normalize_optical_property_mapping",
    "normalize_robust_objective_configuration",
    "normalize_robust_optimizer_policy",
    "assess_robust_optimizer_execution",
    "normalize_robust_gradient_policy",
    "normalize_robust_finalist_validation_policy",
    "normalize_robust_finalist_validation_receipt",
    "assess_robust_finalist_validation",
    "normalize_shape_support_policy",
    "normalize_gradient_record",
    "normalize_native_optimizer_configuration",
    "compare_gradient",
    "compare_directional_gradient",
    "normalize_optimizer_checkpoint",
    "normalize_portfolio",
    "normalize_candidate_record",
    "normalize_research_goal",
    "normalize_workflow_capsule",
    "append_research_journal_record",
    "recover_research_journal",
    "relative_bounds",
    "score_objectives",
    "evaluate_robust_absolute_contrast",
    "aggregate_robust_absolute_contrast_gradient",
    "assess_licensed_gradient_ladder",
    "assess_robust_gradient_acceptance",
    "compile_campaign_manifest",
    "compile_robust_shape_adapter_binding",
    "normalize_robust_shape_adapter_configuration",
    "normalize_robust_material_tensor_rows",
    "prepare_robust_shape_controls",
    "LIN2025_PEDOT_CYLINDER_ADAPTER_ID",
    "LIN2025_PEDOT_CYLINDER_SCHEMA_NAME",
    "LIN2025_PEDOT_CYLINDER_SCHEMA_VERSION",
    "normalize_lin2025_pedot_cylinder_fixture",
    "compile_lin2025_pedot_cylinder_binding",
    "compare_robust_smoothing_candidates",
    "normalize_robust_smoothing_comparison_receipt",
    "configure_native_adjoint",
]
