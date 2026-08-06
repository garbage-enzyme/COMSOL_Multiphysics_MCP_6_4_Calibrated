"""Compile approved goals and design spaces into frozen campaign manifests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .contracts import (
    _identifier,
    _object,
    _text,
    _timestamp,
    normalize_design_space,
    normalize_research_goal,
)
from .materials import normalize_material_catalog
from .workflow import normalize_workflow_capsule

CAMPAIGN_MANIFEST_SCHEMA_NAME = "comsol_mcp.research_campaign_manifest"
CAMPAIGN_MANIFEST_SCHEMA_VERSION = "1.0.0"


def _normalize_approval(value: object) -> dict[str, str]:
    raw = _object(
        value,
        {"campaign_id", "approval_id", "approved_by", "approved_at"},
        "approval",
    )
    return {
        "campaign_id": _identifier(raw["campaign_id"], "approval.campaign_id"),
        "approval_id": _identifier(raw["approval_id"], "approval.approval_id"),
        "approved_by": _text(raw["approved_by"], "approval.approved_by", maximum=256),
        "approved_at": _timestamp(raw["approved_at"], "approval.approved_at"),
    }


def _validate_cross_contracts(goal: Mapping[str, Any], space: Mapping[str, Any]) -> None:
    variable_ids = {item["variable_id"] for item in space["variables"]}
    referenced = {
        variable_id
        for constraint in goal["constraints"]
        for variable_id in constraint["variable_ids"]
    }
    if not referenced <= variable_ids:
        raise ValueError("goal constraints must reference declared design-space variables")
    variables = {item["variable_id"]: item for item in space["variables"]}
    for mapping in space["adapter_mappings"]:
        variable = variables[mapping["variable_id"]]
        if (
            mapping["unit"] != variable["unit"]
            or mapping["adapter_path"] != variable["adapter_path"]
        ):
            raise ValueError("adapter mappings must match variable unit and adapter path")


def compile_campaign_manifest(
    goal: object,
    design_space: object,
    approval: object,
    *,
    workflow_capsule: object | None = None,
    material_catalog: object | None = None,
) -> dict[str, Any]:
    """Compile one solver-free, approved, hash-bound campaign manifest."""
    normalized_goal = normalize_research_goal(goal)
    normalized_space = normalize_design_space(design_space)
    normalized_approval = _normalize_approval(approval)
    _validate_cross_contracts(normalized_goal, normalized_space)
    normalized_workflow = (
        None if workflow_capsule is None else normalize_workflow_capsule(workflow_capsule)
    )
    if normalized_workflow is not None:
        if not normalized_workflow["exploration_ready"]:
            raise ValueError("workflow capsule must be exploration-ready")
        if (
            normalized_workflow["template_mapping"]["structure_family"]
            != normalized_space["structure_family"]
        ):
            raise ValueError("workflow structure family must match the design space")
    normalized_materials = (
        None if material_catalog is None else normalize_material_catalog(material_catalog)
    )
    material_variables = [
        item for item in normalized_space["variables"] if item["dependency_class"] == "material"
    ]
    if material_variables and normalized_materials is None:
        raise ValueError("material variables require a frozen material catalog")
    approved_materials = (
        set()
        if normalized_materials is None
        else set(normalized_materials["caller_approved_material_ids"])
    )
    for variable in material_variables:
        allowed = set(variable["allowed_values"] or [])
        if not allowed <= approved_materials:
            raise ValueError("material variables must use caller-approved catalog entries")
    missing_thresholds = sorted(
        item["objective_id"] for item in normalized_goal["objectives"] if item["tolerance"] is None
    )
    body = {
        "schema_name": CAMPAIGN_MANIFEST_SCHEMA_NAME,
        "schema_version": CAMPAIGN_MANIFEST_SCHEMA_VERSION,
        "campaign_id": normalized_approval["campaign_id"],
        "state": "compiled_not_started",
        "approval": normalized_approval,
        "goal": normalized_goal,
        "design_space": normalized_space,
        "workflow_capsule": normalized_workflow,
        "material_catalog": normalized_materials,
        "success_claim_allowed": not missing_thresholds,
        "missing_success_threshold_objective_ids": missing_thresholds,
    }
    return {
        **body,
        "campaign_fingerprint": domain_sha256_v2(CAMPAIGN_MANIFEST_SCHEMA_NAME, body),
    }


__all__ = [
    "CAMPAIGN_MANIFEST_SCHEMA_NAME",
    "CAMPAIGN_MANIFEST_SCHEMA_VERSION",
    "compile_campaign_manifest",
]
