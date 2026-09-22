"""Solver-free field-schema, transform, split, and adversarial-leakage tests."""

from __future__ import annotations

import pytest

from comsol_mcp.surrogate.fields import (
    apply_transforms,
    build_field_schema,
    fit_transforms,
    invert_transforms,
    validate_field_schema,
    validate_fitted_transforms,
    verify_transform_roundtrip,
)
from comsol_mcp.surrogate.splits import (
    assert_no_leakage,
    assign_group_disjoint_split,
    detect_leakage,
    validate_split_plan,
)

SOLVER_FREE_BANNED = (
    "import mph",
    "from mph",
    "import jpype",
    "from jpype",
    "import java",
    "from java",
    "import comsol",
    "from comsol.",
    "onnx",
    "torch",
    "tensorflow",
)


def _features() -> list[dict]:
    return [
        {"field_id": "w", "unit": "nm", "lower": 100.0, "upper": 900.0},
        {"field_id": "h", "unit": "nm", "lower": 50.0, "upper": 400.0},
    ]


def _targets() -> list[dict]:
    return [
        {
            "field_id": "R",
            "unit": "1",
            "lower": 0.0,
            "upper": 1.0,
            "support": {"coordinates": [1550.0], "unit": "nm"},
        }
    ]


def _schema() -> dict:
    return build_field_schema(
        schema_id="waveopt-scalar-v1", features=_features(), targets=_targets()
    )


# --------------------------------------------------------------------------
# Field schema
# --------------------------------------------------------------------------


def test_field_schema_freezes_order_units_and_support() -> None:
    schema = _schema()
    assert schema["feature_order"] == ["w", "h"]
    assert schema["target_order"] == ["R"]
    assert schema["features"][0]["unit"] == "nm"
    assert schema["targets"][0]["support"]["coordinates"] == [1550.0]
    assert schema["input_dim"] == 2 and schema["target_dim"] == 1
    validate_field_schema(schema)


def test_field_schema_rejects_unknown_fields_and_bad_bounds() -> None:
    with pytest.raises(ValueError, match="unknown fields"):
        build_field_schema(
            schema_id="bad",
            features=[{**_features()[0], "extra": 1}, _features()[1]],
            targets=_targets(),
        )
    with pytest.raises(ValueError, match="lower < upper"):
        build_field_schema(
            schema_id="bad",
            features=[{**_features()[0], "lower": 900.0}, _features()[1]],
            targets=_targets(),
        )


def test_field_schema_requires_target_support_and_refuses_clipping() -> None:
    with pytest.raises(ValueError, match="support must be a mapping"):
        build_field_schema(
            schema_id="bad",
            features=_features(),
            targets=[{"field_id": "R", "unit": "1", "lower": 0.0, "upper": 1.0}],
        )
    with pytest.raises(ValueError, match="clip_policy must be refuse"):
        build_field_schema(
            schema_id="bad",
            features=_features(),
            targets=[{**_targets()[0], "clip_policy": "clip"}],
        )


def test_field_schema_rejects_nonfinite_and_overlapping_ids() -> None:
    with pytest.raises(ValueError, match="finite"):
        build_field_schema(
            schema_id="bad",
            features=[{**_features()[0], "lower": float("nan")}, _features()[1]],
            targets=_targets(),
        )
    with pytest.raises(ValueError, match="must not overlap"):
        build_field_schema(
            schema_id="bad",
            features=_features(),
            targets=[{**_targets()[0], "field_id": "w"}],
        )


def test_field_schema_is_tamper_evident() -> None:
    schema = _schema()
    tampered = dict(schema)
    tampered["feature_order"] = ["h", "w"]
    with pytest.raises(ValueError):
        validate_field_schema(tampered)


# --------------------------------------------------------------------------
# Transforms
# --------------------------------------------------------------------------


def test_transforms_fit_only_on_train_and_roundtrip() -> None:
    schema = _schema()
    train = [
        {"w": 100.0, "h": 50.0, "R": 0.1},
        {"w": 300.0, "h": 150.0, "R": 0.4},
        {"w": 900.0, "h": 400.0, "R": 0.9},
    ]
    transforms = fit_transforms(
        transform_id="std-v1",
        schema=schema,
        train_rows=train,
        kinds={"w": "standardize", "R": "minmax"},
    )
    assert transforms["fit_split"] == "train"
    assert transforms["fit_row_count"] == 3
    validate_fitted_transforms(transforms)
    report = verify_transform_roundtrip(transforms, train)
    assert report["roundtrip_verified"] is True


def test_transforms_ignore_non_train_values_by_construction() -> None:
    """Fitted parameters must depend only on the declared training rows."""
    from comsol_mcp.durable.canonical import canonical_sha256_v1

    schema = _schema()
    train = [
        {"w": 100.0, "h": 50.0, "R": 0.1},
        {"w": 300.0, "h": 150.0, "R": 0.4},
    ]
    baseline = fit_transforms(transform_id="t", schema=schema, train_rows=train)
    # A wildly different validation row cannot enter the fit call at all.
    assert baseline["fit_row_count"] == 2

    # The seal is verified before the semantic rule, so an unsealed edit is
    # rejected as tampering rather than as a policy violation.
    unsealed = {**baseline, "fit_split": "validation"}
    with pytest.raises(ValueError, match="manifest_sha256 mismatch"):
        validate_fitted_transforms(unsealed)

    # A correctly re-sealed manifest still cannot declare a non-train fit split.
    body = {k: v for k, v in baseline.items() if k != "manifest_sha256"}
    body["fit_split"] = "validation"
    with pytest.raises(ValueError, match="fit only on train"):
        validate_fitted_transforms({**body, "manifest_sha256": canonical_sha256_v1(body)})


def test_standardize_with_zero_scale_is_refused() -> None:
    schema = _schema()
    with pytest.raises(ValueError, match="zero scale"):
        fit_transforms(
            transform_id="t",
            schema=schema,
            train_rows=[{"w": 5.0, "h": 1.0, "R": 0.1}, {"w": 5.0, "h": 2.0, "R": 0.2}],
            kinds={"w": "standardize"},
        )


def test_inverse_transform_recovers_physical_values() -> None:
    schema = _schema()
    train = [
        {"w": 100.0, "h": 50.0, "R": 0.1},
        {"w": 300.0, "h": 150.0, "R": 0.4},
        {"w": 900.0, "h": 400.0, "R": 0.9},
    ]
    transforms = fit_transforms(
        transform_id="std-v1",
        schema=schema,
        train_rows=train,
        kinds={"w": "standardize", "h": "minmax", "R": "standardize"},
    )
    row = {"w": 500.0, "h": 200.0, "R": 0.55}
    restored = invert_transforms(transforms, apply_transforms(transforms, row))
    for key, value in row.items():
        assert restored[key] == pytest.approx(value, rel=1e-12, abs=1e-12)


# --------------------------------------------------------------------------
# Deterministic splits
# --------------------------------------------------------------------------


def test_split_is_deterministic_and_disjoint() -> None:
    groups = [f"g{index}" for index in range(20)]
    first = assign_group_disjoint_split(group_ids=groups, seed=17)
    second = assign_group_disjoint_split(group_ids=list(reversed(groups)), seed=17)
    assert first["manifest_sha256"] == second["manifest_sha256"]
    assigned = [item["group_id"] for item in first["assignments"]]
    assert sorted(assigned) == sorted(groups)
    assert len(assigned) == len(set(assigned))
    validate_split_plan(first)


def test_split_changes_with_seed_and_holdout_is_excluded_from_fit() -> None:
    groups = [f"g{index}" for index in range(20)]
    seed17 = assign_group_disjoint_split(group_ids=groups, seed=17)
    seed29 = assign_group_disjoint_split(group_ids=groups, seed=29)
    assert seed17["manifest_sha256"] != seed29["manifest_sha256"]

    plan = assign_group_disjoint_split(
        group_ids=groups, seed=17, holdout_group_ids=["g0", "g1"]
    )
    holdout = {
        item["group_id"] for item in plan["assignments"] if item["split"] == "scientific_holdout"
    }
    assert holdout == {"g0", "g1"}
    fitted = {
        item["group_id"]
        for item in plan["assignments"]
        if item["split"] in {"train", "validation", "test"}
    }
    assert not (holdout & fitted)


def test_split_plan_rejects_tampering_and_bad_inputs() -> None:
    groups = [f"g{index}" for index in range(10)]
    plan = assign_group_disjoint_split(group_ids=groups, seed=17)
    tampered = dict(plan)
    tampered["seed"] = 18
    with pytest.raises(ValueError):
        validate_split_plan(tampered)
    with pytest.raises(ValueError, match="sum to 100"):
        assign_group_disjoint_split(
            group_ids=groups, seed=17, proportions={"train": 70, "validation": 15, "test": 10}
        )
    with pytest.raises(ValueError, match="unique"):
        assign_group_disjoint_split(group_ids=["g", "g"], seed=17)
    with pytest.raises(ValueError, match="unknown groups"):
        assign_group_disjoint_split(group_ids=groups, seed=17, holdout_group_ids=["nope"])


# --------------------------------------------------------------------------
# Adversarial leakage
# --------------------------------------------------------------------------


def test_leakage_detects_duplicate_candidate_across_splits() -> None:
    with pytest.raises(ValueError, match="duplicate_candidate"):
        assert_no_leakage(
            assignments={"g1": "train", "g2": "test"},
            group_members={"g1": ["r1"], "g2": ["r2"]},
            candidate_ids={"r1": "canon-A", "r2": "canon-A"},
        )


def test_leakage_detects_holdout_reachable_from_fitted_split() -> None:
    report = detect_leakage(
        assignments={"g1": "train", "g2": "scientific_holdout"},
        group_members={"g1": ["shared"], "g2": ["shared"]},
    )
    assert report["leakage_detected"] is True
    assert "holdout_in_fit" in {item["leakage_class"] for item in report["findings"]}


def test_leakage_detects_row_spanning_groups_in_different_splits() -> None:
    report = detect_leakage(
        assignments={"g1": "train", "g2": "validation"},
        group_members={"g1": ["r1", "r9"], "g2": ["r9"]},
    )
    assert report["leakage_detected"] is True
    assert "cross_split_group" in {item["leakage_class"] for item in report["findings"]}


def test_clean_group_disjoint_split_reports_no_leakage() -> None:
    plan = assign_group_disjoint_split(
        group_ids=[f"g{i}" for i in range(12)], seed=17, holdout_group_ids=["g0"]
    )
    assignments = {item["group_id"]: item["split"] for item in plan["assignments"]}
    members = {group: [f"row-{group}"] for group in assignments}
    report = assert_no_leakage(assignments=assignments, group_members=members)
    assert report["leakage_detected"] is False
    assert report["finding_count"] == 0


def test_leakage_rejects_unknown_groups_and_splits() -> None:
    with pytest.raises(ValueError, match="unassigned groups"):
        detect_leakage(assignments={"g1": "train"}, group_members={"g1": ["r"], "g2": ["r"]})
    with pytest.raises(ValueError, match="unknown split"):
        detect_leakage(assignments={"g1": "holdout"}, group_members={"g1": ["r"]})


# --------------------------------------------------------------------------
# Solver-free guard
# --------------------------------------------------------------------------


def test_surrogate_modules_are_solver_free() -> None:
    import comsol_mcp.surrogate.fields as fields_mod
    import comsol_mcp.surrogate.manifests as manifests_mod
    import comsol_mcp.surrogate.splits as splits_mod

    for module in (fields_mod, manifests_mod, splits_mod):
        source = open(module.__file__, encoding="utf-8").read().lower()
        for banned in SOLVER_FREE_BANNED:
            assert banned not in source, f"{module.__name__} references {banned}"
