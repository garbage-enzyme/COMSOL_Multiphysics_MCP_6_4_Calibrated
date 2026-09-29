"""Solver-free surrogate registry, lifecycle, drift, OOD, and evidence tests."""

from __future__ import annotations

import pytest

from comsol_mcp.durable.canonical import canonical_sha256_v1
from comsol_mcp.surrogate.registry import (
    LIFECYCLE_STATES,
    advance_registry_entry,
    assert_no_contract_drift,
    assert_prediction_not_evidence,
    build_model_card,
    build_registry_entry,
    detect_drift,
    evaluate_ood,
    validate_model_card,
    validate_registry_entry,
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

IDENTITIES = {
    "dataset_manifest_sha256": "a" * 64,
    "split_manifest_sha256": "b" * 64,
    "field_schema_sha256": "c" * 64,
    "transforms_sha256": "d" * 64,
    "architecture_sha256": "e" * 64,
    "comsol_build": "6.4.0.293",
    "objective": "maximize_R_at_1550nm",
}


def _card(**overrides) -> dict:
    kwargs = {
        "model_id": "m1",
        "lineage_id": "lin1",
        "identities": dict(IDENTITIES),
        "metrics": {"test_rmse": 0.01, "test_mae": 0.005},
        "trained_chksum": "f" * 64,
    }
    kwargs.update(overrides)
    return build_model_card(**kwargs)


# --------------------------------------------------------------------------
# Model card
# --------------------------------------------------------------------------


def test_model_card_is_sealed_and_never_upgrades_evidence() -> None:
    card = _card()
    assert card["never_upgrades_fem_evidence"] is True
    assert card["scientific_disposition"] == "predicted"
    assert card["identities"]["comsol_build"] == "6.4.0.293"
    validate_model_card(card)


def test_model_card_rejects_tampering_and_nonhex_identities() -> None:
    card = _card()
    tampered = dict(card)
    tampered["metrics"] = {"test_rmse": 0.0}
    with pytest.raises(ValueError):
        validate_model_card(tampered)
    with pytest.raises(ValueError, match="hex digest"):
        _card(identities={**IDENTITIES, "dataset_manifest_sha256": "short"})
    with pytest.raises(ValueError, match="finite"):
        _card(metrics={"test_rmse": float("inf")})


def test_model_card_cannot_declare_fem_verified() -> None:
    card = _card()
    unsealed = {k: v for k, v in card.items() if k != "entry_sha256"}
    unsealed["scientific_disposition"] = "fem_verified"
    with pytest.raises(ValueError, match="cannot declare fem_verified"):
        validate_model_card({**unsealed, "entry_sha256": canonical_sha256_v1(unsealed)})


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def test_full_lifecycle_advances_in_order() -> None:
    entry = build_registry_entry(
        model_id="m1",
        lineage_id="lin1",
        state="draft",
        model_card_sha256="a" * 64,
        artifacts={"model_card": "b" * 64},
    )
    for state in (
        "data_validated",
        "configured",
        "training",
        "trained",
        "tested",
        "calibrated",
        "accepted",
    ):
        entry = advance_registry_entry(entry, to_state=state)
        validate_registry_entry(entry)
    assert entry["state"] == "accepted"
    assert entry["immutable"] is True
    assert entry["transition_count"] == len(LIFECYCLE_STATES) - 1


def test_accepted_entry_is_immutable() -> None:
    entry = build_registry_entry(
        model_id="m1",
        lineage_id="lin1",
        state="accepted",
        model_card_sha256="a" * 64,
        artifacts={"model_card": "b" * 64},
    )
    assert entry["immutable"] is True
    with pytest.raises(ValueError, match="immutable"):
        advance_registry_entry(entry, to_state="retired")


def test_lifecycle_rejects_skipped_and_backward_transitions() -> None:
    entry = build_registry_entry(
        model_id="m1",
        lineage_id="lin1",
        state="draft",
        model_card_sha256="a" * 64,
        artifacts={"model_card": "b" * 64},
    )
    with pytest.raises(ValueError, match="not permitted"):
        advance_registry_entry(entry, to_state="training")
    with pytest.raises(ValueError, match="not permitted"):
        advance_registry_entry(entry, to_state="accepted")
    advanced = advance_registry_entry(entry, to_state="data_validated")
    with pytest.raises(ValueError, match="not permitted"):
        advance_registry_entry(advanced, to_state="draft")


def test_terminal_error_state_has_no_forward_transition() -> None:
    entry = build_registry_entry(
        model_id="m1",
        lineage_id="lin1",
        state="draft",
        model_card_sha256="a" * 64,
        artifacts={"model_card": "b" * 64},
    )
    failed = advance_registry_entry(entry, to_state="cancelled")
    assert failed["state"] == "cancelled"
    with pytest.raises(ValueError, match="not permitted"):
        advance_registry_entry(failed, to_state="training")


def test_registry_entry_rejects_tampering_and_bad_state() -> None:
    entry = build_registry_entry(
        model_id="m1",
        lineage_id="lin1",
        state="draft",
        model_card_sha256="a" * 64,
        artifacts={"model_card": "b" * 64},
    )
    tampered = dict(entry)
    tampered["state"] = "accepted"
    with pytest.raises(ValueError):
        validate_registry_entry(tampered)
    with pytest.raises(ValueError, match="unknown lifecycle state"):
        build_registry_entry(
            model_id="m1",
            lineage_id="lin1",
            state="fabricated",
            model_card_sha256="a" * 64,
            artifacts={"model_card": "b" * 64},
        )


def test_registry_history_is_append_only() -> None:
    entry = build_registry_entry(
        model_id="m1",
        lineage_id="lin1",
        state="draft",
        model_card_sha256="a" * 64,
        artifacts={"model_card": "b" * 64},
    )
    first_history = [dict(item) for item in entry["history"]]
    advanced = advance_registry_entry(entry, to_state="data_validated")
    assert advanced["history"][: len(first_history)] == first_history
    assert len(advanced["history"]) == len(first_history) + 1


# --------------------------------------------------------------------------
# Drift
# --------------------------------------------------------------------------


def test_drift_detects_contract_identity_change() -> None:
    previous = _card()
    current = _card(identities={**IDENTITIES, "comsol_build": "6.4.0.300"})
    report = detect_drift(previous, current)
    assert report["drift_detected"] is True
    assert report["requires_new_lineage"] is True
    assert "comsol_build" in report["changed_fields"]
    assert report["automatic_retraining"] is False
    with pytest.raises(ValueError, match="drift_requires_new_lineage"):
        assert_no_contract_drift(previous, current)


def test_identical_identities_show_no_drift() -> None:
    report = assert_no_contract_drift(_card(), _card())
    assert report["drift_detected"] is False
    assert report["requires_new_lineage"] is False


def test_non_contract_identity_change_does_not_force_new_lineage() -> None:
    previous = _card()
    current = _card(identities={**IDENTITIES, "campaign": "campaign-B"})
    report = detect_drift(previous, current)
    assert report["drift_detected"] is True
    assert report["requires_new_lineage"] is False


# --------------------------------------------------------------------------
# OOD policy
# --------------------------------------------------------------------------


def _bounds() -> dict:
    return {"lower": [0.0, 0.0], "upper": [10.0, 10.0]}


def _train_rows() -> list[list[float]]:
    return [[1.0, 1.0], [2.0, 2.0], [3.0, 3.0], [4.0, 4.0]]


def test_ood_refuses_to_clip_and_escalates_out_of_bounds() -> None:
    report = evaluate_ood(
        query=[50.0, 5.0],
        train_rows=_train_rows(),
        bounds=_bounds(),
        calibration={"envelope": 0.02},
    )
    assert report["state"] == "out_of_domain"
    assert report["fem_escalation_required"] is True
    assert report["input_clipped"] is False


def test_ood_marks_near_boundary_query_as_edge() -> None:
    # Nearest training row is [4, 4] over a width-10 box, so a query at 4.8 has
    # a normalized support ratio of 0.08: above edge_fraction (0.05) and at or
    # below twice it (0.10), which is the declared edge band.
    report = evaluate_ood(
        query=[4.8, 4.8],
        train_rows=_train_rows(),
        bounds=_bounds(),
        calibration={"envelope": 0.02},
        edge_fraction=0.05,
    )
    assert report["state"] == "edge"
    assert report["fem_escalation_required"] is True
    assert report["input_clipped"] is False
    assert report["nearest_support_ratio"] == pytest.approx(0.08)


def test_ood_support_ratio_boundary_is_exact() -> None:
    """A query exactly at edge_fraction is still in-domain; just past it is edge."""
    at_boundary = evaluate_ood(
        query=[4.5, 4.5],
        train_rows=_train_rows(),
        bounds=_bounds(),
        calibration={"envelope": 0.02},
        edge_fraction=0.05,
    )
    assert at_boundary["state"] == "in_domain"
    past_boundary = evaluate_ood(
        query=[4.6, 4.6],
        train_rows=_train_rows(),
        bounds=_bounds(),
        calibration={"envelope": 0.02},
        edge_fraction=0.05,
    )
    assert past_boundary["state"] == "edge"


def test_ood_in_domain_query_needs_no_escalation() -> None:
    report = evaluate_ood(
        query=[2.0, 2.0],
        train_rows=_train_rows(),
        bounds=_bounds(),
        calibration={"envelope": 0.02},
    )
    assert report["state"] == "in_domain"
    assert report["fem_escalation_required"] is False


def test_uncalibrated_model_is_never_in_domain() -> None:
    report = evaluate_ood(
        query=[2.0, 2.0], train_rows=_train_rows(), bounds=_bounds(), calibration=None
    )
    assert report["state"] == "uncalibrated"
    assert report["fem_escalation_required"] is True


def test_ood_rejects_mismatched_bounds_and_dimensions() -> None:
    with pytest.raises(ValueError, match="match the query dimension"):
        evaluate_ood(
            query=[1.0, 2.0],
            train_rows=_train_rows(),
            bounds={"lower": [0.0], "upper": [10.0]},
        )
    with pytest.raises(ValueError, match="requires lower < upper"):
        evaluate_ood(
            query=[1.0, 2.0],
            train_rows=_train_rows(),
            bounds={"lower": [0.0, 5.0], "upper": [10.0, 5.0]},
        )


# --------------------------------------------------------------------------
# Evidence separation
# --------------------------------------------------------------------------


def test_prediction_cannot_carry_fem_evidence() -> None:
    with pytest.raises(ValueError, match="cannot carry FEM evidence"):
        assert_prediction_not_evidence(
            {"scientific_disposition": "predicted", "fem_evidence": {"R": 0.5}}
        )
    with pytest.raises(ValueError, match="cannot be fem_verified"):
        assert_prediction_not_evidence({"scientific_disposition": "fem_verified"})
    accepted = assert_prediction_not_evidence({"scientific_disposition": "predicted"})
    assert accepted["never_upgrades_fem_evidence"] is True


# --------------------------------------------------------------------------
# Solver-free guard
# --------------------------------------------------------------------------


def test_registry_module_is_solver_free() -> None:
    import comsol_mcp.surrogate.registry as registry_mod

    source = open(registry_mod.__file__, encoding="utf-8").read().lower()
    for banned in SOLVER_FREE_BANNED:
        assert banned not in source, f"registry references {banned}"
