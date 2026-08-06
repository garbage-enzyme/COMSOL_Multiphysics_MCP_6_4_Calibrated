"""Bounded solver-free gates for the explicit GP/EI acquisition path."""

from __future__ import annotations

import importlib
import sys

import pytest

from development_kit.tests.test_research_contracts import _space


def _selector():
    module = importlib.import_module("comsol_mcp.research.adaptive_acquisition")
    return module.select_expected_improvement_candidate


def test_explicit_gp_selector_is_deterministic_bounded_and_observation_aware():
    observations = [
        {"values": {"patch_length_x": 75.0, "patch_length_y": 60.0}, "loss": 4.0},
        {"values": {"patch_length_x": 125.0, "patch_length_y": 100.0}, "loss": 3.0},
        {"values": {"patch_length_x": 100.0, "patch_length_y": 80.0}, "loss": 0.5},
    ]
    candidates = [
        {"patch_length_x": 90.0, "patch_length_y": 72.0},
        {"patch_length_x": 105.0, "patch_length_y": 84.0},
        {"patch_length_x": 115.0, "patch_length_y": 68.0},
    ]
    first = _selector()(_space(), observations, candidates)
    second = _selector()(_space(), observations, candidates)
    assert first == second
    assert first["values"] in candidates
    assert 0 <= first["selected_index"] < len(candidates)
    assert first["posterior_standard_deviation"] >= 0.0
    assert first["expected_improvement"] >= 0.0
    assert first["observation_count"] == 3
    assert first["candidate_count"] == 3


@pytest.mark.parametrize(
    ("observations", "candidates", "message"),
    [
        (
            [
                {"values": {"patch_length_x": 75.0, "patch_length_y": 60.0}, "loss": 1.0},
                {"values": {"patch_length_x": 75.0, "patch_length_y": 60.0}, "loss": 2.0},
            ],
            [{"patch_length_x": 100.0, "patch_length_y": 80.0}],
            "duplicate",
        ),
        (
            [
                {"values": {"patch_length_x": 75.0, "patch_length_y": 60.0}, "loss": 1.0},
                {"values": {"patch_length_x": 125.0, "patch_length_y": 100.0}, "loss": 2.0},
            ],
            [{"patch_length_x": 130.0, "patch_length_y": 80.0}],
            "outside",
        ),
        (
            [
                {"values": {"patch_length_x": 75.0, "patch_length_y": 60.0}, "loss": 1.0},
                {
                    "values": {"patch_length_x": 125.0, "patch_length_y": 100.0},
                    "loss": float("nan"),
                },
            ],
            [{"patch_length_x": 100.0, "patch_length_y": 80.0}],
            "finite",
        ),
    ],
)
def test_gp_selector_rejects_duplicate_out_of_domain_and_nonfinite_inputs(
    observations, candidates, message
):
    with pytest.raises(ValueError, match=message):
        _selector()(_space(), observations, candidates)


def test_ordinary_research_import_does_not_load_numpy_or_scipy(monkeypatch):
    for name in list(sys.modules):
        if name == "comsol_mcp.research" or name.startswith("comsol_mcp.research."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    before = set(sys.modules)
    importlib.import_module("comsol_mcp.research")
    loaded = set(sys.modules) - before
    assert not any(name == "numpy" or name.startswith("scipy") for name in loaded)
