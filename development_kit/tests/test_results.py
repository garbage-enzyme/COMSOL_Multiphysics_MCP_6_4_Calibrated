"""Unit tests for result normalization without a COMSOL client."""

from pathlib import Path

import numpy as np
import pytest
from src.tools.results import (
    _json_safe_solution_axis,
    evaluate_global_result,
    evaluate_result,
    export_result_file,
)


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


def test_evaluate_global_result_rejects_multiple_solution_values():
    model = FakeModel(np.array([1.0, 2.0]))

    with pytest.raises(ValueError, match="exactly one value"):
        evaluate_global_result(model, "ambiguous")


def test_solution_axis_normalization_preserves_every_json_safe_complex_value():
    values = np.array([1 + 2j, 3 - 4j])

    assert _json_safe_solution_axis(values) == [
        {"real": 1.0, "imag": 2.0},
        {"real": 3.0, "imag": -4.0},
    ]


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        complex(float("nan"), 0.0),
        np.float64("-inf"),
    ],
)
def test_result_normalization_rejects_nonfinite_public_values(value):
    model = FakeModel(np.asarray([value]))

    with pytest.raises(ValueError, match="finite"):
        evaluate_result(model, "unsafe")


def test_result_export_preserves_a_target_created_during_staging(tmp_path):
    target = tmp_path / "result.csv"

    class ExportModel:
        def export(self, _node_name, staging):
            Path(staging).write_bytes(b"ours")
            target.write_bytes(b"competitor")

    with pytest.raises(FileExistsError):
        export_result_file(ExportModel(), "data1", str(target))

    assert target.read_bytes() == b"competitor"
    assert not list(tmp_path.glob(".*.export"))
