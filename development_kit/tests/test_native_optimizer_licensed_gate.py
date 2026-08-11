"""Solver-free contracts for the bounded native optimizer licensed gate."""

import numpy as np
import pytest

from development_kit.scripts import native_optimizer_licensed_gate as gate


class _Feature:
    def __init__(self):
        self.values = {}

    def getType(self):
        return "Optimization"

    def set(self, name, value):
        self.values[name] = value

    def getString(self, name):
        return str(self.values[name])


class _Features:
    def __init__(self, feature):
        self.feature = feature

    def tags(self):
        return ["o1"]


class _Solution:
    def __init__(self, feature):
        self._features = _Features(feature)

    def feature(self, tag=None):
        return self._features if tag is None else self._features.feature


class _Solutions:
    def tags(self):
        return ["sol2"]


class _Java:
    def __init__(self, solution):
        self.solution = solution

    def sol(self, tag=None):
        return _Solutions() if tag is None else self.solution


class _Model:
    def __init__(self):
        self.feature = _Feature()
        self.java = _Java(_Solution(self.feature))


class _Study:
    def __init__(self):
        self.created = []

    def createAutoSequences(self, kind):
        self.created.append(kind)


def test_numeric_series_flattens_exact_values_and_rejects_nonfinite_or_complex():
    assert gate._numeric_series(np.array([[0.1, 0.2]])) == [0.1, 0.2]
    with pytest.raises(ValueError, match="nonfinite"):
        gate._numeric_series(np.array([np.nan]))
    with pytest.raises(ValueError, match="complex"):
        gate._numeric_series(np.array([1 + 1j]))


def test_generated_solver_receives_caller_iteration_and_move_limits():
    model = _Model()
    study = _Study()

    readback = gate._configure_solver_move_limit(model, study, 0.1, 2)

    assert study.created == ["sol"]
    assert readback == {
        "solution_tag": "sol2",
        "feature_tag": "o1",
        "movelimitactive": "on",
        "movelimit": "0.10000000000000001",
        "mmamaxiteractive": "on",
        "mmamaxiter": "2",
    }


def test_requested_solver_iterations_are_separate_from_budget_cap():
    args = type("Args", (), {"optimizer_iterations": 2})()
    assert gate._requested_optimizer_iterations(args, {"max_iterations": 3}) == 2
    args.optimizer_iterations = 4
    with pytest.raises(ValueError, match="exceeds"):
        gate._requested_optimizer_iterations(args, {"max_iterations": 3})


def test_move_limit_is_a_required_positive_caller_input():
    args = type("Args", (), {"move_limit": 0.05})()
    assert gate._requested_move_limit(args) == 0.05
    for value in (None, False, 0.0, -0.1, float("nan"), float("inf")):
        args.move_limit = value
        with pytest.raises(ValueError, match="move_limit"):
            gate._requested_move_limit(args)


def test_gate_parser_has_no_host_resource_defaults():
    parser = gate._parser()
    actions = {action.dest: action.default for action in parser._actions}
    for name in (
        "cores",
        "max_solves",
        "max_iterations",
        "optimizer_iterations",
        "move_limit",
        "max_wall_time_seconds",
        "max_commit_fraction",
        "max_disk_bytes",
        "max_review_items",
        "max_elements_per_model",
        "minimum_element_quality",
    ):
        assert actions[name] is None


def test_mesh_admission_uses_caller_element_and_quality_thresholds():
    accepted = {
        "element_count": 300_000,
        "minimum_quality": 0.1,
        "mean_quality": 0.5,
        "quality_measure": "volcircum",
    }
    gate._admit_mesh(accepted, max_elements=300_000, minimum_quality=0.1)
    too_large = {**accepted, "element_count": 300_001}
    with pytest.raises(ValueError, match="element count"):
        gate._admit_mesh(too_large, max_elements=300_000, minimum_quality=0.1)
    too_low = {**accepted, "minimum_quality": 0.099}
    with pytest.raises(ValueError, match="minimum quality"):
        gate._admit_mesh(too_low, max_elements=300_000, minimum_quality=0.1)
