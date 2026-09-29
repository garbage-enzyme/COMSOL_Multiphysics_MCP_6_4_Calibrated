"""Solver-free surrogate metrics, baseline, and continuation tests."""

from __future__ import annotations

import math

import pytest

from comsol_mcp.surrogate.training import (
    BASELINE_KINDS,
    assert_continuation_allowed,
    build_continuation_identity,
    compare_against_baseline,
    compute_metrics,
    evaluate_continuation,
    evaluate_seed_stability,
    fit_baseline,
    predict_baseline,
)

SOLVER_FREE_BANNED = (
    "import mph",
    "from mph",
    "import jpype",
    "from jpype",
    "import comsol",
    "from comsol.",
    "onnx",
    "torch",
    "tensorflow",
    "sklearn",
    "numpy",
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def test_metrics_are_computed_in_physical_units() -> None:
    metrics = compute_metrics(
        predicted=[[1.0], [2.0], [3.0]],
        actual=[[1.5], [2.5], [3.5]],
    )
    assert metrics["mae"] == pytest.approx(0.5)
    assert metrics["rmse"] == pytest.approx(0.5)
    assert metrics["max_abs_error"] == pytest.approx(0.5)
    assert metrics["units"] == "physical"
    assert metrics["computed_on"] == "declared_split_only"
    assert metrics["failed_prediction_count"] == 0


def test_perfect_prediction_scores_zero_and_r2_one() -> None:
    metrics = compute_metrics(
        predicted=[[1.0], [2.0], [3.0], [4.0]],
        actual=[[1.0], [2.0], [3.0], [4.0]],
    )
    assert metrics["rmse"] == 0.0
    assert metrics["r2"] == pytest.approx(1.0)
    assert metrics["normalized_rmse"] == 0.0


def test_r2_is_undefined_for_zero_variance_targets() -> None:
    metrics = compute_metrics(
        predicted=[[1.1], [2.1]], actual=[[1.0], [1.0]]
    )
    assert metrics["r2"] is None
    assert metrics["r2_limitation"] == "target_variance_is_zero_r2_undefined"
    assert metrics["normalized_rmse"] is None


def test_nonfinite_predictions_are_counted_not_scored() -> None:
    metrics = compute_metrics(
        predicted=[[1.0], [float("nan")]],
        actual=[[1.0], [2.0]],
    )
    assert metrics["failed_prediction_count"] == 1
    assert metrics["scored_pair_count"] == 1


def test_worst_group_mae_exposes_a_failed_regime() -> None:
    metrics = compute_metrics(
        predicted=[[1.0], [1.0], [9.0]],
        actual=[[1.0], [1.0], [5.0]],
        group_ids=["good", "good", "bad"],
    )
    # The aggregate MAE hides the bad regime; the group metric does not.
    assert metrics["mae"] == pytest.approx(4.0 / 3.0)
    assert metrics["worst_group_mae"] == pytest.approx(4.0)


def test_metrics_reject_misaligned_and_bad_inputs() -> None:
    with pytest.raises(ValueError, match="same row count"):
        compute_metrics(predicted=[[1.0]], actual=[[1.0], [2.0]])
    with pytest.raises(ValueError, match="finite"):
        compute_metrics(predicted=[[float("inf")]], actual=[[1.0]])
    with pytest.raises(ValueError, match="align"):
        compute_metrics(predicted=[[1.0]], actual=[[1.0]], group_ids=["a", "b"])


# --------------------------------------------------------------------------
# Baseline comparison
# --------------------------------------------------------------------------


def test_surrogate_must_beat_the_baseline_materially() -> None:
    surrogate = {"rmse": 0.5}
    baseline = {"rmse": 1.0}
    report = compare_against_baseline(
        surrogate_metrics=surrogate, baseline_metrics=baseline
    )
    assert report["beats_baseline"] is True
    assert report["improvement_fraction"] == pytest.approx(0.5)
    assert report["accepted_on_training_loss"] is False


def test_any_improvement_beats_a_zero_threshold() -> None:
    """With the default zero threshold, a strict improvement counts as beating."""
    report = compare_against_baseline(
        surrogate_metrics={"rmse": 0.9}, baseline_metrics={"rmse": 1.0}
    )
    assert report["beats_baseline"] is True
    assert report["improvement_fraction"] == pytest.approx(0.1)


def test_surrogate_worse_than_baseline_is_reported_truthfully() -> None:
    report = compare_against_baseline(
        surrogate_metrics={"rmse": 1.2}, baseline_metrics={"rmse": 1.0}
    )
    assert report["beats_baseline"] is False
    assert report["improvement_fraction"] == pytest.approx(-0.2)
    assert report["accepted_on_training_loss"] is False


def test_equal_rmse_does_not_beat_the_baseline() -> None:
    report = compare_against_baseline(
        surrogate_metrics={"rmse": 1.0}, baseline_metrics={"rmse": 1.0}
    )
    assert report["beats_baseline"] is False
    assert report["improvement_fraction"] == 0.0


def test_minimum_improvement_threshold_is_enforced() -> None:
    report = compare_against_baseline(
        surrogate_metrics={"rmse": 0.95},
        baseline_metrics={"rmse": 1.0},
        minimum_improvement_fraction=0.1,
    )
    assert report["beats_baseline"] is False
    with pytest.raises(ValueError, match="minimum_improvement_fraction"):
        compare_against_baseline(
            surrogate_metrics={"rmse": 0.5},
            baseline_metrics={"rmse": 1.0},
            minimum_improvement_fraction=1.0,
        )


# --------------------------------------------------------------------------
# Baselines
# --------------------------------------------------------------------------


def test_constant_mean_baseline_predicts_the_training_mean() -> None:
    baseline = fit_baseline(
        baseline_id="b1",
        kind="constant_mean",
        train_features=[[0.0], [1.0], [2.0]],
        train_targets=[[2.0], [4.0], [6.0]],
    )
    assert baseline["intercept"] == pytest.approx([4.0])
    assert baseline["is_dnn"] is False
    assert baseline["fitted_on"] == "train_split_only"
    predictions = predict_baseline(baseline, [[10.0], [20.0]])
    assert predictions == [[4.0], [4.0]]


def test_linear_least_squares_recovers_an_exact_affine_law() -> None:
    baseline = fit_baseline(
        baseline_id="b2",
        kind="linear_least_squares",
        train_features=[[0.0], [1.0], [2.0], [3.0]],
        train_targets=[[1.0], [3.0], [5.0], [7.0]],  # y = 1 + 2x
    )
    predictions = predict_baseline(baseline, [[10.0]])
    assert predictions[0][0] == pytest.approx(21.0, abs=1e-9)


def test_ridge_recovers_a_multivariate_law() -> None:
    baseline = fit_baseline(
        baseline_id="b3",
        kind="ridge",
        train_features=[[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 1.0], [2.0, 1.0]],
        train_targets=[[1.0], [4.0], [2.0], [5.0], [8.0]],  # y = 1 + 3a + 1b
        ridge_lambda=1e-12,
    )
    predictions = predict_baseline(baseline, [[3.0, 2.0]])
    assert predictions[0][0] == pytest.approx(12.0, abs=1e-6)


def test_baseline_supports_multiple_outputs() -> None:
    baseline = fit_baseline(
        baseline_id="b4",
        kind="linear_least_squares",
        train_features=[[0.0], [1.0], [2.0]],
        train_targets=[[1.0, 10.0], [2.0, 20.0], [3.0, 30.0]],
    )
    predictions = predict_baseline(baseline, [[3.0]])
    assert predictions[0][0] == pytest.approx(4.0, abs=1e-9)
    assert predictions[0][1] == pytest.approx(40.0, abs=1e-9)


def test_baseline_rejects_bad_kind_and_tampering() -> None:
    with pytest.raises(ValueError, match="kind must be one of"):
        fit_baseline(
            baseline_id="b5",
            kind="neural_network",
            train_features=[[0.0]],
            train_targets=[[1.0]],
        )
    baseline = fit_baseline(
        baseline_id="b6",
        kind="constant_mean",
        train_features=[[0.0], [1.0]],
        train_targets=[[1.0], [3.0]],
    )
    tampered = dict(baseline)
    tampered["intercept"] = [99.0]
    with pytest.raises(ValueError, match="baseline_sha256 mismatch"):
        predict_baseline(tampered, [[0.0]])


def test_all_declared_baseline_kinds_are_fitted_on_train_only() -> None:
    for kind in BASELINE_KINDS:
        baseline = fit_baseline(
            baseline_id=f"b-{kind}",
            kind=kind,
            train_features=[[0.0], [1.0], [2.0]],
            train_targets=[[1.0], [3.0], [5.0]],
        )
        assert baseline["fitted_on"] == "train_split_only"
        assert baseline["is_dnn"] is False


# --------------------------------------------------------------------------
# Continuation
# --------------------------------------------------------------------------


def _identity(**overrides) -> dict:
    kwargs = {
        "dataset_manifest_sha256": SHA_A,
        "split_manifest_sha256": SHA_B,
        "field_schema_sha256": SHA_C,
        "transforms_sha256": SHA_D,
        "architecture_sha256": SHA_E,
        "comsol_build": "6.4.0.293",
        "objective": "maximize_R_at_1550nm",
        "prior_checkpoint_sha256": SHA_F,
    }
    kwargs.update(overrides)
    return build_continuation_identity(**kwargs)


def test_identical_identities_allow_continuation() -> None:
    report = assert_continuation_allowed(prior=_identity(), current=_identity())
    assert report["continuation_allowed"] is True
    assert report["creates_new_lineage"] is False
    assert report["overwrites_accepted_model"] is False


def test_changed_identity_creates_a_new_lineage() -> None:
    changed = _identity(comsol_build="6.4.0.300")
    report = evaluate_continuation(prior=_identity(), current=changed)
    assert report["continuation_allowed"] is False
    assert report["reason_code"] == "nonidentical_continuation"
    assert report["mismatched_fields"] == ["comsol_build"]
    assert report["creates_new_lineage"] is True
    with pytest.raises(ValueError, match="nonidentical_continuation"):
        assert_continuation_allowed(prior=_identity(), current=changed)


def test_every_contract_identity_blocks_continuation_independently() -> None:
    from comsol_mcp.surrogate.training import CONTINUATION_IDENTITY_FIELDS

    for field in CONTINUATION_IDENTITY_FIELDS:
        replacement = "9" * 64 if field.endswith("_sha256") else "changed"
        report = evaluate_continuation(
            prior=_identity(), current=_identity(**{field: replacement})
        )
        assert report["continuation_allowed"] is False, field
        assert field in report["mismatched_fields"], field


def test_continuation_identity_is_sealed() -> None:
    identity = _identity()
    assert len(identity["identity_sha256"]) == 64
    assert _identity()["identity_sha256"] == identity["identity_sha256"]


def test_continuation_identity_rejects_bad_digests() -> None:
    with pytest.raises(ValueError, match="hex digest"):
        _identity(dataset_manifest_sha256="nope")
    with pytest.raises(ValueError, match="comsol_build"):
        _identity(comsol_build="")


# --------------------------------------------------------------------------
# Seed stability
# --------------------------------------------------------------------------


def test_seed_stability_reports_every_seed() -> None:
    report = evaluate_seed_stability(
        [{"rmse": 0.10}, {"rmse": 0.12}, {"rmse": 0.11}],
        maximum_spread_fraction=0.5,
    )
    assert report["seed_count"] == 3
    assert report["best"] == pytest.approx(0.10)
    assert report["worst"] == pytest.approx(0.12)
    assert report["all_seeds_reported"] is True
    assert report["selected_only_best_seed"] is False
    assert report["stable"] is True


def test_unstable_seeds_are_reported_as_unstable() -> None:
    report = evaluate_seed_stability(
        [{"rmse": 0.10}, {"rmse": 0.90}], maximum_spread_fraction=0.2
    )
    assert report["stable"] is False
    assert report["spread_fraction"] is not None
    assert report["spread_fraction"] > 0.2


def test_seed_stability_rejects_empty_and_missing_metric() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        evaluate_seed_stability([], maximum_spread_fraction=0.2)
    with pytest.raises(ValueError, match="rmse"):
        evaluate_seed_stability([{"mae": 0.1}], maximum_spread_fraction=0.2)


# --------------------------------------------------------------------------
# Solver-free guard
# --------------------------------------------------------------------------


def test_training_module_is_solver_free() -> None:
    import comsol_mcp.surrogate.training as module

    source = open(module.__file__, encoding="utf-8").read().lower()
    for banned in SOLVER_FREE_BANNED:
        assert banned not in source, f"training references {banned}"


def test_no_external_numeric_dependency_is_imported() -> None:
    """Baselines must fit with the standard library only."""
    import comsol_mcp.surrogate.training as module

    source = open(module.__file__, encoding="utf-8").read()
    assert "import numpy" not in source
    assert math.isfinite(1.0)
