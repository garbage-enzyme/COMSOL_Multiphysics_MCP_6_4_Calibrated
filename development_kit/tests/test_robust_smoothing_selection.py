"""Evidence-only robust soft-min candidate comparison tests."""

from __future__ import annotations

import copy

import pytest

from comsol_mcp.research.robust_smoothing_selection import (
    compare_robust_smoothing_candidates,
    normalize_robust_smoothing_comparison_receipt,
)
from development_kit.tests.test_robust_conditions import _table
from development_kit.tests.test_robust_objectives import (
    _configuration,
    _gradients,
    _observations,
)


def test_three_fixture_candidates_compare_objective_weights_and_gradient_without_selection():
    receipt = compare_robust_smoothing_candidates(
        _configuration(),
        _table(),
        _observations(),
        _gradients(),
        [0.02, 0.005, 0.01],
    )
    assert receipt["candidate_temperatures"] == [0.005, 0.01, 0.02]
    assert receipt["candidate_count"] == 3
    assert receipt["selection_disposition"] == "review_required"
    assert receipt["selected_temperature"] is None
    assert receipt["automatic_selection_used"] is False
    assert receipt["rows"][0]["gradient_delta_norm_from_previous_candidate"] is None
    assert all(
        row["gradient_delta_norm_from_previous_candidate"] is not None
        for row in receipt["rows"][1:]
    )
    assert len({row["objective_fingerprint"] for row in receipt["rows"]}) == 3
    assert normalize_robust_smoothing_comparison_receipt(receipt) == receipt


@pytest.mark.parametrize(
    "candidates",
    [[0.01], [0.01, 0.01], [0.0, 0.01], [float("nan"), 0.01]],
)
def test_candidate_set_rejects_single_duplicate_nonpositive_or_nonfinite_values(candidates):
    with pytest.raises(ValueError):
        compare_robust_smoothing_candidates(
            _configuration(), _table(), _observations(), _gradients(), candidates
        )


def test_receipt_rejects_auto_selection_row_reordering_or_metric_tampering():
    receipt = compare_robust_smoothing_candidates(
        _configuration(),
        _table(),
        _observations(),
        _gradients(),
        [0.005, 0.01, 0.02],
    )
    selected = copy.deepcopy(receipt)
    selected["selected_temperature"] = 0.01
    selected["automatic_selection_used"] = True
    with pytest.raises(ValueError, match="cannot select automatically"):
        normalize_robust_smoothing_comparison_receipt(selected)
    reordered = copy.deepcopy(receipt)
    reordered["rows"].reverse()
    with pytest.raises(ValueError, match="row order"):
        normalize_robust_smoothing_comparison_receipt(reordered)
    tampered = copy.deepcopy(receipt)
    tampered["rows"][0]["aggregate_objective"] += 0.1
    with pytest.raises(ValueError, match="fingerprint"):
        normalize_robust_smoothing_comparison_receipt(tampered)
