"""Independent wavelength controls, failure restoration and resume regressions."""

from __future__ import annotations

import csv
from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pytest

from comsol_mcp.strict_wavelength import normalize_policy, requested_metres, verify_metres
from comsol_mcp.tools.workflow import run_staged_parametric_sweep
from development_kit.tests.test_workflow import FakeModel, FakeStep, read_csv

POLICY = {"relative_tolerance": 1e-8, "absolute_tolerance_m": 1e-15, "dataset_tag": "dset1"}


class TypedStep(FakeStep):
    def __init__(self):
        super().__init__()
        self.properties = {"plist": [1.0, 2.0], "punit": "um"}
        self.step_type = "Wavelength"

    def getType(self):
        return self.step_type

    def getValueType(self, key):
        return "DoubleArray" if key == "plist" else "String"

    def getDoubleArray(self, key):
        return list(self.properties[key])

    def getString(self, key):
        return self.properties[key]


class WavelengthModel(FakeModel):
    """Use the study list for the solve and the parameter for evaluation separately."""

    def __init__(self, *, solved_override=None, evaluated_override=None, dataset_study="std1"):
        super().__init__()
        self.java.study_node.step = TypedStep()
        self.java.study_node.features["wave"] = self.java.study_node.step
        self.java.result = lambda: SimpleNamespace(
            dataset=lambda tag: SimpleNamespace(getString=lambda key: "sol1")
        )
        self.java.sol = lambda tag: SimpleNamespace(study=lambda: dataset_study)
        self.solved_override = solved_override
        self.evaluated_override = evaluated_override
        self.evaluations = []
        self.dataset_node = SimpleNamespace(tag=lambda: "dset1")

    def __truediv__(self, name):
        assert name == "datasets"
        return [self.dataset_node]

    def evaluate(self, expressions, *, unit=None, dataset=None):
        assert dataset is None or dataset is self.dataset_node, "MPh rejects Java dataset tags"
        self.evaluations.append((expressions, unit, dataset))
        names = [expressions] if isinstance(expressions, str) else expressions
        requested = next(iter(self.java.parameters.values.values()))
        evaluated = (
            requested_metres(requested, None)
            if self.evaluated_override is None
            else self.evaluated_override
        )
        solved = (
            self.java.study_node.step.properties["plist"][0]
            if self.solved_override is None
            else self.solved_override
        )
        values = {"wl": evaluated, "wavelength": evaluated, "c_const/ewfd.freq": solved, "A": 0.25}
        arrays = [np.array([values[name]]) for name in names]
        return arrays[0] if len(arrays) == 1 else arrays


def run(model, tmp_path, **changes):
    arguments = dict(
        parameter_unit="um",
        study_name="std1",
        study_step_tag="wave",
        csv_path=str(tmp_path / "points.csv"),
        strict_wavelength_policy=POLICY,
    )
    arguments.update(changes)
    return run_staged_parametric_sweep(model, "wl", [5.0, 5.002], ["A"], **arguments)


@pytest.mark.parametrize("name", ["wl", "wavelength"])
def test_strict_points_use_single_point_si_controls_and_restore(tmp_path, name):
    model = WavelengthModel()
    baseline = deepcopy(model.java.study_node.step.properties)
    result = run_staged_parametric_sweep(
        model,
        name,
        [5.0, 5.002],
        ["A"],
        parameter_unit="um",
        study_name="std1",
        study_step_tag="wave",
        csv_path=str(tmp_path / "points.csv"),
        strict_wavelength_policy=POLICY,
    )
    assert result["success"] is True
    assert result["wavelength_validation"]["verified"] is True
    assert model.java.study_node.run_count == 2
    assert model.java.study_node.step.properties == baseline
    rows = read_csv(tmp_path / "points.csv")
    assert [float(row["evaluated_wl"]) for row in rows] == pytest.approx([5e-6, 5.002e-6])
    assert [float(row["evaluated_c_const_over_ewfd_freq"]) for row in rows] == pytest.approx(
        [5e-6, 5.002e-6]
    )
    assert all(item[2] is model.dataset_node for item in model.evaluations)
    assert sum(item[1] == "m" for item in model.evaluations) == 4


@pytest.mark.parametrize(
    "changes",
    [
        {"solved_override": 1e-6},
        {"evaluated_override": 1e-6},
        {"solved_override": float("nan")},
        {"evaluated_override": float("inf")},
        {"solved_override": -1.0},
    ],
)
def test_drift_and_invalid_controls_fail_and_restore(tmp_path, changes):
    model = WavelengthModel(**changes)
    baseline = deepcopy(model.java.study_node.step.properties)
    with pytest.raises(ValueError):
        run(model, tmp_path)
    assert model.java.study_node.step.properties == baseline
    rows = read_csv(tmp_path / "points.csv")
    assert len(rows) == 1
    assert rows[0]["status"] == "error"


def test_continue_on_error_never_claims_verified(tmp_path):
    result = run(WavelengthModel(solved_override=1e-6), tmp_path, continue_on_error=True)
    assert result["success"] is False
    assert result["n_failed"] == 2
    assert result["wavelength_validation"]["verified"] is False


@pytest.mark.parametrize(
    "changes",
    [
        {"parameter_unit": None},
        {"parameter_unit": "s"},
        {"study_name": None},
        {"study_step_tag": None},
        {"record_wavelength_controls": False},
        {"study_step_property": "freq"},
    ],
)
def test_bad_inputs_refuse_before_solve_or_files(tmp_path, changes):
    model = WavelengthModel()
    with pytest.raises(ValueError):
        run(model, tmp_path, **changes)
    assert model.java.study_node.run_count == 0
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("case", ["wrong_step", "stale_dataset"])
def test_step_and_dataset_are_bound_before_work(tmp_path, case):
    model = WavelengthModel(dataset_study="old" if case == "stale_dataset" else "std1")
    if case == "wrong_step":
        model.java.study_node.step.step_type = "Frequency"
    with pytest.raises(ValueError):
        run(model, tmp_path)
    assert not list(tmp_path.iterdir())
    assert model.java.study_node.run_count == 0


def test_partial_run_and_record_only_are_unverified(tmp_path):
    partial = run(WavelengthModel(), tmp_path, max_new_points=1)
    assert partial["success"] is True  # Execution succeeded, coverage is partial.
    assert partial["stopped_early"] is True
    assert partial["wavelength_validation"]["verified"] is False
    record = run(WavelengthModel(), tmp_path, strict_wavelength_policy=None)
    assert record["wavelength_validation"] == {"mode": "record_only", "verified": False}


def test_resume_rejects_changed_policy_and_tampered_controls(tmp_path):
    source = tmp_path / "source.mph"
    source.write_bytes(b"immutable source")
    model = WavelengthModel()
    assert run(model, tmp_path, source_model_path=str(source))["success"] is True
    before = model.java.study_node.run_count
    changed = dict(POLICY, relative_tolerance=1e-7)
    mismatch = run(
        model,
        tmp_path,
        source_model_path=str(source),
        resume_csv=True,
        strict_wavelength_policy=changed,
    )
    assert mismatch["success"] is False
    assert model.java.study_node.run_count == before
    path = tmp_path / "points.csv"
    rows = read_csv(path)
    rows[0]["evaluated_c_const_over_ewfd_freq"] = "1e-6"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    mismatch = run(model, tmp_path, source_model_path=str(source), resume_csv=True)
    assert mismatch["success"] is False
    assert "strict CSV" in mismatch["error"]
    assert model.java.study_node.run_count == before
    assert source.read_bytes() == b"immutable source"


@pytest.mark.parametrize(
    "policy",
    [
        dict(POLICY, relative_tolerance=True),
        dict(POLICY, absolute_tolerance_m=-1),
        dict(POLICY, relative_tolerance=float("inf")),
        dict(POLICY, dataset_tag="../old"),
        dict(POLICY, silent_fallback=True),
    ],
)
def test_policy_rejects_ambiguity(policy):
    with pytest.raises(ValueError):
        normalize_policy(policy)


def test_tolerance_is_explicit_and_unit_conversion_is_consistent():
    assert requested_metres("5000[nm]", None) == pytest.approx(requested_metres(5.0, "um"))
    verify_metres(5e-6, 5e-6, 5e-6 + 1e-15, POLICY)
    with pytest.raises(ValueError):
        verify_metres(5e-6, 5e-6, 5.002e-6, POLICY)


def test_public_job_contract_normalizer_worker_and_fingerprint_preserve_policy(tmp_path):
    from comsol_mcp.contracts.job_submission import job_submission_dict, validate_job_submission
    from comsol_mcp.jobs.manager import validate_staged_sweep_spec
    from comsol_mcp.jobs.worker import _runner_kwargs

    source = tmp_path / "source.mph"
    source.write_bytes(b"source")
    raw = dict(
        job_type="staged_sweep",
        source_model_path=str(source),
        parameter_name="wl",
        parameter_values=[5.0, 5.002],
        expressions=["A"],
        parameter_unit="um",
        study_name="std1",
        study_step_tag="wave",
        strict_wavelength_policy=POLICY,
    )
    transported = job_submission_dict(validate_job_submission(raw))
    spec = validate_staged_sweep_spec(transported)
    assert spec["strict_wavelength_policy"] == POLICY
    assert _runner_kwargs(spec, tmp_path)["strict_wavelength_policy"] == POLICY
    changed = validate_staged_sweep_spec(
        dict(raw, strict_wavelength_policy=dict(POLICY, relative_tolerance=1e-7))
    )
    assert changed["spec_fingerprint"] != spec["spec_fingerprint"]
    assert source.read_bytes() == b"source"


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"study_step_tag": None}, "study and step tags"),
        ({"parameter_name": "temperature"}, "wavelength parameter"),
        ({"record_wavelength_controls": False}, "recorded controls"),
    ],
)
def test_public_strict_contract_rejects_inconsistent_controls(changes, message):
    from comsol_mcp.contracts.job_submission import validate_job_submission

    raw = dict(
        job_type="staged_sweep",
        source_model_path="/immutable/source.mph",
        parameter_name="wl",
        parameter_values=[5.0, 5.002],
        expressions=["A"],
        parameter_unit="um",
        study_name="std1",
        study_step_tag="wave",
        strict_wavelength_policy=POLICY,
    )
    with pytest.raises(ValueError, match=message):
        validate_job_submission(dict(raw, **changes))


@pytest.mark.parametrize("tags", [[], ["dset1", "dset1"], ["other"]])
def test_dataset_tag_resolution_refuses_missing_or_ambiguous_nodes(tags):
    from comsol_mcp.adapter.wavelength_controls import evaluate_on_dataset

    class Model:
        def __truediv__(self, name):
            assert name == "datasets"
            return [SimpleNamespace(tag=lambda tag=tag: tag) for tag in tags]

        def evaluate(self, *args, **kwargs):
            pytest.fail("invalid dataset resolution must refuse before evaluation")

    with pytest.raises(ValueError, match="exactly one"):
        evaluate_on_dataset(Model(), ["wl"], "dset1")
