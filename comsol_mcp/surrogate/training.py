"""Solver-free surrogate metrics, non-DNN baselines, and continuation rules.

This module never imports COMSOL, Java, MPh, or network clients.  It computes
held-out metrics in physical units, fits deterministic non-DNN baselines on the
identical transforms and splits, and decides continuation eligibility from exact
contract identities.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from comsol_mcp.durable.canonical import canonical_sha256_v1

SCHEMA_VERSION = "1.0.0"

BASELINE_KINDS = ("constant_mean", "linear_least_squares", "ridge")
DEFAULT_RIDGE_LAMBDA = 1e-6

# Metrics a surrogate must report.  A model is never accepted on training loss.
METRIC_NAMES = (
    "mae",
    "rmse",
    "max_abs_error",
    "normalized_rmse",
    "r2",
    "worst_group_mae",
    "failed_prediction_count",
)

# Continuation identities that must match exactly.  Any difference creates a new
# lineage rather than resuming the same training run.
CONTINUATION_IDENTITY_FIELDS = (
    "dataset_manifest_sha256",
    "split_manifest_sha256",
    "field_schema_sha256",
    "transforms_sha256",
    "architecture_sha256",
    "comsol_build",
    "objective",
    "prior_checkpoint_sha256",
)

MAX_SAMPLES = 4096
MAX_TARGETS = 8


def _require_finite(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _require_hex64(name: str, value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{name} must be a 64-character hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{name} must be a 64-character hex digest") from exc
    return value.lower()


def _require_str(name: str, value: Any, *, max_len: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise ValueError(f"{name} must be a non-empty string up to {max_len}")
    return value


def _matrix(
    rows: Sequence[Sequence[float]],
    *,
    name: str,
    width: int | None = None,
    allow_nonfinite: bool = False,
) -> list[list[float]]:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or not rows:
        raise ValueError(f"{name} must be a non-empty sequence")
    if len(rows) > MAX_SAMPLES:
        raise ValueError(f"{name} exceeds the {MAX_SAMPLES} sample limit")
    normalized: list[list[float]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
            raise ValueError(f"{name}[{index}] must be a sequence")
        if allow_nonfinite:
            values = []
            for item in row:
                if isinstance(item, bool) or not isinstance(item, (int, float)):
                    raise ValueError(f"{name}[{index}] must contain numbers")
                values.append(float(item))
        else:
            values = [_require_finite(f"{name}[{index}]", item) for item in row]
        if width is not None and len(values) != width:
            raise ValueError(f"{name}[{index}] must have width {width}")
        if not values:
            raise ValueError(f"{name}[{index}] must be non-empty")
        normalized.append(values)
    if width is None:
        width = len(normalized[0])
        if any(len(row) != width for row in normalized):
            raise ValueError(f"{name} rows must share one width")
    return normalized


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def compute_metrics(
    *,
    predicted: Sequence[Sequence[float]],
    actual: Sequence[Sequence[float]],
    group_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Compute held-out error metrics in physical units.

    Metrics are computed on the declared split only.  ``r2`` is reported with an
    explicit limitation because a coefficient of determination is not meaningful
    when the target variance is near zero.
    """
    # A non-finite prediction is a countable failure, not a scoring input, so it
    # is admitted here and reported through failed_prediction_count.
    forecast = _matrix(predicted, name="predicted", allow_nonfinite=True)
    observed = _matrix(actual, name="actual", width=len(forecast[0]))
    if len(forecast) != len(observed):
        raise ValueError("predicted and actual must have the same row count")

    count = len(forecast)
    dimensions = len(forecast[0])
    flattened_pairs: list[tuple[float, float]] = []
    failed = 0
    for index in range(count):
        for dimension in range(dimensions):
            guess = forecast[index][dimension]
            truth = observed[index][dimension]
            if not math.isfinite(guess) or not math.isfinite(truth):
                failed += 1
                continue
            flattened_pairs.append((guess, truth))
    if not flattened_pairs:
        raise ValueError("no finite predicted/actual pairs to score")

    absolute = [abs(guess - truth) for guess, truth in flattened_pairs]
    squared = [(guess - truth) ** 2 for guess, truth in flattened_pairs]
    mae = sum(absolute) / len(absolute)
    rmse = math.sqrt(sum(squared) / len(squared))
    max_abs = max(absolute)

    truths = [truth for _guess, truth in flattened_pairs]
    mean_truth = sum(truths) / len(truths)
    variance = sum((truth - mean_truth) ** 2 for truth in truths) / len(truths)
    ss_tot = sum((truth - mean_truth) ** 2 for truth in truths)
    ss_res = sum(squared)
    if ss_tot > 0.0:
        r2: float | None = 1.0 - ss_res / ss_tot
        r2_limitation = None
    else:
        r2 = None
        r2_limitation = "target_variance_is_zero_r2_undefined"

    spread = math.sqrt(variance)
    normalized_rmse = rmse / spread if spread > 0.0 else None

    worst_group_mae = None
    if group_ids is not None:
        if len(group_ids) != count:
            raise ValueError("group_ids must align with the scored rows")
        grouped: dict[str, list[float]] = {}
        for index, group in enumerate(group_ids):
            key = _require_str("group_id", group)
            for dimension in range(dimensions):
                grouped.setdefault(key, []).append(
                    abs(forecast[index][dimension] - observed[index][dimension])
                )
        if grouped:
            worst_group_mae = max(sum(values) / len(values) for values in grouped.values())

    return {
        "sample_count": count,
        "dimension": dimensions,
        "scored_pair_count": len(flattened_pairs),
        "failed_prediction_count": failed,
        "mae": mae,
        "rmse": rmse,
        "max_abs_error": max_abs,
        "normalized_rmse": normalized_rmse,
        "r2": r2,
        "r2_limitation": r2_limitation,
        "worst_group_mae": worst_group_mae,
        "units": "physical",
        "computed_on": "declared_split_only",
    }


def compare_against_baseline(
    *,
    surrogate_metrics: Mapping[str, Any],
    baseline_metrics: Mapping[str, Any],
    minimum_improvement_fraction: float = 0.0,
) -> dict[str, Any]:
    """Compare a surrogate with a baseline on identical splits and metrics.

    A DNN is accepted only when it provides material held-out benefit; a lower
    training loss is not evidence of anything.
    """
    if not 0.0 <= minimum_improvement_fraction < 1.0:
        raise ValueError("minimum_improvement_fraction must be in [0, 1)")
    surrogate_rmse = _require_finite("surrogate_metrics.rmse", surrogate_metrics.get("rmse"))
    baseline_rmse = _require_finite("baseline_metrics.rmse", baseline_metrics.get("rmse"))
    if baseline_rmse <= 0.0:
        raise ValueError("baseline rmse must be positive to compare")
    improvement = (baseline_rmse - surrogate_rmse) / baseline_rmse
    beats = improvement > minimum_improvement_fraction
    return {
        "surrogate_rmse": surrogate_rmse,
        "baseline_rmse": baseline_rmse,
        "improvement_fraction": improvement,
        "minimum_improvement_fraction": minimum_improvement_fraction,
        "beats_baseline": beats,
        "comparison_scope": "identical_splits_and_metrics",
        "accepted_on_training_loss": False,
    }


# --------------------------------------------------------------------------
# Non-DNN baselines
# --------------------------------------------------------------------------


def fit_baseline(
    *,
    baseline_id: str,
    kind: str,
    train_features: Sequence[Sequence[float]],
    train_targets: Sequence[Sequence[float]],
    ridge_lambda: float = DEFAULT_RIDGE_LAMBDA,
) -> dict[str, Any]:
    """Fit one deterministic non-DNN baseline on the training split only.

    ``constant_mean`` predicts the training mean, ``linear_least_squares`` fits
    ordinary least squares, and ``ridge`` adds a small Tikhonov term.  The solve
    is a plain normal-equation elimination with no external dependency.
    """
    if kind not in BASELINE_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(BASELINE_KINDS)}")
    features = _matrix(train_features, name="train_features")
    targets = _matrix(train_targets, name="train_targets")
    if len(features) != len(targets):
        raise ValueError("train_features and train_targets must align")
    if len(targets[0]) > MAX_TARGETS:
        raise ValueError(f"train_targets must have at most {MAX_TARGETS} columns")
    if kind == "ridge":
        ridge_lambda = _require_finite("ridge_lambda", ridge_lambda)
        if ridge_lambda < 0.0:
            raise ValueError("ridge_lambda must be nonnegative")

    count = len(features)
    width = len(features[0])
    output_dim = len(targets[0])

    means = [sum(row[column] for row in targets) / count for column in range(output_dim)]

    if kind == "constant_mean":
        # Represent the constant model with explicit zero coefficients so a
        # single prediction path serves every baseline kind.
        coefficients = [[0.0] * output_dim for _ in range(width)]
        intercept = means
    else:
        # Augment with a leading 1 so the intercept is fitted, not assumed.
        design = [[1.0, *row] for row in features]
        columns = width + 1
        normal = [[0.0] * columns for _ in range(columns)]
        for row in design:
            for left in range(columns):
                for right in range(columns):
                    normal[left][right] += row[left] * row[right]
        if kind == "ridge":
            for index in range(1, columns):  # never penalize the intercept
                normal[index][index] += ridge_lambda
        rhs = [[0.0] * output_dim for _ in range(columns)]
        for row_index, row in enumerate(design):
            for column in range(columns):
                for output in range(output_dim):
                    rhs[column][output] += row[column] * targets[row_index][output]
        solution = _solve_normal_equations(normal, rhs)
        intercept = [solution[0][output] for output in range(output_dim)]
        coefficients = [
            [solution[column + 1][output] for output in range(output_dim)]
            for column in range(width)
        ]

    body = {
        "schema": "comsol_mcp.surrogate_baseline",
        "schema_version": SCHEMA_VERSION,
        "baseline_id": _require_str("baseline_id", baseline_id),
        "kind": kind,
        "fit_row_count": count,
        "input_dim": width,
        "output_dim": output_dim,
        "intercept": intercept,
        "coefficients": coefficients,
        "ridge_lambda": ridge_lambda if kind == "ridge" else None,
        "fitted_on": "train_split_only",
        "is_dnn": False,
    }
    return {**body, "baseline_sha256": canonical_sha256_v1(body)}


def _solve_normal_equations(matrix: list[list[float]], rhs: list[list[float]]) -> list[list[float]]:
    """Solve ``matrix * x = rhs`` by Gauss-Jordan elimination with partial pivoting."""
    size = len(matrix)
    columns = len(rhs[0])
    augmented = [[*matrix[row], *rhs[row]] for row in range(size)]
    for pivot in range(size):
        best = max(range(pivot, size), key=lambda row: abs(augmented[row][pivot]))
        if abs(augmented[best][pivot]) < 1e-14:
            raise ValueError("baseline normal equations are singular")
        augmented[pivot], augmented[best] = augmented[best], augmented[pivot]
        divisor = augmented[pivot][pivot]
        augmented[pivot] = [value / divisor for value in augmented[pivot]]
        for row in range(size):
            if row == pivot:
                continue
            factor = augmented[row][pivot]
            if factor == 0.0:
                continue
            augmented[row] = [
                augmented[row][column] - factor * augmented[pivot][column]
                for column in range(size + columns)
            ]
    return [row[size:] for row in augmented]


def predict_baseline(
    baseline: Mapping[str, Any], features: Sequence[Sequence[float]]
) -> list[list[float]]:
    """Evaluate a fitted baseline on new rows."""
    if not isinstance(baseline, Mapping):
        raise ValueError("baseline must be a mapping")
    body = {key: value for key, value in baseline.items() if key != "baseline_sha256"}
    if baseline.get("baseline_sha256") != canonical_sha256_v1(body):
        raise ValueError("baseline_sha256 mismatch")
    if baseline.get("is_dnn") is not False:
        raise ValueError("baseline must not be a DNN")
    rows = _matrix(features, name="features", width=int(baseline["input_dim"]))
    intercept = [float(value) for value in baseline["intercept"]]
    coefficients = [[float(value) for value in column] for column in baseline["coefficients"]]
    predictions: list[list[float]] = []
    for row in rows:
        output = list(intercept)
        for column, value in enumerate(row):
            for target in range(len(intercept)):
                output[target] += coefficients[column][target] * value
        predictions.append(output)
    return predictions


# --------------------------------------------------------------------------
# Continuation eligibility
# --------------------------------------------------------------------------


def build_continuation_identity(
    *,
    dataset_manifest_sha256: str,
    split_manifest_sha256: str,
    field_schema_sha256: str,
    transforms_sha256: str,
    architecture_sha256: str,
    comsol_build: str,
    objective: str,
    prior_checkpoint_sha256: str,
) -> dict[str, Any]:
    """Bind the exact identities a continuation must reproduce."""
    body = {
        "schema": "comsol_mcp.surrogate_continuation_identity",
        "schema_version": SCHEMA_VERSION,
        "dataset_manifest_sha256": _require_hex64(
            "dataset_manifest_sha256", dataset_manifest_sha256
        ),
        "split_manifest_sha256": _require_hex64("split_manifest_sha256", split_manifest_sha256),
        "field_schema_sha256": _require_hex64("field_schema_sha256", field_schema_sha256),
        "transforms_sha256": _require_hex64("transforms_sha256", transforms_sha256),
        "architecture_sha256": _require_hex64("architecture_sha256", architecture_sha256),
        "comsol_build": _require_str("comsol_build", comsol_build, max_len=64),
        "objective": _require_str("objective", objective),
        "prior_checkpoint_sha256": _require_hex64(
            "prior_checkpoint_sha256", prior_checkpoint_sha256
        ),
    }
    return {**body, "identity_sha256": canonical_sha256_v1(body)}


def evaluate_continuation(
    *,
    prior: Mapping[str, Any],
    current: Mapping[str, Any],
    trained_chksum: str | None = None,
) -> dict[str, Any]:
    """Decide whether a continuation may resume the same lineage.

    A changed dataset, split, transform, architecture, COMSOL build, objective,
    or prior checkpoint creates a new lineage and is reported as a nonidentical
    continuation rather than being silently resumed.
    """
    mismatched: list[str] = []
    for field in CONTINUATION_IDENTITY_FIELDS:
        if prior.get(field) != current.get(field):
            mismatched.append(field)
    if mismatched:
        return {
            "continuation_allowed": False,
            "reason_code": "nonidentical_continuation",
            "mismatched_fields": mismatched,
            "creates_new_lineage": True,
            "overwrites_accepted_model": False,
            "trained_chksum": trained_chksum,
        }
    return {
        "continuation_allowed": True,
        "reason_code": "identical_continuation",
        "mismatched_fields": [],
        "creates_new_lineage": False,
        "overwrites_accepted_model": False,
        "trained_chksum": trained_chksum,
    }


def assert_continuation_allowed(**kwargs: Any) -> dict[str, Any]:
    """Fail closed when a continuation would cross a contract identity."""
    report = evaluate_continuation(**kwargs)
    if not report["continuation_allowed"]:
        raise ValueError("nonidentical_continuation: " + ", ".join(report["mismatched_fields"]))
    return report


def evaluate_seed_stability(
    per_seed_metrics: Sequence[Mapping[str, Any]],
    *,
    maximum_spread_fraction: float,
    metric: str = "rmse",
) -> dict[str, Any]:
    """Report multi-seed stability without hiding an unfavourable seed.

    Every declared seed must be supplied; selecting only the best seed is
    refused because the caller-visible result would overstate stability.
    """
    if not per_seed_metrics:
        raise ValueError("per_seed_metrics must be non-empty")
    if not 0.0 <= maximum_spread_fraction:
        raise ValueError("maximum_spread_fraction must be non-negative")
    values: list[float] = []
    for index, item in enumerate(per_seed_metrics):
        if not isinstance(item, Mapping):
            raise ValueError(f"per_seed_metrics[{index}] must be a mapping")
        values.append(_require_finite(f"per_seed_metrics[{index}].{metric}", item.get(metric)))
    worst = max(values)
    best = min(values)
    median = sorted(values)[len(values) // 2]
    spread = (worst - best) / median if median > 0.0 else None
    stable = spread is not None and spread <= maximum_spread_fraction
    return {
        "metric": metric,
        "seed_count": len(values),
        "best": best,
        "median": median,
        "worst": worst,
        "spread_fraction": spread,
        "maximum_spread_fraction": maximum_spread_fraction,
        "stable": stable,
        "all_seeds_reported": True,
        "selected_only_best_seed": False,
    }


__all__ = [
    "BASELINE_KINDS",
    "CONTINUATION_IDENTITY_FIELDS",
    "DEFAULT_RIDGE_LAMBDA",
    "METRIC_NAMES",
    "SCHEMA_VERSION",
    "assert_continuation_allowed",
    "build_continuation_identity",
    "compare_against_baseline",
    "compute_metrics",
    "evaluate_continuation",
    "evaluate_seed_stability",
    "fit_baseline",
    "predict_baseline",
]
