"""Deterministic finalist perturbation and robustness-summary contracts."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from statistics import fmean
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .contracts import normalize_design_space


def axis_perturbation_matrix(
    design_space: object, candidate_values: object, *, relative_fraction: float
) -> dict[str, Any]:
    space = normalize_design_space(design_space)
    if (
        isinstance(relative_fraction, bool)
        or not isinstance(relative_fraction, (int, float))
        or not math.isfinite(float(relative_fraction))
        or not 0.0 < float(relative_fraction) <= 0.25
    ):
        raise ValueError("relative_fraction must be finite and in (0, 0.25]")
    variables = list(space["variables"])
    if any(item["kind"] != "continuous" for item in variables):
        raise ValueError("axis perturbation requires continuous variables")
    expected = {item["variable_id"] for item in variables}
    if not isinstance(candidate_values, Mapping) or set(candidate_values) != expected:
        raise ValueError("candidate_values must exactly cover the frozen space")
    center: dict[str, float] = {}
    for variable in variables:
        variable_id = variable["variable_id"]
        raw = candidate_values[variable_id]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError("candidate values must be finite numbers")
        value = float(raw)
        if not math.isfinite(value) or not float(variable["lower"]) <= value <= float(
            variable["upper"]
        ):
            raise ValueError("candidate values are outside the frozen space")
        center[variable_id] = value
    points = [{"point_id": "center", "values": dict(sorted(center.items()))}]
    for variable in variables:
        variable_id = variable["variable_id"]
        delta = abs(center[variable_id]) * float(relative_fraction)
        if delta == 0.0:
            delta = (float(variable["upper"]) - float(variable["lower"])) * float(relative_fraction)
        for direction, sign in (("minus", -1.0), ("plus", 1.0)):
            perturbed = dict(center)
            perturbed[variable_id] += sign * delta
            if not float(variable["lower"]) <= perturbed[variable_id] <= float(variable["upper"]):
                raise ValueError("candidate lacks the declared perturbation margin")
            points.append(
                {
                    "point_id": f"{variable_id}:{direction}",
                    "values": dict(sorted(perturbed.items())),
                }
            )
    body = {
        "schema_name": "research.robustness.matrix",
        "schema_version": "1.0.0",
        "space_fingerprint": space["space_fingerprint"],
        "relative_fraction": float(relative_fraction),
        "points": points,
    }
    return {**body, "matrix_fingerprint": domain_sha256_v2(body["schema_name"], body)}


def summarize_robustness(
    matrix: object, rows: object, *, maximum_total_loss: float | None
) -> dict[str, Any]:
    if not isinstance(matrix, Mapping) or not isinstance(rows, Sequence):
        raise ValueError("matrix and rows must be structured values")
    expected = [item["point_id"] for item in matrix.get("points", [])]
    losses: dict[str, float] = {}
    evidence: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {
            "point_id",
            "total_loss",
            "evidence_fingerprint",
        }:
            raise ValueError("robustness row shape is unsupported")
        point_id, raw, fingerprint = row["point_id"], row["total_loss"], row["evidence_fingerprint"]
        if point_id not in expected or point_id in losses:
            raise ValueError("robustness rows contain unknown or duplicate points")
        if (
            isinstance(raw, bool)
            or not isinstance(raw, (int, float))
            or not math.isfinite(float(raw))
            or float(raw) < 0.0
        ):
            raise ValueError("robustness loss must be finite and nonnegative")
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            raise ValueError("robustness evidence fingerprint is invalid")
        losses[point_id], evidence[point_id] = float(raw), fingerprint
    if set(losses) != set(expected):
        raise ValueError("robustness rows must exactly cover the matrix")
    threshold = None
    if maximum_total_loss is not None:
        if (
            isinstance(maximum_total_loss, bool)
            or not isinstance(maximum_total_loss, (int, float))
            or not math.isfinite(float(maximum_total_loss))
            or float(maximum_total_loss) < 0.0
        ):
            raise ValueError("maximum_total_loss must be finite and nonnegative")
        threshold = float(maximum_total_loss)
    values = [losses[point_id] for point_id in expected]
    body = {
        "schema_name": "research.robustness.summary",
        "schema_version": "1.0.0",
        "matrix_fingerprint": matrix["matrix_fingerprint"],
        "evidence_fingerprints": dict(sorted(evidence.items())),
        "minimum_total_loss": min(values),
        "mean_total_loss": fmean(values),
        "maximum_total_loss": max(values),
        "required_maximum_total_loss": threshold,
        "threshold_outcome": "not_declared"
        if threshold is None
        else ("pass" if max(values) <= threshold else "fail"),
    }
    return {**body, "summary_fingerprint": domain_sha256_v2(body["schema_name"], body)}


__all__ = ["axis_perturbation_matrix", "summarize_robustness"]
