from __future__ import annotations

from copy import deepcopy

import numpy as np
import pytest

from src.evidence.field_bundle import normalize_field_evidence_request
from src.evidence.field_dataset import collect_existing_dataset_field_evidence
from tests.test_field_bundle import _request


class _TaggedCollection:
    def __init__(self, values):
        self.values = values

    def tags(self):
        return list(self.values)

    def get(self, tag):
        return self.values[tag]


class _Dataset:
    def __init__(self, solution="sol_on"):
        self.solution = solution

    def getString(self, name):
        if name != "solution":
            raise KeyError(name)
        return self.solution


class _Result:
    def __init__(self, dataset):
        self._dataset = dataset

    def dataset(self):
        return self._dataset


class _JavaModel:
    def __init__(self, solution="sol_on"):
        self._components = _TaggedCollection({"comp1": object()})
        self._datasets = _TaggedCollection({"dset_on": _Dataset(solution)})

    def component(self):
        return self._components

    def result(self):
        return _Result(self._datasets)


class _Model:
    def __init__(self, values=None, solution="sol_on"):
        self.java = _JavaModel(solution)
        self.calls = []
        self.values = self._default_values() if values is None else values

    @staticmethod
    def _default_values():
        x = np.array([-1.0, 1.0, -1.0, 1.0])
        y = np.array([-1.5, -1.5, 1.5, 1.5])
        z = np.full(x.shape, 0.5)
        return [
            (x + 2.0 * y).astype(complex),
            (3.0 * x - y).astype(complex),
            x.astype(complex),
            y.astype(complex),
            z.astype(complex),
        ]

    def evaluate(self, expressions, **kwargs):
        self.calls.append((expressions, kwargs))
        return self.values


def _normalized_request():
    raw = _request(paired=False, png=False)
    raw["views"][0]["source"] = {
        "kind": "existing_dataset",
        "component_tag": "comp1",
        "dataset_name": "研究 1//解 1",
        "dataset_tag": "dset_on",
        "solution_tag": "sol_on",
        "solution_number": 1,
    }
    raw["grid"]["shape"] = [9, 11]
    raw["limits"]["max_grid_points"] = 200
    return normalize_field_evidence_request(raw)


def test_existing_dataset_adapter_verifies_readback_and_writes_artifacts(tmp_path):
    request = _normalized_request()
    model = _Model()
    result = collect_existing_dataset_field_evidence(
        model=model,
        request=request,
        view_id="on",
        artifact_root=tmp_path,
    )

    assert model.calls == [
        (
            ["ewfd.normE", "ewfd.normH", "x", "y", "z"],
            {"dataset": "研究 1//解 1", "inner": 1},
        )
    ]
    assert result["dataset_identity"]["readback_state"] == "verified"
    assert result["dataset_identity"]["solution_tag"] == "sol_on"
    assert result["model_mutated"] is False
    assert result["study_run"] is False
    assert (tmp_path / result["array_artifact"]["relative_path"]).is_file()


def test_adapter_rejects_missing_component_dataset_and_solution_mismatch(tmp_path):
    request = _normalized_request()
    missing_component = _Model()
    missing_component.java._components = _TaggedCollection({"other": object()})
    missing_dataset = _Model()
    missing_dataset.java._datasets = _TaggedCollection({"other": _Dataset()})
    wrong_solution = _Model(solution="sol_other")

    for model, message in (
        (missing_component, "component_tag is not present"),
        (missing_dataset, "dataset_tag is not present"),
        (wrong_solution, "solution readback does not match"),
    ):
        with pytest.raises(ValueError, match=message):
            collect_existing_dataset_field_evidence(
                model=model,
                request=request,
                view_id="on",
                artifact_root=tmp_path / message.split()[0],
            )
        assert model.calls == []


def test_adapter_rejects_complex_nonfinite_and_mismatched_evaluation_arrays(tmp_path):
    request = _normalized_request()
    complex_values = _Model().values
    complex_values[0] = complex_values[0] + 1j
    nonfinite_values = _Model().values
    nonfinite_values[1][0] = np.nan
    mismatched_values = _Model().values
    mismatched_values[-1] = np.ones(2)

    for index, (values, message) in enumerate(
        (
            (complex_values, "explicit real scalar expression"),
            (nonfinite_values, "nonfinite values"),
            (mismatched_values, "incompatible array lengths"),
        )
    ):
        with pytest.raises(ValueError, match=message):
            collect_existing_dataset_field_evidence(
                model=_Model(values=values),
                request=request,
                view_id="on",
                artifact_root=tmp_path / str(index),
            )


def test_adapter_rejects_matrix_source_without_evaluating(tmp_path):
    raw = _request(paired=False, png=False)
    raw["views"][0]["source"] = {
        "kind": "validation_matrix_point",
        "job_id": "job-123",
        "point_id": "on",
        "point_fingerprint": "a" * 64,
        "artifact_id": "audit-on",
        "component_tag": "comp1",
        "dataset_name": "研究 1//解 1",
        "dataset_tag": "dset_on",
        "solution_tag": "sol_on",
    }
    request = normalize_field_evidence_request(raw)
    model = _Model()

    with pytest.raises(ValueError, match="cannot read a validation-matrix source"):
        collect_existing_dataset_field_evidence(
            model=model,
            request=request,
            view_id="on",
            artifact_root=tmp_path,
        )
    assert model.calls == []


def test_adapter_requires_ordered_result_list_and_model_readback(tmp_path):
    request = _normalized_request()
    scalar_model = _Model(values=np.ones(4))
    no_java = _Model()
    no_java.java = None

    with pytest.raises(ValueError, match="preserve expression order and count"):
        collect_existing_dataset_field_evidence(
            model=scalar_model,
            request=request,
            view_id="on",
            artifact_root=tmp_path / "scalar",
        )
    with pytest.raises(ValueError, match="clientapi readback is required"):
        collect_existing_dataset_field_evidence(
            model=no_java,
            request=request,
            view_id="on",
            artifact_root=tmp_path / "no-java",
        )
