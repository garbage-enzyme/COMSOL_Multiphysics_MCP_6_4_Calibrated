"""Closed, bounded, deterministic research-goal and design-space contracts."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from comsol_mcp.durable import domain_sha256_v2, validate_finite_json

RESEARCH_GOAL_SCHEMA_NAME = "comsol_mcp.research_goal"
RESEARCH_GOAL_SCHEMA_VERSION = "1.0.0"
DESIGN_SPACE_SCHEMA_NAME = "comsol_mcp.design_space"
DESIGN_SPACE_SCHEMA_VERSION = "1.0.0"
MAX_GOAL_BYTES = 512 * 1024
MAX_DESIGN_SPACE_BYTES = 512 * 1024
MAX_OBJECTIVES = 16
MAX_CONSTRAINTS = 128
MAX_VARIABLES = 128
MAX_ALLOWED_VALUES = 256
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_DIRECTIONS = {"minimize", "maximize", "match", "range", "pareto"}
_OUTPUT_INTENTS = {"exploratory", "engineering_candidate", "publication_oriented"}
_AUTONOMY_LEVELS = {"A1_supervised_point", "A2_bounded_campaign", "A3_adaptive_campaign"}
_VARIABLE_KINDS = {"continuous", "integer", "categorical", "ordinal", "conditional"}
_DEPENDENCY_CLASSES = {"geometry", "material", "mesh", "physics", "study", "postprocess"}
_OPERATORS = {"eq", "le", "ge", "lt", "gt", "in"}


def relative_bounds(baseline: object, fraction: object = 0.25) -> tuple[float, float]:
    """Return closed multiplicative bounds around a finite baseline."""
    center = _finite(baseline, "baseline")
    span = _finite(fraction, "fraction", minimum=0.0)
    if center <= 0.0 or not 0.0 < span < 1.0:
        raise ValueError("baseline must be positive and fraction must be between zero and one")
    return (center * (1.0 - span), center * (1.0 + span))


def _object(value: object, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object with string keys")
    result = dict(value)
    if set(result) != fields:
        missing = sorted(fields - set(result))
        unknown = sorted(set(result) - fields)
        raise ValueError(f"{name} fields mismatch; missing={missing}, unknown={unknown}")
    return result


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a bounded identifier")
    return value


def _text(value: object, name: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a bounded nonempty string")
    return value.strip()


def _optional_text(value: object, name: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, name, maximum=maximum)


def _finite(value: object, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise ValueError(f"{name} must be a finite number in the allowed range")
    return number


def _integer(value: object, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _bounded_json(value: object, name: str, maximum_bytes: int) -> Any:
    validate_finite_json(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(encoded) > maximum_bytes:
        raise ValueError(f"{name} exceeds the bounded JSON size")
    return json.loads(encoded)


def _timestamp(value: object, name: str) -> str:
    text = _text(value, name, maximum=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include an explicit timezone")
    return text


def _normalize_objective(value: object, index: int) -> dict[str, Any]:
    name = f"objectives[{index}]"
    raw = _object(
        value,
        {"objective_id", "observable", "direction", "metric", "unit", "target", "tolerance"},
        name,
    )
    direction = raw["direction"]
    if direction not in _DIRECTIONS:
        raise ValueError(f"{name}.direction is unsupported")
    target = raw["target"]
    if direction == "range":
        if not isinstance(target, list) or len(target) != 2:
            raise ValueError(f"{name}.target must contain two bounds for a range objective")
        target_value_list = [
            _finite(target[0], f"{name}.target[0]"),
            _finite(target[1], f"{name}.target[1]"),
        ]
        if target_value_list[0] > target_value_list[1]:
            raise ValueError(f"{name}.target range is reversed")
        target_value: float | list[float] = target_value_list
    else:
        target_value = _finite(target, f"{name}.target")
    tolerance = (
        None
        if raw["tolerance"] is None
        else _finite(raw["tolerance"], f"{name}.tolerance", minimum=0.0)
    )
    return {
        "objective_id": _identifier(raw["objective_id"], f"{name}.objective_id"),
        "observable": _identifier(raw["observable"], f"{name}.observable"),
        "direction": direction,
        "metric": _identifier(raw["metric"], f"{name}.metric"),
        "unit": _text(raw["unit"], f"{name}.unit", maximum=32),
        "target": target_value,
        "tolerance": tolerance,
    }


def _normalize_constraint(value: object, index: int) -> dict[str, Any]:
    name = f"constraints[{index}]"
    raw = _object(value, {"constraint_id", "kind", "variable_ids", "operator", "value"}, name)
    variable_ids = raw["variable_ids"]
    if not isinstance(variable_ids, list) or not 1 <= len(variable_ids) <= MAX_VARIABLES:
        raise ValueError(f"{name}.variable_ids must be a bounded nonempty list")
    normalized_ids = [
        _identifier(item, f"{name}.variable_ids[{i}]") for i, item in enumerate(variable_ids)
    ]
    if len(normalized_ids) != len(set(normalized_ids)):
        raise ValueError(f"{name}.variable_ids must be unique")
    if raw["operator"] not in _OPERATORS:
        raise ValueError(f"{name}.operator is unsupported")
    return {
        "constraint_id": _identifier(raw["constraint_id"], f"{name}.constraint_id"),
        "kind": _identifier(raw["kind"], f"{name}.kind"),
        "variable_ids": sorted(normalized_ids),
        "operator": raw["operator"],
        "value": _bounded_json(raw["value"], f"{name}.value", 16 * 1024),
    }


def normalize_research_goal(value: object) -> dict[str, Any]:
    """Normalize a complete goal without loading an optimizer or solver."""
    raw = _object(
        _bounded_json(value, "research goal", MAX_GOAL_BYTES),
        {
            "schema_name",
            "schema_version",
            "goal_id",
            "title",
            "user_statement",
            "owner",
            "created_at",
            "objectives",
            "constraints",
            "autonomy_level",
            "output_intent",
            "resource_budget",
            "stop_policy",
            "target_data",
            "fidelity_policy",
            "evidence_policy",
        },
        "research goal",
    )
    if (
        raw["schema_name"] != RESEARCH_GOAL_SCHEMA_NAME
        or raw["schema_version"] != RESEARCH_GOAL_SCHEMA_VERSION
    ):
        raise ValueError("research goal schema identity is unsupported")
    objectives = raw["objectives"]
    if not isinstance(objectives, list) or not 1 <= len(objectives) <= MAX_OBJECTIVES:
        raise ValueError("objectives must be a bounded nonempty list")
    normalized_objectives = [_normalize_objective(item, i) for i, item in enumerate(objectives)]
    objective_ids = [item["objective_id"] for item in normalized_objectives]
    if len(objective_ids) != len(set(objective_ids)):
        raise ValueError("objective_id values must be unique")
    constraints = raw["constraints"]
    if not isinstance(constraints, list) or len(constraints) > MAX_CONSTRAINTS:
        raise ValueError("constraints must be a bounded list")
    normalized_constraints = [_normalize_constraint(item, i) for i, item in enumerate(constraints)]
    constraint_ids = [item["constraint_id"] for item in normalized_constraints]
    if len(constraint_ids) != len(set(constraint_ids)):
        raise ValueError("constraint_id values must be unique")
    budget = _object(
        raw["resource_budget"],
        {"max_fem_evaluations", "max_wall_time_seconds", "max_memory_bytes", "max_disk_bytes"},
        "resource_budget",
    )
    normalized_budget = {
        "max_fem_evaluations": _integer(
            budget["max_fem_evaluations"],
            "resource_budget.max_fem_evaluations",
            minimum=1,
            maximum=4096,
        ),
        "max_wall_time_seconds": _integer(
            budget["max_wall_time_seconds"],
            "resource_budget.max_wall_time_seconds",
            minimum=1,
            maximum=30 * 24 * 3600,
        ),
        "max_memory_bytes": _integer(
            budget["max_memory_bytes"],
            "resource_budget.max_memory_bytes",
            minimum=1,
            maximum=1 << 50,
        ),
        "max_disk_bytes": _integer(
            budget["max_disk_bytes"], "resource_budget.max_disk_bytes", minimum=1, maximum=1 << 50
        ),
    }
    stop = _object(
        raw["stop_policy"], {"success", "infeasible", "stagnation", "budget"}, "stop_policy"
    )
    normalized_stop = {key: _text(stop[key], f"stop_policy.{key}", maximum=64) for key in stop}
    if raw["autonomy_level"] not in _AUTONOMY_LEVELS:
        raise ValueError("autonomy_level is unsupported")
    if raw["output_intent"] not in _OUTPUT_INTENTS:
        raise ValueError("output_intent is unsupported")
    target_data = _bounded_json(raw["target_data"], "target_data", 128 * 1024)
    normalized = {
        "schema_name": RESEARCH_GOAL_SCHEMA_NAME,
        "schema_version": RESEARCH_GOAL_SCHEMA_VERSION,
        "goal_id": _identifier(raw["goal_id"], "goal_id"),
        "title": _text(raw["title"], "title", maximum=256),
        "user_statement": _text(raw["user_statement"], "user_statement", maximum=4096),
        "owner": _optional_text(raw["owner"], "owner", maximum=256),
        "created_at": _timestamp(raw["created_at"], "created_at"),
        "objectives": sorted(normalized_objectives, key=lambda item: item["objective_id"]),
        "constraints": sorted(normalized_constraints, key=lambda item: item["constraint_id"]),
        "autonomy_level": raw["autonomy_level"],
        "output_intent": raw["output_intent"],
        "resource_budget": normalized_budget,
        "stop_policy": normalized_stop,
        "target_data": target_data,
        "fidelity_policy": _bounded_json(raw["fidelity_policy"], "fidelity_policy", 32 * 1024),
        "evidence_policy": _bounded_json(raw["evidence_policy"], "evidence_policy", 32 * 1024),
    }
    normalized["goal_fingerprint"] = domain_sha256_v2(RESEARCH_GOAL_SCHEMA_NAME, normalized)
    return normalized


def _normalize_variable(value: object, index: int) -> dict[str, Any]:
    name = f"variables[{index}]"
    raw = _object(
        value,
        {
            "variable_id",
            "kind",
            "unit",
            "baseline",
            "lower",
            "upper",
            "allowed_values",
            "dependency_class",
            "adapter_path",
        },
        name,
    )
    kind = raw["kind"]
    if kind not in _VARIABLE_KINDS:
        raise ValueError(f"{name}.kind is unsupported")
    dependency = raw["dependency_class"]
    if dependency not in _DEPENDENCY_CLASSES:
        raise ValueError(f"{name}.dependency_class is unsupported")
    if kind in {"categorical", "ordinal"}:
        validate_finite_json(raw["baseline"])
        baseline = raw["baseline"]
    elif kind == "integer":
        if isinstance(raw["baseline"], bool) or not isinstance(raw["baseline"], int):
            raise ValueError(f"{name}.baseline must be an integer")
        baseline = raw["baseline"]
    else:
        baseline = _finite(raw["baseline"], f"{name}.baseline")
    lower = _finite(raw["lower"], f"{name}.lower") if raw["lower"] is not None else None
    upper = _finite(raw["upper"], f"{name}.upper") if raw["upper"] is not None else None
    if kind in {"continuous", "integer", "conditional"}:
        if lower is None or upper is None or lower >= upper or not lower <= baseline <= upper:
            raise ValueError(f"{name} must declare ordered bounds containing baseline")
        if kind == "integer" and (not lower.is_integer() or not upper.is_integer()):
            raise ValueError(f"{name}.bounds must be integers")
    allowed = raw["allowed_values"]
    if allowed is not None:
        if not isinstance(allowed, list) or not 1 <= len(allowed) <= MAX_ALLOWED_VALUES:
            raise ValueError(f"{name}.allowed_values is invalid")
        if any(item is None or isinstance(item, (list, dict)) for item in allowed):
            raise ValueError(f"{name}.allowed_values must contain JSON scalars")
        encoded_allowed = [
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for item in allowed
        ]
        if len(encoded_allowed) != len(set(encoded_allowed)):
            raise ValueError(f"{name}.allowed_values must be unique")
        allowed = [item for _, item in sorted(zip(encoded_allowed, allowed, strict=True))]
    if kind in {"categorical", "ordinal"}:
        baseline_encoded = json.dumps(
            baseline, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if allowed is None or baseline_encoded not in {
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for item in allowed
        }:
            raise ValueError(f"{name} requires allowed_values containing baseline")
    return {
        "variable_id": _identifier(raw["variable_id"], f"{name}.variable_id"),
        "kind": kind,
        "unit": _text(raw["unit"], f"{name}.unit", maximum=32),
        "baseline": baseline,
        "lower": lower,
        "upper": upper,
        "allowed_values": allowed,
        "dependency_class": dependency,
        "adapter_path": _text(raw["adapter_path"], f"{name}.adapter_path", maximum=512),
    }


def normalize_design_space(value: object) -> dict[str, Any]:
    """Normalize one closed design space and derive an optimizer-independent fingerprint."""
    raw = _object(
        _bounded_json(value, "design space", MAX_DESIGN_SPACE_BYTES),
        {
            "schema_name",
            "schema_version",
            "space_id",
            "structure_family",
            "template_identity",
            "variables",
            "constraints",
            "canonicalization",
            "adapter_mappings",
        },
        "design space",
    )
    if (
        raw["schema_name"] != DESIGN_SPACE_SCHEMA_NAME
        or raw["schema_version"] != DESIGN_SPACE_SCHEMA_VERSION
    ):
        raise ValueError("design-space schema identity is unsupported")
    variables = raw["variables"]
    if not isinstance(variables, list) or not 1 <= len(variables) <= MAX_VARIABLES:
        raise ValueError("variables must be a bounded nonempty list")
    normalized_variables = [_normalize_variable(item, i) for i, item in enumerate(variables)]
    variable_ids = [item["variable_id"] for item in normalized_variables]
    if len(variable_ids) != len(set(variable_ids)):
        raise ValueError("variable_id values must be unique")
    constraints = raw["constraints"]
    if not isinstance(constraints, list) or len(constraints) > MAX_CONSTRAINTS:
        raise ValueError("constraints must be a bounded list")
    normalized_constraints = [_normalize_constraint(item, i) for i, item in enumerate(constraints)]
    for constraint in normalized_constraints:
        missing = set(constraint["variable_ids"]) - set(variable_ids)
        if missing:
            raise ValueError("constraints must reference declared variables")
    canonicalization = _object(
        raw["canonicalization"], {"float_digits", "relative_tolerance"}, "canonicalization"
    )
    normalized_canonicalization = {
        "float_digits": _integer(
            canonicalization["float_digits"], "canonicalization.float_digits", minimum=0, maximum=15
        ),
        "relative_tolerance": _finite(
            canonicalization["relative_tolerance"],
            "canonicalization.relative_tolerance",
            minimum=0.0,
        ),
    }
    mappings = raw["adapter_mappings"]
    if not isinstance(mappings, list) or len(mappings) != len(variable_ids):
        raise ValueError("adapter_mappings must contain one mapping per variable")
    normalized_mappings = []
    for index, mapping in enumerate(mappings):
        item = _object(
            mapping, {"variable_id", "adapter_path", "unit"}, f"adapter_mappings[{index}]"
        )
        variable_id = _identifier(item["variable_id"], f"adapter_mappings[{index}].variable_id")
        if variable_id not in variable_ids:
            raise ValueError("adapter_mappings must reference declared variables")
        normalized_mappings.append(
            {
                "variable_id": variable_id,
                "adapter_path": _text(item["adapter_path"], "adapter_path", maximum=512),
                "unit": _text(item["unit"], "unit", maximum=32),
            }
        )
    if {item["variable_id"] for item in normalized_mappings} != set(variable_ids):
        raise ValueError("adapter_mappings must cover each variable exactly once")
    normalized = {
        "schema_name": DESIGN_SPACE_SCHEMA_NAME,
        "schema_version": DESIGN_SPACE_SCHEMA_VERSION,
        "space_id": _identifier(raw["space_id"], "space_id"),
        "structure_family": _identifier(raw["structure_family"], "structure_family"),
        "template_identity": _bounded_json(
            raw["template_identity"], "template_identity", 32 * 1024
        ),
        "variables": sorted(normalized_variables, key=lambda item: item["variable_id"]),
        "constraints": sorted(normalized_constraints, key=lambda item: item["constraint_id"]),
        "canonicalization": normalized_canonicalization,
        "adapter_mappings": sorted(normalized_mappings, key=lambda item: item["variable_id"]),
    }
    normalized["space_fingerprint"] = domain_sha256_v2(DESIGN_SPACE_SCHEMA_NAME, normalized)
    return normalized


__all__ = [
    "DESIGN_SPACE_SCHEMA_NAME",
    "DESIGN_SPACE_SCHEMA_VERSION",
    "RESEARCH_GOAL_SCHEMA_NAME",
    "RESEARCH_GOAL_SCHEMA_VERSION",
    "normalize_design_space",
    "normalize_research_goal",
    "relative_bounds",
]
