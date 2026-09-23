"""Solver-free dataset row provenance and deterministic DOE design.

This module never imports COMSOL, Java, MPh, or network clients.  Every training
label is bound to the exact candidate, model, solver, study, solution, dataset,
fidelity, and evidence identities that produced it, so a surrogate row can never
masquerade as a completed FEM row.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from typing import Any

from comsol_mcp.durable.canonical import canonical_sha256_v1

SCHEMA_VERSION = "1.0.0"
STRATEGY_VERSION = "lhs_sha256_v1"

MAX_ROWS = 4096
MAX_DIM = 12
MAX_SEED = 2**31 - 1

# Identity fields every row must bind.  A missing identity is a hard failure:
# a label whose provenance cannot be named is not usable as training data.
ROW_IDENTITY_FIELDS = (
    "candidate_id",
    "source_model_sha256",
    "solver_identity_sha256",
    "study_identity",
    "solution_identity",
    "dataset_identity",
    "fidelity",
    "evidence_state",
)

# Evidence states a label row may carry.  Only a verified FEM row is eligible.
ROW_EVIDENCE_STATES = (
    "verified",
    "label_only",
    "derived_from_declared_convention",
    "not_requested",
    "unknown",
)

# Reason codes for rows that must be retained but excluded from fitting.
INELIGIBLE_REASON_CODES = (
    "label_missing",
    "fem_failed",
    "nonconverged",
    "outside_design_domain",
    "identity_mismatch",
    "nonfinite_target",
    "support_mismatch",
    "evidence_incomplete",
    "duplicate_family",
)


def _require_str(name: str, value: Any, *, max_len: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise ValueError(f"{name} must be a non-empty string up to {max_len}")
    return value


def _require_hex64(name: str, value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{name} must be a 64-character hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{name} must be a 64-character hex digest") from exc
    return value.lower()


def _require_finite(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _require_seed(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_SEED:
        raise ValueError(f"seed must be an integer in 0..{MAX_SEED}")
    return value


def build_row_provenance(
    *,
    row_id: str,
    candidate_id: str,
    source_model_sha256: str,
    solver_identity_sha256: str,
    study_identity: str,
    solution_identity: str,
    dataset_identity: str,
    fidelity: str,
    evidence_state: str,
    features: Mapping[str, float],
    targets: Mapping[str, float],
    leakage_group_id: str,
    ineligible_reason: str | None = None,
) -> dict[str, Any]:
    """Bind one label row to its exact producing identities.

    ``evidence_state`` records how the label was produced.  A surrogate may only
    ever consume rows whose state is ``verified``; any other state is retained
    with an explicit reason and excluded from fitting.
    """
    if evidence_state not in ROW_EVIDENCE_STATES:
        raise ValueError(f"evidence_state must be one of: {', '.join(ROW_EVIDENCE_STATES)}")
    if not isinstance(features, Mapping) or not features:
        raise ValueError("features must be a non-empty mapping")
    if not isinstance(targets, Mapping) or not targets:
        raise ValueError("targets must be a non-empty mapping")
    normalized_features = {
        _require_str("feature name", key, max_len=64): _require_finite(f"features.{key}", value)
        for key, value in features.items()
    }
    normalized_targets = {
        _require_str("target name", key, max_len=64): _require_finite(f"targets.{key}", value)
        for key, value in targets.items()
    }

    if ineligible_reason is not None:
        if ineligible_reason not in INELIGIBLE_REASON_CODES:
            raise ValueError(
                f"ineligible_reason must be one of: {', '.join(INELIGIBLE_REASON_CODES)}"
            )
    elif evidence_state != "verified":
        # A non-verified row is never silently eligible.
        raise ValueError("a non-verified row requires an explicit ineligible_reason")

    body = {
        "schema": "comsol_mcp.surrogate_row_provenance",
        "schema_version": SCHEMA_VERSION,
        "row_id": _require_str("row_id", row_id),
        "candidate_id": _require_str("candidate_id", candidate_id),
        "source_model_sha256": _require_hex64("source_model_sha256", source_model_sha256),
        "solver_identity_sha256": _require_hex64("solver_identity_sha256", solver_identity_sha256),
        "study_identity": _require_str("study_identity", study_identity),
        "solution_identity": _require_str("solution_identity", solution_identity),
        "dataset_identity": _require_str("dataset_identity", dataset_identity),
        "fidelity": _require_str("fidelity", fidelity, max_len=64),
        "evidence_state": evidence_state,
        "leakage_group_id": _require_str("leakage_group_id", leakage_group_id),
        "features": dict(sorted(normalized_features.items())),
        "targets": dict(sorted(normalized_targets.items())),
        "eligible": ineligible_reason is None,
        "ineligible_reason": ineligible_reason,
        "is_surrogate_prediction": False,
        "upgrades_fem_evidence": False,
    }
    return {**body, "row_sha256": canonical_sha256_v1(body)}


def validate_row_provenance(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("row provenance must be a mapping")
    required = (
        "schema",
        "schema_version",
        "row_id",
        "candidate_id",
        "source_model_sha256",
        "solver_identity_sha256",
        "study_identity",
        "solution_identity",
        "dataset_identity",
        "fidelity",
        "evidence_state",
        "leakage_group_id",
        "features",
        "targets",
        "eligible",
        "ineligible_reason",
        "is_surrogate_prediction",
        "upgrades_fem_evidence",
    )
    if set(value) != set(required) | {"row_sha256"}:
        raise ValueError("row provenance keys are closed")
    if value["schema"] != "comsol_mcp.surrogate_row_provenance":
        raise ValueError("unexpected schema name")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported schema_version")
    body = {key: value[key] for key in required}
    if value["row_sha256"] != canonical_sha256_v1(body):
        raise ValueError("row_sha256 mismatch")
    if value["is_surrogate_prediction"] is not False:
        raise ValueError("a dataset label row is never a surrogate prediction")
    if value["upgrades_fem_evidence"] is not False:
        raise ValueError("a dataset label row never upgrades FEM evidence")
    if value["evidence_state"] not in ROW_EVIDENCE_STATES:
        raise ValueError("unknown evidence_state")
    if value["eligible"] is True and value["evidence_state"] != "verified":
        raise ValueError("only a verified row may be eligible")
    if value["eligible"] is True and value["ineligible_reason"] is not None:
        raise ValueError("an eligible row must not carry an ineligible_reason")
    if value["eligible"] is False and value["ineligible_reason"] is None:
        raise ValueError("an ineligible row requires an ineligible_reason")
    if value["ineligible_reason"] is not None:
        if value["ineligible_reason"] not in INELIGIBLE_REASON_CODES:
            raise ValueError("unknown ineligible_reason")
    return dict(value)


def summarize_row_ledger(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize a row ledger while retaining every failed row with its reason."""
    validated = [validate_row_provenance(row) for row in rows]
    row_ids = [row["row_id"] for row in validated]
    if len(set(row_ids)) != len(row_ids):
        raise ValueError("row_id values must be unique")
    eligible = [row for row in validated if row["eligible"]]
    ineligible = [row for row in validated if not row["eligible"]]
    reasons: dict[str, int] = {}
    for row in ineligible:
        reasons[row["ineligible_reason"]] = reasons.get(row["ineligible_reason"], 0) + 1
    groups = {row["leakage_group_id"] for row in validated}
    return {
        "row_count": len(validated),
        "eligible_count": len(eligible),
        "ineligible_count": len(ineligible),
        "ineligible_reasons": dict(sorted(reasons.items())),
        "leakage_group_count": len(groups),
        "failed_rows_retained": True,
        "ledger_sha256": canonical_sha256_v1([row["row_sha256"] for row in validated]),
    }


def _unit_hash(seed: int, index: int, dimension: int) -> float:
    """Return one deterministic uniform value in [0, 1)."""
    payload = f"{STRATEGY_VERSION}\x00{seed}\x00{index}\x00{dimension}".encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    # Use 53 bits so the value is exactly representable as a double.
    numerator = int.from_bytes(digest[:8], "big") >> 11
    return numerator / float(1 << 53)


def build_lhs_design(
    *,
    design_id: str,
    bounds: Mapping[str, Sequence[float]],
    row_count: int,
    seed: int,
) -> dict[str, Any]:
    """Build a bounded Latin-hypercube design deterministically.

    The design is a pure function of the declared bounds, row count, and seed,
    so a resumed generation can prove it reproduces the identical plan without
    re-solving anything.
    """
    if not isinstance(bounds, Mapping) or not bounds:
        raise ValueError("bounds must be a non-empty mapping")
    if not 2 <= len(bounds) <= MAX_DIM:
        raise ValueError(f"bounds must declare 2-{MAX_DIM} variables")
    if isinstance(row_count, bool) or not isinstance(row_count, int):
        raise ValueError("row_count must be an integer")
    if not 1 <= row_count <= MAX_ROWS:
        raise ValueError(f"row_count must be in 1..{MAX_ROWS}")
    seed = _require_seed(seed)

    names = sorted(str(name) for name in bounds)
    lower: list[float] = []
    upper: list[float] = []
    for name in names:
        pair = bounds[name]
        if not isinstance(pair, Sequence) or isinstance(pair, (str, bytes)) or len(pair) != 2:
            raise ValueError(f"bounds.{name} must be a lower/upper pair")
        low = _require_finite(f"bounds.{name}[0]", pair[0])
        high = _require_finite(f"bounds.{name}[1]", pair[1])
        if not low < high:
            raise ValueError(f"bounds.{name} requires lower < upper")
        lower.append(low)
        upper.append(high)

    dimension = len(names)
    points: list[dict[str, float]] = []
    for row in range(row_count):
        point: dict[str, float] = {}
        for column in range(dimension):
            # A stratified uniform sample: one draw per (row, column) cell.
            fraction = (_unit_hash(seed, row, column) + row) / row_count
            point[names[column]] = lower[column] + fraction * (upper[column] - lower[column])
        points.append(point)

    body = {
        "schema": "comsol_mcp.surrogate_lhs_design",
        "schema_version": SCHEMA_VERSION,
        "strategy": STRATEGY_VERSION,
        "design_id": _require_str("design_id", design_id),
        "seed": seed,
        "variable_order": names,
        "bounds": {name: [bounds[name][0], bounds[name][1]] for name in names},
        "row_count": row_count,
        "dimension": dimension,
        "points": points,
    }
    return {**body, "design_sha256": canonical_sha256_v1(body)}


def validate_lhs_design(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("design must be a mapping")
    required = (
        "schema",
        "schema_version",
        "strategy",
        "design_id",
        "seed",
        "variable_order",
        "bounds",
        "row_count",
        "dimension",
        "points",
    )
    if set(value) != set(required) | {"design_sha256"}:
        raise ValueError("design keys are closed")
    if value["schema"] != "comsol_mcp.surrogate_lhs_design":
        raise ValueError("unexpected schema name")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported schema_version")
    if value["strategy"] != STRATEGY_VERSION:
        raise ValueError("unsupported design strategy")
    body = {key: value[key] for key in required}
    if value["design_sha256"] != canonical_sha256_v1(body):
        raise ValueError("design_sha256 mismatch")
    rebuilt = build_lhs_design(
        design_id=value["design_id"],
        bounds={name: pair for name, pair in value["bounds"].items()},
        row_count=value["row_count"],
        seed=value["seed"],
    )
    if rebuilt != value:
        raise ValueError("design is not reproducible from its declared inputs")
    return dict(value)


def assert_design_within_bounds(
    design: Mapping[str, Any], *, tolerance: float = 1e-12
) -> dict[str, Any]:
    """Prove every design point lies inside its declared bounds."""
    validated = validate_lhs_design(design)
    if tolerance <= 0:
        raise ValueError("tolerance must be positive")
    checked = 0
    for index, point in enumerate(validated["points"]):
        for name in validated["variable_order"]:
            low, high = validated["bounds"][name]
            value = _require_finite(f"points[{index}].{name}", point[name])
            if value < low - tolerance or value > high + tolerance:
                raise ValueError(f"design point {index} escapes bounds for {name}")
            checked += 1
    return {
        "within_bounds": True,
        "points_checked": len(validated["points"]),
        "coordinates_checked": checked,
    }


__all__ = [
    "INELIGIBLE_REASON_CODES",
    "MAX_DIM",
    "MAX_ROWS",
    "ROW_EVIDENCE_STATES",
    "ROW_IDENTITY_FIELDS",
    "SCHEMA_VERSION",
    "STRATEGY_VERSION",
    "assert_design_within_bounds",
    "build_lhs_design",
    "build_row_provenance",
    "summarize_row_ledger",
    "validate_lhs_design",
    "validate_row_provenance",
]
