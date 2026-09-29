"""Solver-free surrogate row provenance and deterministic DOE design tests."""

from __future__ import annotations

import pytest

from comsol_mcp.durable.canonical import canonical_sha256_v1
from comsol_mcp.surrogate.rows import (
    assert_design_within_bounds,
    build_lhs_design,
    build_row_provenance,
    summarize_row_ledger,
    validate_lhs_design,
    validate_row_provenance,
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
)

SHA_A = "a" * 64
SHA_B = "b" * 64


def _row(**overrides) -> dict:
    kwargs = {
        "row_id": "row-1",
        "candidate_id": "cand-1",
        "source_model_sha256": SHA_A,
        "solver_identity_sha256": SHA_B,
        "study_identity": "std1",
        "solution_identity": "sol1",
        "dataset_identity": "dset1",
        "fidelity": "wave_optics_point",
        "evidence_state": "verified",
        "features": {"w": 100.0, "h": 50.0},
        "targets": {"R": 0.31},
        "leakage_group_id": "g1",
    }
    kwargs.update(overrides)
    return build_row_provenance(**kwargs)


# --------------------------------------------------------------------------
# Row provenance
# --------------------------------------------------------------------------


def test_row_binds_every_identity_and_is_sealed() -> None:
    row = _row()
    assert row["eligible"] is True
    assert row["evidence_state"] == "verified"
    assert row["is_surrogate_prediction"] is False
    assert row["upgrades_fem_evidence"] is False
    assert row["source_model_sha256"] == SHA_A
    assert row["solver_identity_sha256"] == SHA_B
    validate_row_provenance(row)


def test_row_is_tamper_evident() -> None:
    row = _row()
    tampered = dict(row)
    tampered["targets"] = {"R": 0.99}
    with pytest.raises(ValueError):
        validate_row_provenance(tampered)


def test_row_rejects_bad_identities_and_nonfinite_values() -> None:
    with pytest.raises(ValueError, match="hex digest"):
        _row(source_model_sha256="short")
    with pytest.raises(ValueError, match="finite"):
        _row(targets={"R": float("nan")})
    with pytest.raises(ValueError, match="non-empty mapping"):
        _row(features={})
    with pytest.raises(ValueError, match="evidence_state"):
        _row(evidence_state="fabricated")


def test_nonverified_row_requires_an_explicit_reason() -> None:
    with pytest.raises(ValueError, match="explicit ineligible_reason"):
        _row(evidence_state="label_only")
    row = _row(evidence_state="label_only", ineligible_reason="evidence_incomplete")
    assert row["eligible"] is False
    assert row["ineligible_reason"] == "evidence_incomplete"


def test_ineligible_reason_must_be_known() -> None:
    with pytest.raises(ValueError, match="ineligible_reason must be one of"):
        _row(ineligible_reason="because_i_said_so")


def test_eligible_row_cannot_carry_an_ineligible_reason() -> None:
    row = _row(ineligible_reason="fem_failed")
    assert row["eligible"] is False
    # A re-sealed row claiming eligibility alongside a failure reason is refused.
    body = {k: v for k, v in row.items() if k != "row_sha256"}
    body["eligible"] = True
    resealed = {**body, "row_sha256": canonical_sha256_v1(body)}
    with pytest.raises(ValueError, match="must not carry an ineligible_reason"):
        validate_row_provenance(resealed)


def test_only_verified_rows_may_be_eligible() -> None:
    row = _row(evidence_state="unknown", ineligible_reason="evidence_incomplete")
    body = {k: v for k, v in row.items() if k != "row_sha256"}
    body["eligible"] = True
    body["ineligible_reason"] = None
    resealed = {**body, "row_sha256": canonical_sha256_v1(body)}
    with pytest.raises(ValueError, match="only a verified row may be eligible"):
        validate_row_provenance(resealed)


def test_ledger_retains_failed_rows_with_reasons() -> None:
    rows = [
        _row(row_id="row-1"),
        _row(row_id="row-2", ineligible_reason="fem_failed"),
        _row(row_id="row-3", ineligible_reason="fem_failed"),
        _row(
            row_id="row-4",
            evidence_state="label_only",
            ineligible_reason="evidence_incomplete",
        ),
    ]
    summary = summarize_row_ledger(rows)
    assert summary["row_count"] == 4
    assert summary["eligible_count"] == 1
    assert summary["ineligible_count"] == 3
    assert summary["ineligible_reasons"] == {"evidence_incomplete": 1, "fem_failed": 2}
    assert summary["failed_rows_retained"] is True


def test_ledger_rejects_duplicate_row_ids() -> None:
    with pytest.raises(ValueError, match="unique"):
        summarize_row_ledger([_row(), _row()])


def test_ledger_hash_is_deterministic_and_order_sensitive() -> None:
    first = _row(row_id="row-1")
    second = _row(row_id="row-2")
    forward = summarize_row_ledger([first, second])["ledger_sha256"]
    reverse = summarize_row_ledger([second, first])["ledger_sha256"]
    assert forward == summarize_row_ledger([first, second])["ledger_sha256"]
    assert forward != reverse


# --------------------------------------------------------------------------
# Deterministic DOE design
# --------------------------------------------------------------------------


def _bounds() -> dict:
    return {"w": [100.0, 900.0], "h": [50.0, 400.0]}


def test_lhs_design_is_deterministic_and_in_bounds() -> None:
    first = build_lhs_design(design_id="d1", bounds=_bounds(), row_count=12, seed=17)
    second = build_lhs_design(design_id="d1", bounds=_bounds(), row_count=12, seed=17)
    assert first["design_sha256"] == second["design_sha256"]
    assert first["row_count"] == 12
    assert first["dimension"] == 2
    assert first["variable_order"] == ["h", "w"]
    validate_lhs_design(first)
    report = assert_design_within_bounds(first)
    assert report["within_bounds"] is True
    assert report["points_checked"] == 12
    assert report["coordinates_checked"] == 24


def test_lhs_design_changes_with_seed_and_is_stratified() -> None:
    seed17 = build_lhs_design(design_id="d1", bounds=_bounds(), row_count=8, seed=17)
    seed29 = build_lhs_design(design_id="d1", bounds=_bounds(), row_count=8, seed=29)
    assert seed17["design_sha256"] != seed29["design_sha256"]

    # Latin-hypercube stratification: each variable occupies every 1/N stratum
    # exactly once, so the design covers the box rather than clustering.
    for name in seed17["variable_order"]:
        low, high = seed17["bounds"][name]
        width = (high - low) / 8
        strata = sorted(
            min(7, int((point[name] - low) / width)) for point in seed17["points"]
        )
        assert strata == list(range(8)), f"{name} is not stratified: {strata}"


def test_lhs_design_rejects_bad_bounds_and_counts() -> None:
    with pytest.raises(ValueError, match="lower < upper"):
        build_lhs_design(
            design_id="d1", bounds={"w": [900.0, 100.0], "h": [50.0, 400.0]}, row_count=4, seed=1
        )
    with pytest.raises(ValueError, match="2-12 variables"):
        build_lhs_design(design_id="d1", bounds={"w": [0.0, 1.0]}, row_count=4, seed=1)
    with pytest.raises(ValueError, match="row_count"):
        build_lhs_design(design_id="d1", bounds=_bounds(), row_count=0, seed=1)
    with pytest.raises(ValueError, match="lower/upper pair"):
        build_lhs_design(design_id="d1", bounds={"w": [0.0], "h": [0.0, 1.0]}, row_count=4, seed=1)
    with pytest.raises(ValueError, match="seed"):
        build_lhs_design(design_id="d1", bounds=_bounds(), row_count=4, seed=-1)


def test_lhs_design_rejects_tampering_and_nonreproducible_seed() -> None:
    design = build_lhs_design(design_id="d1", bounds=_bounds(), row_count=6, seed=17)
    tampered = dict(design)
    tampered["seed"] = 18
    with pytest.raises(ValueError):
        validate_lhs_design(tampered)
    # Re-sealing with a seed that cannot reproduce the stored points is refused.
    body = {k: v for k, v in design.items() if k != "design_sha256"}
    body["seed"] = 18
    resealed = {**body, "design_sha256": canonical_sha256_v1(body)}
    with pytest.raises(ValueError, match="not reproducible"):
        validate_lhs_design(resealed)


def test_design_points_are_unique() -> None:
    design = build_lhs_design(design_id="d1", bounds=_bounds(), row_count=16, seed=43)
    rendered = [tuple(point[name] for name in design["variable_order"]) for point in design["points"]]
    assert len(set(rendered)) == len(rendered)


# --------------------------------------------------------------------------
# Solver-free guard
# --------------------------------------------------------------------------


def test_rows_module_is_solver_free() -> None:
    import comsol_mcp.surrogate.rows as module

    source = open(module.__file__, encoding="utf-8").read().lower()
    for banned in SOLVER_FREE_BANNED:
        assert banned not in source, f"rows references {banned}"
