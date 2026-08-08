"""Closed solver-free contracts for native derivative support."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

from comsol_mcp.durable import domain_sha256_v2, validate_finite_json

DERIVATIVE_SUPPORT_SCHEMA_NAME = "comsol_mcp.derivative_support"
DERIVATIVE_SUPPORT_SCHEMA_VERSION = "1.0.0"
DERIVATIVE_VARIABLE_SCHEMA_NAME = "comsol_mcp.derivative_variable"
DERIVATIVE_VARIABLE_SCHEMA_VERSION = "1.0.0"
DERIVATIVE_OBJECTIVE_SCHEMA_NAME = "comsol_mcp.derivative_objective"
DERIVATIVE_OBJECTIVE_SCHEMA_VERSION = "1.0.0"
DERIVATIVE_CONSTRAINT_SCHEMA_NAME = "comsol_mcp.derivative_constraint"
DERIVATIVE_CONSTRAINT_SCHEMA_VERSION = "1.0.0"
MAX_VARIABLES = 8
MAX_CONSTRAINTS = 16
MAX_TEXT = 512
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_METHODS = {"adjoint", "forward"}
_SUPPORT_STATES = {
    "structurally_supported",
    "licensed_proven",
    "gradient_validated",
    "restricted",
    "unsupported",
}
_DEPENDENCIES = {"geometry", "material", "mesh", "physics"}
_CONSTRAINT_KINDS = {"bound", "differentiable_scalar", "forward_only", "evidence", "policy"}
_DIRECTIONS = {"minimize", "maximize", "match"}


def _object(value: object, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object with string keys")
    result = dict(value)
    if set(result) != fields:
        raise ValueError(
            f"{name} fields mismatch; missing={sorted(fields - set(result))}, "
            f"unknown={sorted(set(result) - fields)}"
        )
    return result


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} must be a bounded identifier")
    return value


def _text(value: object, name: str, *, maximum: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a bounded nonempty string")
    return value.strip()


def _finite(value: object, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0.0):
        raise ValueError(f"{name} must be a finite number in the allowed range")
    return number


def _bounded_json(value: object, name: str, maximum: int = 64 * 1024) -> Any:
    validate_finite_json(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > maximum:
        raise ValueError(f"{name} exceeds the bounded JSON size")
    return json.loads(encoded)


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a SHA-256 hex digest")
    return value.lower()


def normalize_derivative_variable(value: object, *, index: int) -> dict[str, Any]:
    """Normalize one explicit, continuous native-derivative control."""
    name = f"variables[{index}]"
    raw = _object(
        value,
        {
            "variable_id",
            "order",
            "kind",
            "meaning",
            "unit",
            "baseline",
            "lower",
            "upper",
            "scale",
            "mapping",
            "dependency_class",
            "step_policy",
            "active_bound_semantics",
        },
        name,
    )
    if isinstance(raw["order"], bool) or not isinstance(raw["order"], int) or raw["order"] != index:
        raise ValueError(f"{name}.order must be the canonical zero-based variable order")
    if raw["kind"] != "continuous":
        raise ValueError(f"{name}.kind must be continuous for alpha7.1")
    baseline = _finite(raw["baseline"], f"{name}.baseline")
    lower = _finite(raw["lower"], f"{name}.lower")
    upper = _finite(raw["upper"], f"{name}.upper")
    if not lower < baseline < upper:
        raise ValueError(f"{name} bounds must contain an interior baseline")
    scale = _finite(raw["scale"], f"{name}.scale", positive=True)
    mapping = _object(
        raw["mapping"],
        {"feature_tag", "feature_type", "property_name", "readback_expression"},
        f"{name}.mapping",
    )
    normalized_mapping = {
        "feature_tag": _identifier(mapping["feature_tag"], f"{name}.mapping.feature_tag"),
        "feature_type": _identifier(mapping["feature_type"], f"{name}.mapping.feature_type"),
        "property_name": _identifier(mapping["property_name"], f"{name}.mapping.property_name"),
        "readback_expression": _text(
            mapping["readback_expression"], f"{name}.mapping.readback_expression", maximum=256
        ),
    }
    dependency = raw["dependency_class"]
    if dependency not in _DEPENDENCIES:
        raise ValueError(f"{name}.dependency_class is unsupported")
    step = _object(
        raw["step_policy"],
        {"relative_steps", "absolute_floor", "central_difference", "near_bound_mode"},
        f"{name}.step_policy",
    )
    steps = step["relative_steps"]
    if not isinstance(steps, list) or len(steps) != 3:
        raise ValueError(f"{name}.step_policy.relative_steps must be three descending steps")
    normalized_steps = [
        _finite(item, f"{name}.step_policy.relative_steps", positive=True) for item in steps
    ]
    if normalized_steps != sorted(normalized_steps, reverse=True):
        raise ValueError(f"{name}.step_policy.relative_steps must be three descending steps")
    near_bound = step["near_bound_mode"]
    if near_bound not in {"one_sided", "reject"}:
        raise ValueError(f"{name}.step_policy.near_bound_mode is unsupported")
    if not isinstance(step["central_difference"], bool):
        raise ValueError(f"{name}.step_policy.central_difference must be boolean")
    if raw["active_bound_semantics"] not in {"projected_zero", "one_sided"}:
        raise ValueError(f"{name}.active_bound_semantics is unsupported")
    return {
        "variable_id": _identifier(raw["variable_id"], f"{name}.variable_id"),
        "order": index,
        "kind": "continuous",
        "meaning": _text(raw["meaning"], f"{name}.meaning"),
        "unit": _text(raw["unit"], f"{name}.unit", maximum=32),
        "baseline": baseline,
        "lower": lower,
        "upper": upper,
        "scale": scale,
        "mapping": normalized_mapping,
        "dependency_class": dependency,
        "step_policy": {
            "relative_steps": normalized_steps,
            "absolute_floor": _finite(
                step["absolute_floor"], f"{name}.step_policy.absolute_floor", positive=True
            ),
            "central_difference": step["central_difference"],
            "near_bound_mode": near_bound,
        },
        "active_bound_semantics": raw["active_bound_semantics"],
    }


def normalize_derivative_objective(value: object) -> dict[str, Any]:
    """Normalize a scalar objective bound to one COMSOL result identity."""
    raw = _object(
        value,
        {
            "objective_id",
            "expression",
            "direction",
            "unit",
            "wavelength_um",
            "study_tag",
            "solution_tag",
            "dataset_tag",
            "evidence_paths",
        },
        "objective",
    )
    if raw["direction"] not in _DIRECTIONS:
        raise ValueError("objective.direction is unsupported")
    paths = raw["evidence_paths"]
    if not isinstance(paths, list) or not 1 <= len(paths) <= 16:
        raise ValueError("objective.evidence_paths must be a bounded nonempty list")
    normalized_paths = [_text(path, "objective.evidence_paths", maximum=256) for path in paths]
    if len(normalized_paths) != len(set(normalized_paths)):
        raise ValueError("objective.evidence_paths must be unique")
    return {
        "objective_id": _identifier(raw["objective_id"], "objective.objective_id"),
        "expression": _text(raw["expression"], "objective.expression", maximum=512),
        "direction": raw["direction"],
        "unit": _text(raw["unit"], "objective.unit", maximum=32),
        "wavelength_um": _finite(raw["wavelength_um"], "objective.wavelength_um", positive=True),
        "study_tag": _identifier(raw["study_tag"], "objective.study_tag"),
        "solution_tag": _identifier(raw["solution_tag"], "objective.solution_tag"),
        "dataset_tag": _identifier(raw["dataset_tag"], "objective.dataset_tag"),
        "evidence_paths": sorted(normalized_paths),
    }


def normalize_derivative_constraint(value: object) -> dict[str, Any]:
    """Normalize a constraint without hiding its derivative status."""
    raw = _object(
        value,
        {"constraint_id", "kind", "expression", "unit", "lower", "upper", "derivative_supported"},
        "constraint",
    )
    kind = raw["kind"]
    if kind not in _CONSTRAINT_KINDS:
        raise ValueError("constraint.kind is unsupported")
    derivative_supported = raw["derivative_supported"]
    if not isinstance(derivative_supported, bool):
        raise ValueError("constraint.derivative_supported must be boolean")
    if kind == "differentiable_scalar" and not derivative_supported:
        raise ValueError("differentiable_scalar constraints must support native derivatives")
    if kind == "forward_only" and derivative_supported:
        raise ValueError("forward_only constraints cannot claim derivative support")
    lower = None if raw["lower"] is None else _finite(raw["lower"], "constraint.lower")
    upper = None if raw["upper"] is None else _finite(raw["upper"], "constraint.upper")
    if lower is None and upper is None:
        raise ValueError("constraint must declare a lower or upper bound")
    if lower is not None and upper is not None and lower > upper:
        raise ValueError("constraint bounds are reversed")
    return {
        "constraint_id": _identifier(raw["constraint_id"], "constraint.constraint_id"),
        "kind": kind,
        "expression": _text(raw["expression"], "constraint.expression", maximum=512),
        "unit": _text(raw["unit"], "constraint.unit", maximum=32),
        "lower": lower,
        "upper": upper,
        "derivative_supported": derivative_supported,
    }


def normalize_derivative_support(value: object) -> dict[str, Any]:
    """Normalize a complete versioned support declaration without native imports."""
    bounded = _bounded_json(value, "derivative support")
    supplied = None
    if isinstance(bounded, dict) and "support_fingerprint" in bounded:
        supplied = bounded.pop("support_fingerprint")
    raw = _object(
        bounded,
        {
            "schema_name",
            "schema_version",
            "contract_id",
            "comsol_version",
            "comsol_build",
            "required_products",
            "adapter_id",
            "adapter_version",
            "source_identity",
            "study_identity",
            "derivative_method",
            "variables",
            "objective",
            "constraints",
            "mesh_policy",
            "nondifferentiable_events",
            "result_identity",
            "support_state",
        },
        "derivative support",
    )
    if (
        raw["schema_name"] != DERIVATIVE_SUPPORT_SCHEMA_NAME
        or raw["schema_version"] != DERIVATIVE_SUPPORT_SCHEMA_VERSION
    ):
        raise ValueError("derivative support schema identity is unsupported")
    if raw["derivative_method"] not in _METHODS:
        raise ValueError("derivative_method is unsupported")
    products = raw["required_products"]
    if not isinstance(products, list) or not 1 <= len(products) <= 16:
        raise ValueError("required_products must be a bounded nonempty list")
    normalized_products = [_text(item, "required_products", maximum=128) for item in products]
    if len(normalized_products) != len(set(normalized_products)):
        raise ValueError("required_products must be unique")
    variables = raw["variables"]
    if not isinstance(variables, list) or not 1 <= len(variables) <= MAX_VARIABLES:
        raise ValueError("variables must be a bounded nonempty list")
    normalized_variables = [
        normalize_derivative_variable(item, index=i) for i, item in enumerate(variables)
    ]
    ids = [item["variable_id"] for item in normalized_variables]
    if len(ids) != len(set(ids)):
        raise ValueError("variable_id values must be unique")
    mapping_keys = [
        (
            item["mapping"]["feature_tag"],
            item["mapping"]["property_name"],
            item["mapping"]["readback_expression"],
        )
        for item in normalized_variables
    ]
    if len(mapping_keys) != len(set(mapping_keys)):
        raise ValueError("variable mappings must be unique")
    constraints = raw["constraints"]
    if not isinstance(constraints, list) or len(constraints) > MAX_CONSTRAINTS:
        raise ValueError("constraints must be a bounded list")
    normalized_constraints = [normalize_derivative_constraint(item) for item in constraints]
    constraint_ids = [item["constraint_id"] for item in normalized_constraints]
    if len(constraint_ids) != len(set(constraint_ids)):
        raise ValueError("constraint_id values must be unique")
    mesh = _object(
        raw["mesh_policy"],
        {"topology", "selection", "quality_expression", "finalist_remesh"},
        "mesh_policy",
    )
    if mesh["topology"] != "fixed" or mesh["selection"] != "preserve":
        raise ValueError("mesh_policy must preserve fixed topology and selections")
    if not isinstance(mesh["finalist_remesh"], bool):
        raise ValueError("mesh_policy.finalist_remesh must be boolean")
    result = _object(
        raw["result_identity"],
        {"study_tag", "solution_tag", "dataset_tag", "derivative_expression", "derivative_units"},
        "result_identity",
    )
    normalized_result = {
        "study_tag": _identifier(result["study_tag"], "result_identity.study_tag"),
        "solution_tag": _identifier(result["solution_tag"], "result_identity.solution_tag"),
        "dataset_tag": _identifier(result["dataset_tag"], "result_identity.dataset_tag"),
        "derivative_expression": _text(
            result["derivative_expression"], "result_identity.derivative_expression", maximum=512
        ),
        "derivative_units": _text(
            result["derivative_units"], "result_identity.derivative_units", maximum=64
        ),
    }
    objective = normalize_derivative_objective(raw["objective"])
    if any(
        objective[key] != normalized_result[key]
        for key in ("study_tag", "solution_tag", "dataset_tag")
    ):
        raise ValueError("objective and derivative result identities must match")
    events = raw["nondifferentiable_events"]
    if not isinstance(events, list) or len(events) > 32:
        raise ValueError("nondifferentiable_events must be a bounded list")
    normalized_events = [_identifier(item, "nondifferentiable_events") for item in events]
    if len(normalized_events) != len(set(normalized_events)):
        raise ValueError("nondifferentiable_events must be unique")
    normalized = {
        "schema_name": DERIVATIVE_SUPPORT_SCHEMA_NAME,
        "schema_version": DERIVATIVE_SUPPORT_SCHEMA_VERSION,
        "contract_id": _identifier(raw["contract_id"], "contract_id"),
        "comsol_version": _text(raw["comsol_version"], "comsol_version", maximum=32),
        "comsol_build": _text(raw["comsol_build"], "comsol_build", maximum=64),
        "required_products": sorted(normalized_products),
        "adapter_id": _identifier(raw["adapter_id"], "adapter_id"),
        "adapter_version": _text(raw["adapter_version"], "adapter_version", maximum=32),
        "source_identity": _sha256(raw["source_identity"], "source_identity"),
        "study_identity": _sha256(raw["study_identity"], "study_identity"),
        "derivative_method": raw["derivative_method"],
        "variables": normalized_variables,
        "objective": objective,
        "constraints": sorted(normalized_constraints, key=lambda item: item["constraint_id"]),
        "mesh_policy": {
            "topology": "fixed",
            "selection": "preserve",
            "quality_expression": _text(
                mesh["quality_expression"], "mesh_policy.quality_expression", maximum=256
            ),
            "finalist_remesh": mesh["finalist_remesh"],
        },
        "nondifferentiable_events": sorted(normalized_events),
        "result_identity": normalized_result,
        "support_state": raw["support_state"],
    }
    if normalized["support_state"] not in _SUPPORT_STATES:
        raise ValueError("support_state is unsupported")
    normalized["support_fingerprint"] = domain_sha256_v2(DERIVATIVE_SUPPORT_SCHEMA_NAME, normalized)
    if supplied is not None and supplied != normalized["support_fingerprint"]:
        raise ValueError("derivative support fingerprint is invalid")
    return normalized


__all__ = [
    "DERIVATIVE_CONSTRAINT_SCHEMA_NAME",
    "DERIVATIVE_CONSTRAINT_SCHEMA_VERSION",
    "DERIVATIVE_OBJECTIVE_SCHEMA_NAME",
    "DERIVATIVE_OBJECTIVE_SCHEMA_VERSION",
    "DERIVATIVE_SUPPORT_SCHEMA_NAME",
    "DERIVATIVE_SUPPORT_SCHEMA_VERSION",
    "DERIVATIVE_VARIABLE_SCHEMA_NAME",
    "DERIVATIVE_VARIABLE_SCHEMA_VERSION",
    "normalize_derivative_constraint",
    "normalize_derivative_objective",
    "normalize_derivative_support",
    "normalize_derivative_variable",
]
