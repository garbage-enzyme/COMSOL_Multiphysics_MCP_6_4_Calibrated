from __future__ import annotations

import hashlib

import numpy as np
import pytest
from src.evidence.field_bundle import normalize_field_evidence_request
from src.evidence.field_dataset import (
    collect_existing_dataset_field_evidence,
    collect_validation_matrix_field_evidence,
)

from development_kit.tests.test_field_bundle import _request


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


class _MphNode:
    def __init__(self, name, tag):
        self._name = name
        self._tag = tag

    def name(self):
        return self._name

    def tag(self):
        return self._tag


class _Model:
    def __init__(
        self,
        values=None,
        solution="sol_on",
        dataset_name="研究 1//解 1",
        dataset_tag="dset_on",
    ):
        self.java = _JavaModel(solution)
        self.calls = []
        self.values = self._default_values() if values is None else values
        self.groups = {"datasets": [_MphNode(dataset_name, dataset_tag)]}

    def __truediv__(self, group):
        return self.groups[group]

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
        "source_model_sha256": "d" * 64,
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
            [
                "ewfd.normE",
                "ewfd.normH",
                "x",
                "y",
                "z",
                "comp1.x",
                "comp1.y",
                "comp1.z",
            ],
            {"dataset": "研究 1//解 1", "inner": [1]},
        )
    ]
    assert result["dataset_identity"]["readback_state"] == "verified"
    assert result["dataset_identity"]["solution_tag"] == "sol_on"
    assert result["model_mutated"] is False
    assert result["study_run"] is False
    array_path = tmp_path / result["array_artifact"]["relative_path"]
    assert array_path.is_file()
    assert result["array_artifact"]["sha256"] == hashlib.sha256(array_path.read_bytes()).hexdigest()
    with np.load(array_path, allow_pickle=False) as archive:
        assert archive.files == [
            "coordinate_x",
            "coordinate_y",
            "quantity_electric_norm",
            "quantity_magnetic_norm",
        ]
        assert archive["quantity_electric_norm"].shape == (9, 11)
        assert archive["quantity_magnetic_norm"].shape == (9, 11)
        assert np.allclose(archive["coordinate_x"], np.linspace(-1.0, 1.0, 11))
        assert np.allclose(archive["coordinate_y"], np.linspace(-1.5, 1.5, 9))
        xx, yy = np.meshgrid(archive["coordinate_x"], archive["coordinate_y"])
        assert np.allclose(archive["quantity_electric_norm"], xx + 2.0 * yy)
        assert np.allclose(archive["quantity_magnetic_norm"], 3.0 * xx - yy)


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


def test_adapter_binds_mph_dataset_name_to_tag_before_evaluation(tmp_path):
    request = _normalized_request()

    for model in (
        _Model(dataset_name="Different dataset"),
        _Model(dataset_tag="dset_other"),
    ):
        with pytest.raises(ValueError, match="dataset_name"):
            collect_existing_dataset_field_evidence(
                model=model,
                request=request,
                view_id="on",
                artifact_root=tmp_path / model.groups["datasets"][0].tag(),
            )
        assert model.calls == []


def test_adapter_rejects_dataset_not_bound_to_declared_component(tmp_path):
    request = _normalized_request()
    model = _Model()
    model.values[-3] = model.values[-3] + 0.25

    with pytest.raises(ValueError, match="declared component_tag"):
        collect_existing_dataset_field_evidence(
            model=model,
            request=request,
            view_id="on",
            artifact_root=tmp_path,
        )


def test_adapter_allows_only_rounding_scale_coordinate_differences(tmp_path):
    request = _normalized_request()
    values = _Model().values
    values[-3] = values[-3] + 1.0e-14
    result = collect_existing_dataset_field_evidence(
        model=_Model(values=values), request=request, view_id="on", artifact_root=tmp_path
    )
    assert result["dataset_identity"]["readback_state"] == "verified"


def test_adapter_rejects_complex_nonfinite_and_mismatched_evaluation_arrays(tmp_path):
    request = _normalized_request()
    complex_values = _Model().values
    complex_values[0] = complex_values[0] + 1j
    nonfinite_values = _Model().values
    nonfinite_values[1][0] = np.nan
    empty_values = _Model().values
    empty_values[0] = np.array([])
    nonnumeric_values = _Model().values
    nonnumeric_values[0] = np.array(["field"] * 4)
    first_quantity_mismatch = _Model().values
    first_quantity_mismatch[0] = np.ones(2)
    second_quantity_mismatch = _Model().values
    second_quantity_mismatch[1] = np.ones(2)
    coordinate_mismatch = _Model().values
    coordinate_mismatch[-1] = np.ones(2)

    for index, (values, message) in enumerate(
        (
            (complex_values, "explicit real scalar expression"),
            (nonfinite_values, "nonfinite values"),
            (empty_values, "nonempty numeric array"),
            (nonnumeric_values, "nonempty numeric array"),
            (first_quantity_mismatch, "incompatible array lengths"),
            (second_quantity_mismatch, "incompatible array lengths"),
            (coordinate_mismatch, "incompatible array lengths"),
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
        "source_model_sha256": "d" * 64,
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


def test_validation_matrix_adapter_reads_exact_bound_dataset(tmp_path):
    raw = _request(paired=False, png=False)
    raw["views"][0]["source"] = {
        "kind": "validation_matrix_point",
        "source_model_sha256": "d" * 64,
        "job_id": "job-123",
        "point_id": "target",
        "point_fingerprint": "a" * 64,
        "artifact_id": "audit-target",
        "component_tag": "comp1",
        "dataset_name": "研究 1//解 1",
        "dataset_tag": "dset_on",
        "solution_tag": "sol_on",
    }
    raw["grid"]["shape"] = [9, 11]
    raw["limits"]["max_grid_points"] = 200
    request = normalize_field_evidence_request(raw)
    model = _Model()

    result = collect_validation_matrix_field_evidence(
        model=model,
        request=request,
        view_id="on",
        artifact_root=tmp_path,
    )

    assert model.calls[0][1] == {"dataset": "研究 1//解 1", "inner": None}
    assert result["dataset_identity"]["source_kind"] == "validation_matrix_point"
    assert result["dataset_identity"]["job_id"] == "job-123"
    assert result["dataset_identity"]["source_artifact_id"] == "audit-target"


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
