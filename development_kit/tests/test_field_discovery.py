from __future__ import annotations

import builtins
import subprocess
import sys

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
        self._properties = {} if properties is None else properties
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
            "components": (
                [_Node("组件 1", "comp1", "Component")] if components is None else components
            ),
            "datasets": (
                [_Node("研究 1//解 1", "dset1", "Solution", {"solution": "sol1"})]
                if datasets is None
                else datasets
            ),
            "solutions": (
                [_Node("解 1", "sol1", "Solution", empty=False)] if solutions is None else solutions
            ),
        }

    def __truediv__(self, group):
        return self.groups[group]

    def evaluate(self, *_args, **_kwargs):
        pytest.fail("discovery must not evaluate the model")

    def solve(self, *_args, **_kwargs):
        pytest.fail("discovery must not solve the model")

    def run(self, *_args, **_kwargs):
        pytest.fail("discovery must not run a study")


def _model_snapshot(model):
    return {
        group: [
            {
                "name": node._name,
                "tag": node._tag,
                "type": node._type,
                "properties": dict(node._properties),
                "empty": node.java.empty,
                "fail": node.java.fail,
            }
            for node in nodes
        ]
        for group, nodes in model.groups.items()
    }


def test_discovery_pairs_unicode_names_with_stable_tags_without_solver_import(monkeypatch):
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "mph" or name.startswith("mph."):
            raise AssertionError("discovery module must not import MPh")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    result = discover_field_datasets(_Model())

    assert result["components"] == [{"component_name": "组件 1", "component_tag": "comp1"}]
    dataset = result["datasets"][0]
    assert dataset["dataset_name"] == "研究 1//解 1"
    assert dataset["dataset_tag"] == "dset1"
    assert dataset["solution_tag"] == "sol1"
    assert dataset["solution_name"] == "解 1"
    assert dataset["computed_state"] == "verified_computed"
    assert dataset["field_evaluation_eligible"] is True
    assert len(dataset["dataset_identity_sha256"]) == 64
    assert len(result["discovery_sha256"]) == 64


def test_discovery_module_import_is_solver_free_in_a_clean_process():
    script = r"""
import builtins

real_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if name == "mph" or name.startswith("mph."):
        raise AssertionError("field discovery module must not import MPh")
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
from comsol_mcp.evidence.field_discovery import discover_field_datasets
assert callable(discover_field_datasets)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_discovery_handles_english_names_empty_unknown_and_non_solution_datasets():
    result = discover_field_datasets(
        _Model(
            components=[_Node("Component 1", "comp1", "Component")],
            datasets=[
                _Node("Study 1//Solution 1", "dset1", "Solution", {"solution": "sol1"}),
                _Node("Cut Plane 1", "cpl1", "CutPlane", {"data": "dset1"}),
                _Node("Unbound", "dset2", "Solution"),
                _Node("Unavailable", "dset3", "Solution", {"solution": "sol2"}),
            ],
            solutions=[
                _Node("Solution 1", "sol1", "Solution", empty=True),
                _Node("Solution 2", "sol2", "Solution", fail=True),
            ],
        )
    )

    assert result["datasets"][0]["computed_state"] == "verified_empty"
    assert result["datasets"][0]["field_evaluation_eligible"] is False
    assert result["datasets"][1]["solution_reference_kind"] == "data"
    assert result["datasets"][1]["solution_tag"] == "sol1"
    assert result["datasets"][1]["computed_state"] == "verified_empty"
    assert result["datasets"][2]["solution_reference_kind"] is None
    assert result["datasets"][3]["solution_tag"] == "sol2"
    assert result["datasets"][3]["computed_state"] == "unknown"
    assert result["datasets"][3]["field_evaluation_eligible"] is False
    assert result["eligible_dataset_count"] == 0
    assert result["success"] is False
    assert result["discovery_state"] == "partial"
    assert result["solution_diagnostics"] == [
        {
            "code": "solution_state_unavailable",
            "solution_tag": "sol2",
            "error_type": "RuntimeError",
        }
    ]


def test_discovery_resolves_nested_dataset_references_to_the_terminal_solution():
    result = discover_field_datasets(
        _Model(
            datasets=[
                _Node("Solution", "dset1", "Solution", {"solution": "sol1"}),
                _Node("Cut Plane", "cpl1", "CutPlane", {"data": "dset1"}),
                _Node("Derived", "drv1", "Derived", {"data": "cpl1"}),
            ],
            solutions=[_Node("Solution 1", "sol1", "Solution", empty=False)],
        )
    )

    assert [item["solution_tag"] for item in result["datasets"]] == ["sol1"] * 3
    assert [item["computed_state"] for item in result["datasets"]] == ["verified_computed"] * 3
    assert result["eligible_dataset_count"] == 3


def test_discovery_fixture_preserves_explicit_empty_collections():
    result = discover_field_datasets(_Model(components=[], datasets=[], solutions=[]))

    assert result["components"] == []
    assert result["datasets"] == []
    assert result["component_count"] == 0
    assert result["dataset_count"] == 0
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


def test_discovery_stops_iterating_at_the_declared_limit():
    class CountingChildren:
        def __init__(self):
            self.yielded = 0

        def __iter__(self):
            while True:
                self.yielded += 1
                yield _Node(
                    f"Component {self.yielded}",
                    f"comp{self.yielded}",
                    "Component",
                )

    children = CountingChildren()
    model = _Model()
    model.groups["components"] = children

    with pytest.raises(ValueError, match="component count exceeds"):
        discover_field_datasets(model, max_components=2)

    assert children.yielded == 3


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


@pytest.mark.parametrize("reference", [None, "", "bad tag", "x" * 129, object()])
def test_discovery_rejects_malformed_dataset_references(reference):
    model = _Model(datasets=[_Node("Data", "dset1", "Solution", {"solution": reference})])

    with pytest.raises(ValueError, match="exact clientapi tag|bounded nonempty text"):
        discover_field_datasets(model)


def test_discovery_does_not_evaluate_or_run_study():
    model = _Model()
    before = _model_snapshot(model)

    result = discover_field_datasets(model)

    assert _model_snapshot(model) == before
    assert result["model_mutated"] is False
    assert result["study_run"] is False
