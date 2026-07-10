"""Unit tests for result normalization without a COMSOL client."""

import numpy as np
import pytest

from src.tools.results import evaluate_global_result, evaluate_result


class FakeModel:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def evaluate(self, expression, **kwargs):
        self.calls.append((expression, kwargs))
        return self.result


def test_evaluate_result_serializes_complex_array():
    model = FakeModel(np.array([1 + 2j, 3 - 4j]))

    result = evaluate_result(model, "ewfd.Ex", dataset="dset1", inner="last")

    assert result["shape"] == [2]
    assert result["value"] == [
        {"real": 1.0, "imag": 2.0},
        {"real": 3.0, "imag": -4.0},
    ]
    assert model.calls == [
        (
            "ewfd.Ex",
            {"unit": None, "dataset": "dset1", "inner": "last", "outer": None},
        )
    ]


def test_evaluate_global_result_preserves_complex_scalar():
    model = FakeModel(np.array([2.5 - 0.25j]))

    result = evaluate_global_result(model, "S11")

    assert result["value"] == {"real": 2.5, "imag": -0.25}


def test_evaluate_global_result_rejects_empty_data():
    model = FakeModel(np.array([]))

    with pytest.raises(ValueError, match="no values"):
        evaluate_global_result(model, "missing")
