"""Solver-free robustness matrix and threshold-separation gates."""

import pytest

from comsol_mcp.research.robustness import (
    axis_perturbation_matrix,
    summarize_optional_fidelity_bridge,
    summarize_robustness,
)
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


def test_optional_fidelity_bridge_preserves_not_applicable_and_threshold_states():
    skipped = summarize_optional_fidelity_bridge(
        applicable=False,
        primary={"peak_wavelength_nm": 1550.0},
        independent=None,
        maximum_absolute_differences=None,
    )
    assert skipped["outcome"] == "not_applicable"
    compared = summarize_optional_fidelity_bridge(
        applicable=True,
        primary={"peak_wavelength_nm": 1550.0, "q_factor": 20.0},
        independent={"peak_wavelength_nm": 1552.0, "q_factor": 19.5},
        maximum_absolute_differences={"peak_wavelength_nm": 3.0, "q_factor": 1.0},
    )
    assert compared["outcome"] == "pass"
    assert compared["differences"] == {"peak_wavelength_nm": 2.0, "q_factor": 0.5}


def test_non_applicable_fidelity_rejects_hidden_comparison_evidence():
    with pytest.raises(ValueError, match="non-applicable"):
        summarize_optional_fidelity_bridge(
            applicable=False,
            primary={"peak_wavelength_nm": 1550.0},
            independent={"peak_wavelength_nm": 1550.0},
            maximum_absolute_differences=None,
        )
