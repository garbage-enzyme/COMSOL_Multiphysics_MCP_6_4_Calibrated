from __future__ import annotations

import builtins

import pytest

from src.evidence.field_discovery import discover_field_datasets


class _JavaSolution:
    def __init__(self, empty=False, fail=False):
        self.empty = empty
        self.fail = fail

    def isEmpty(self):
        if self.fail:
            raise RuntimeError("unavailable")
        return self.empty


class _Node:
    def __init__(self, name, tag, node_type, properties=None, *, empty=False, fail=False):
        self._name = name
        self._tag = tag
        self._type = node_type
        self._properties = properties or {}
        self.java = _JavaSolution(empty=empty, fail=fail)

    def name(self):
        return self._name

    def tag(self):
        return self._tag

    def type(self):
        return self._type

    def properties(self):
        return list(self._properties)

    def property(self, name):
        return self._properties[name]


class _Model:
    def __init__(self, *, components=None, datasets=None, solutions=None):
        self.groups = {
            "components": components or [_Node("组件 1", "comp1", "Component")],
            "datasets": datasets
            or [_Node("研究 1//解 1", "dset1", "Solution", {"solution": "sol1"})],
            "solutions": solutions or [_Node("解 1", "sol1", "Solution", empty=False)],
        }

    def __truediv__(self, group):
        return self.groups[group]


def test_discovery_pairs_unicode_names_with_stable_tags_without_solver_import(monkeypatch):
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "mph" or name.startswith("mph."):
            raise AssertionError("discovery module must not import MPh")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    result = discover_field_datasets(_Model())

    assert result["components"] == [
        {"component_name": "组件 1", "component_tag": "comp1"}
    ]
    dataset = result["datasets"][0]
    assert dataset["dataset_name"] == "研究 1//解 1"
    assert dataset["dataset_tag"] == "dset1"
    assert dataset["solution_tag"] == "sol1"
    assert dataset["solution_name"] == "解 1"
    assert dataset["computed_state"] == "verified_computed"
    assert dataset["field_evaluation_eligible"] is True
    assert len(dataset["dataset_identity_sha256"]) == 64
    assert len(result["discovery_sha256"]) == 64


def test_discovery_handles_english_names_empty_unknown_and_non_solution_datasets():
    result = discover_field_datasets(
        _Model(
            components=[_Node("Component 1", "comp1", "Component")],
            datasets=[
                _Node("Study 1//Solution 1", "dset1", "Solution", {"solution": "sol1"}),
                _Node("Cut Plane 1", "cpl1", "CutPlane", {"data": "dset1"}),
                _Node("Unbound", "dset2", "Solution"),
            ],
            solutions=[
                _Node("Solution 1", "sol1", "Solution", empty=True),
                _Node("Solution 2", "sol2", "Solution", fail=True),
            ],
        )
    )

    assert result["datasets"][0]["computed_state"] == "verified_empty"
    assert result["datasets"][0]["field_evaluation_eligible"] is False
    assert result["datasets"][1]["computed_state"] == "not_solution"
    assert result["datasets"][2]["solution_reference_kind"] is None
    assert result["eligible_dataset_count"] == 0


def test_discovery_limits_fail_before_unbounded_response():
    components = [_Node(f"Component {index}", f"comp{index}", "Component") for index in range(3)]
    datasets = [
        _Node(f"Dataset {index}", f"dset{index}", "Solution", {"solution": "sol1"})
        for index in range(3)
    ]
    model = _Model(components=components, datasets=datasets)

    with pytest.raises(ValueError, match="component count exceeds"):
        discover_field_datasets(model, max_components=2)
    with pytest.raises(ValueError, match="dataset count exceeds"):
        discover_field_datasets(model, max_datasets=2)


def test_discovery_rejects_duplicate_names_tags_and_invalid_clientapi_tags():
    duplicate_name = _Model(
        datasets=[
            _Node("Same", "dset1", "Solution", {"solution": "sol1"}),
            _Node("Same", "dset2", "Solution", {"solution": "sol1"}),
        ]
    )
    duplicate_tag = _Model(
        datasets=[
            _Node("First", "dset1", "Solution", {"solution": "sol1"}),
            _Node("Second", "dset1", "Solution", {"solution": "sol1"}),
        ]
    )
    invalid_tag = _Model(datasets=[_Node("Data", "研究1", "Solution", {"solution": "sol1"})])

    with pytest.raises(ValueError, match="dataset names must be unique"):
        discover_field_datasets(duplicate_name)
    with pytest.raises(ValueError, match="dataset tags must be unique"):
        discover_field_datasets(duplicate_tag)
    with pytest.raises(ValueError, match="exact clientapi tag"):
        discover_field_datasets(invalid_tag)


def test_discovery_does_not_evaluate_or_run_study():
    model = _Model()

    result = discover_field_datasets(model)

    assert result["model_mutated"] is False
    assert result["study_run"] is False
