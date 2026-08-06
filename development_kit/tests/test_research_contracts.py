"""Solver-free contracts for bounded alpha7 research campaigns."""

from __future__ import annotations

import ast
import copy
from pathlib import Path

import pytest

from comsol_mcp.research import (
    compile_campaign_manifest,
    normalize_design_space,
    normalize_research_goal,
    relative_bounds,
)


def _goal() -> dict:
    return {
        "schema_name": "comsol_mcp.research_goal",
        "schema_version": "1.0.0",
        "goal_id": "mim-q-target",
        "title": "MIM patch target",
        "user_statement": "Match the declared peak and Q target.",
        "owner": "researcher",
        "created_at": "2026-08-06T00:00:00Z",
        "objectives": [
            {
                "objective_id": "q",
                "observable": "q_factor",
                "direction": "match",
                "metric": "absolute_error",
                "unit": "1",
                "target": 20,
                "tolerance": 2,
            },
            {
                "objective_id": "peak",
                "observable": "peak_wavelength",
                "direction": "match",
                "metric": "absolute_error",
                "unit": "nm",
                "target": 1550,
                "tolerance": 10,
            },
        ],
        "constraints": [],
        "autonomy_level": "A3_adaptive_campaign",
        "output_intent": "engineering_candidate",
        "resource_budget": {
            "max_fem_evaluations": 32,
            "max_wall_time_seconds": 8 * 3600,
            "max_memory_bytes": 1 << 30,
            "max_disk_bytes": 1 << 30,
        },
        "stop_policy": {
            "success": "all objectives within tolerance",
            "infeasible": "none",
            "stagnation": "none",
            "budget": "32 evaluations",
        },
        "target_data": {"peak_nm": 1550, "q": 20},
        "fidelity_policy": {"final": "fem", "rcwa": "optional"},
        "evidence_policy": {"require_closure": True},
    }


def _space() -> dict:
    lo_x, hi_x = relative_bounds(100.0)
    lo_y, hi_y = relative_bounds(80.0)
    variables = [
        {
            "variable_id": "patch_length_x",
            "kind": "continuous",
            "unit": "nm",
            "baseline": 100.0,
            "lower": lo_x,
            "upper": hi_x,
            "allowed_values": None,
            "dependency_class": "geometry",
            "adapter_path": "geom.patch_length_x",
        },
        {
            "variable_id": "patch_length_y",
            "kind": "continuous",
            "unit": "nm",
            "baseline": 80.0,
            "lower": lo_y,
            "upper": hi_y,
            "allowed_values": None,
            "dependency_class": "geometry",
            "adapter_path": "geom.patch_length_y",
        },
    ]
    return {
        "schema_name": "comsol_mcp.design_space",
        "schema_version": "1.0.0",
        "space_id": "mim-space",
        "structure_family": "periodic_mim_patch_v1",
        "template_identity": {"sha256": "a" * 64},
        "variables": variables,
        "constraints": [],
        "canonicalization": {"float_digits": 12, "relative_tolerance": 1e-12},
        "adapter_mappings": [
            {
                "variable_id": item["variable_id"],
                "adapter_path": item["adapter_path"],
                "unit": item["unit"],
            }
            for item in variables
        ],
    }


def _approval() -> dict:
    return {
        "campaign_id": "mim-campaign-001",
        "approval_id": "approval-001",
        "approved_by": "researcher",
        "approved_at": "2026-08-06T01:00:00Z",
    }


def test_d0_bounds_and_fingerprints_are_stable():
    assert relative_bounds(100) == (75.0, 125.0)
    first = normalize_design_space(_space())
    reordered = copy.deepcopy(_space())
    reordered["variables"].reverse()
    reordered["adapter_mappings"].reverse()
    second = normalize_design_space(reordered)
    assert first["space_fingerprint"] == second["space_fingerprint"]
    assert first["variables"][0]["variable_id"] == "patch_length_x"


def test_goal_fingerprint_is_domain_separated_and_order_independent():
    first = normalize_research_goal(_goal())
    reordered = copy.deepcopy(_goal())
    reordered["objectives"].reverse()
    second = normalize_research_goal(reordered)
    assert first["goal_fingerprint"] == second["goal_fingerprint"]
    assert first["goal_fingerprint"] != normalize_design_space(_space())["space_fingerprint"]


@pytest.mark.parametrize("field", ["title", "user_statement", "target_data"])
def test_goal_rejects_unknown_fields(field):
    value = _goal()
    value[field + "_unknown"] = 1
    with pytest.raises(ValueError):
        normalize_research_goal(value)


def test_goal_rejects_empty_objectives_and_nonfinite_values():
    value = _goal()
    value["objectives"] = []
    with pytest.raises(ValueError):
        normalize_research_goal(value)
    value = _goal()
    value["objectives"][0]["target"] = float("nan")
    with pytest.raises(ValueError):
        normalize_research_goal(value)


def test_space_rejects_boolean_numbers_and_out_of_space_constraints():
    value = _space()
    value["variables"][0]["baseline"] = True
    with pytest.raises(ValueError):
        normalize_design_space(value)
    value = _space()
    value["constraints"] = [
        {
            "constraint_id": "c",
            "kind": "geometry",
            "variable_ids": ["missing"],
            "operator": "ge",
            "value": 1,
        }
    ]
    with pytest.raises(ValueError):
        normalize_design_space(value)


def test_space_rejects_duplicate_variables_and_bad_categorical_domain():
    value = _space()
    value["variables"].append(copy.deepcopy(value["variables"][0]))
    with pytest.raises(ValueError):
        normalize_design_space(value)
    value = _space()
    value["variables"][0]["kind"] = "categorical"
    value["variables"][0]["allowed_values"] = ["a", "b"]
    value["variables"][0]["baseline"] = "c"
    with pytest.raises(ValueError):
        normalize_design_space(value)


def test_normalized_nested_inputs_are_defensive_copies():
    value = _goal()
    normalized = normalize_research_goal(value)
    value["target_data"]["peak_nm"] = 999
    assert normalized["target_data"]["peak_nm"] == 1550


def test_research_package_has_no_solver_or_optimizer_imports():
    root = Path(__file__).parents[2] / "comsol_mcp" / "research"
    imported_roots: set[str] = set()
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])
    assert imported_roots.isdisjoint({"mph", "jpype", "scipy", "sklearn", "optuna"})


def test_campaign_compilation_binds_goal_space_and_approval():
    manifest = compile_campaign_manifest(_goal(), _space(), _approval())
    assert manifest["state"] == "compiled_not_started"
    assert manifest["success_claim_allowed"] is True
    assert manifest["missing_success_threshold_objective_ids"] == []
    assert len(manifest["campaign_fingerprint"]) == 64
    assert manifest == compile_campaign_manifest(_goal(), _space(), _approval())


def test_missing_threshold_allows_ranking_but_forbids_success_claim():
    goal = _goal()
    goal["objectives"][0]["tolerance"] = None
    manifest = compile_campaign_manifest(goal, _space(), _approval())
    assert manifest["success_claim_allowed"] is False
    assert manifest["missing_success_threshold_objective_ids"] == ["q"]


def test_campaign_rejects_goal_constraints_outside_design_space():
    goal = _goal()
    goal["constraints"] = [
        {
            "constraint_id": "undeclared",
            "kind": "geometry",
            "variable_ids": ["gap"],
            "operator": "ge",
            "value": 10,
        }
    ]
    with pytest.raises(ValueError, match="declared design-space variables"):
        compile_campaign_manifest(goal, _space(), _approval())


def test_campaign_rejects_adapter_mapping_drift_and_unapproved_fields():
    space = _space()
    space["adapter_mappings"][0]["unit"] = "um"
    with pytest.raises(ValueError, match="adapter mappings"):
        compile_campaign_manifest(_goal(), space, _approval())
    approval = _approval()
    approval["extra"] = True
    with pytest.raises(ValueError, match="fields mismatch"):
        compile_campaign_manifest(_goal(), _space(), approval)
