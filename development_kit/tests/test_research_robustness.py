"""Solver-free robustness matrix and threshold-separation gates."""

import pytest

from comsol_mcp.research.robustness import axis_perturbation_matrix, summarize_robustness
from development_kit.tests.test_research_contracts import _space


def _matrix():
    return axis_perturbation_matrix(
        _space(), {"patch_length_x": 100.0, "patch_length_y": 80.0}, relative_fraction=0.01
    )


def test_axis_matrix_is_deterministic_bounded_and_complete():
    first = _matrix()
    assert first == _matrix()
    assert [point["point_id"] for point in first["points"]] == [
        "center",
        "patch_length_x:minus",
        "patch_length_x:plus",
        "patch_length_y:minus",
        "patch_length_y:plus",
    ]


def test_summary_preserves_losses_and_separates_optional_threshold():
    matrix = _matrix()
    rows = [
        {
            "point_id": point["point_id"],
            "total_loss": index / 10,
            "evidence_fingerprint": f"{index + 1:064x}",
        }
        for index, point in enumerate(matrix["points"])
    ]
    assert (
        summarize_robustness(matrix, rows, maximum_total_loss=None)["threshold_outcome"]
        == "not_declared"
    )
    strict = summarize_robustness(matrix, rows, maximum_total_loss=0.3)
    assert strict["threshold_outcome"] == "fail"
    assert strict["maximum_total_loss"] == 0.4


def test_axis_matrix_rejects_candidate_without_requested_margin():
    with pytest.raises(ValueError, match="margin"):
        axis_perturbation_matrix(
            _space(), {"patch_length_x": 75.0, "patch_length_y": 80.0}, relative_fraction=0.01
        )
