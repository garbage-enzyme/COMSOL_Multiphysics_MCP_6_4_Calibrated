"""Deterministic objective scoring from exact evidence pointers."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .contracts import _bounded_json, _identifier, _object, normalize_research_goal

OBJECTIVE_SCORE_SCHEMA_NAME = "comsol_mcp.research_objective_score"
OBJECTIVE_SCORE_SCHEMA_VERSION = "1.0.0"
MAX_POINTER_DEPTH = 32


def _read_pointer(evidence: object, path: object, name: str) -> tuple[list[str], object]:
    if not isinstance(path, list) or not 1 <= len(path) <= MAX_POINTER_DEPTH:
        raise ValueError(f"{name}.path must be a bounded nonempty list")
    normalized_path = [
        _identifier(item, f"{name}.path[{index}]") for index, item in enumerate(path)
    ]
    current = evidence
    for key in normalized_path:
        if not isinstance(current, Mapping) or key not in current:
            raise ValueError(f"{name}.path does not resolve in evidence")
        current = current[key]
    return normalized_path, current


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must resolve to a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must resolve to a finite number")
    return number


def _score(objective: Mapping[str, Any], observed: float) -> tuple[float | None, bool | None]:
    direction = objective["direction"]
    metric = objective["metric"]
    target = objective["target"]
    tolerance = objective["tolerance"]
    if direction == "match":
        error = abs(observed - float(target))
        if metric == "relative_error":
            denominator = abs(float(target))
            if denominator == 0.0:
                raise ValueError("relative_error requires a nonzero target")
            error /= denominator
        elif metric != "absolute_error":
            raise ValueError("match objectives require absolute_error or relative_error")
        return error, None if tolerance is None else error <= float(tolerance)
    if direction == "range":
        lower, upper = (float(item) for item in target)
        violation = max(lower - observed, 0.0, observed - upper)
        if metric == "relative_error":
            denominator = max(abs(lower), abs(upper))
            if denominator == 0.0:
                raise ValueError("relative_error requires a nonzero target range")
            violation /= denominator
        elif metric != "absolute_error":
            raise ValueError("range objectives require absolute_error or relative_error")
        return violation, None if tolerance is None else violation <= float(tolerance)
    if direction == "minimize":
        if metric != "raw_value":
            raise ValueError("minimize objectives require raw_value")
        return observed, None if tolerance is None else observed <= float(target) + float(tolerance)
    if direction == "maximize":
        if metric != "raw_value":
            raise ValueError("maximize objectives require raw_value")
        return -observed, None if tolerance is None else observed >= float(target) - float(
            tolerance
        )
    if direction == "pareto":
        if metric != "raw_value":
            raise ValueError("pareto objectives require raw_value")
        return None, None
    raise ValueError("objective direction is unsupported")


def score_objectives(goal: object, evidence: object, pointers: object) -> dict[str, Any]:
    """Score frozen objectives without changing evidence or inventing thresholds."""
    normalized_goal = normalize_research_goal(goal)
    normalized_evidence = _bounded_json(evidence, "objective evidence", 2 * 1024 * 1024)
    if not isinstance(pointers, list) or len(pointers) != len(normalized_goal["objectives"]):
        raise ValueError("pointers must contain exactly one entry per objective")
    pointer_by_id: dict[str, dict[str, Any]] = {}
    for index, pointer_value in enumerate(pointers):
        name = f"pointers[{index}]"
        pointer = _object(pointer_value, {"objective_id", "path"}, name)
        objective_id = _identifier(pointer["objective_id"], f"{name}.objective_id")
        if objective_id in pointer_by_id:
            raise ValueError("objective pointers must be unique")
        path, observed = _read_pointer(normalized_evidence, pointer["path"], name)
        pointer_by_id[objective_id] = {"path": path, "observed": observed}
    objective_ids = {item["objective_id"] for item in normalized_goal["objectives"]}
    if set(pointer_by_id) != objective_ids:
        raise ValueError("objective pointers must exactly cover declared objectives")
    scores = []
    for objective in normalized_goal["objectives"]:
        objective_id = objective["objective_id"]
        pointer = pointer_by_id[objective_id]
        observed = _number(pointer["observed"], f"objective {objective_id}")
        loss, threshold_met = _score(objective, observed)
        scores.append(
            {
                "objective_id": objective_id,
                "evidence_path": pointer["path"],
                "observed_value": observed,
                "unit": objective["unit"],
                "direction": objective["direction"],
                "metric": objective["metric"],
                "target": objective["target"],
                "tolerance": objective["tolerance"],
                "normalized_loss": loss,
                "threshold_met": threshold_met,
            }
        )
    dispositions = [item["threshold_met"] for item in scores]
    success_claim_allowed = all(item is not None for item in dispositions)
    body = {
        "schema_name": OBJECTIVE_SCORE_SCHEMA_NAME,
        "schema_version": OBJECTIVE_SCORE_SCHEMA_VERSION,
        "goal_fingerprint": normalized_goal["goal_fingerprint"],
        "evidence_fingerprint": domain_sha256_v2(
            "comsol_mcp.research_objective_evidence", normalized_evidence
        ),
        "scores": scores,
        "success_claim_allowed": success_claim_allowed,
        "all_thresholds_met": (
            all(bool(item) for item in dispositions) if success_claim_allowed else None
        ),
    }
    return {**body, "score_fingerprint": domain_sha256_v2(OBJECTIVE_SCORE_SCHEMA_NAME, body)}


__all__ = [
    "OBJECTIVE_SCORE_SCHEMA_NAME",
    "OBJECTIVE_SCORE_SCHEMA_VERSION",
    "score_objectives",
]
