"""Unit tests for durable workflow execution without a COMSOL client."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import pytest

from src.tools.workflow import (
    _model_identity,
    _csv_value,
    _scalarize,
    _sweep_point_id,
    run_mesh_convergence,
    run_staged_parametric_sweep,
)


class JavaStringLike:
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return self.value


class FakeEntityList:
    def __init__(self, entities):
        self.entities = entities

    def tags(self):
        return [JavaStringLike(tag) for tag in self.entities]

    def get(self, tag):
        return self.entities[tag]


class FakeStep:
    def __init__(self):
        self.properties = {}

    def set(self, key, value):
        self.properties[key] = value


class FakeStudy:
    def __init__(self, java):
        self.java = java
        self.step = FakeStep()
        self.run_count = 0

    def label(self):
        return "Study 1"

    def feature(self, _tag):
        return self.step

    def run(self):
        self.run_count += 1
        value = self.java.parameters.values.get("wl")
        remaining = self.java.failures.get(value, 0)
        if remaining:
            self.java.failures[value] = remaining - 1
            raise RuntimeError(f"planned failure for {value}")


class FakeParameters:
    def __init__(self):
        self.values = {}

    def set(self, key, value):
        self.values[key] = value


class FakeSize:
    def __init__(self):
        self.properties = {}

    def set(self, key, value):
        self.properties[key] = value


class FakeMesh:
    def __init__(self):
        self.size = FakeSize()
        self.run_count = 0

    def feature(self, _tag):
        return self.size

    def run(self):
        self.run_count += 1

    def getNumElem(self):
        return 1000 + self.run_count

    def getNumVertex(self):
        return 500 + self.run_count


class FakeComponent:
    def __init__(self):
        self.mesh_node = FakeMesh()

    def mesh(self, _tag):
        return self.mesh_node


class FakeJava:
    def __init__(self, failures=None):
        self.parameters = FakeParameters()
        self.failures = dict(failures or {})
        self.study_node = FakeStudy(self)
        self.studies = FakeEntityList({"std1": self.study_node})
        self.component_node = FakeComponent()
        self.saved = []

    def param(self):
        return self.parameters

    def study(self, tag=None):
        return self.studies if tag is None else self.studies.get(tag)

    def component(self, _tag):
        return self.component_node

    def save(self, path, save_copy=False):
        self.saved.append((path, save_copy))


class FakeModel:
    def __init__(self, failures=None):
        self.java = FakeJava(failures)

    def name(self):
        return "fake"

    def evaluate(self, expressions):
        raw = self.java.parameters.values.get("wl", "0")
        value = float(str(raw).split("[", 1)[0])
        arrays = [np.array([value + index]) for index, _ in enumerate(expressions)]
        return arrays[0] if len(arrays) == 1 else arrays


def read_csv(path: Path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_staged_sweep_retries_and_checkpoints(tmp_path):
    csv_path = tmp_path / "sweep.csv"
    checkpoint = tmp_path / "checkpoint.mph"
    model = FakeModel(failures={"1[m]": 1})

    result = run_staged_parametric_sweep(
        model,
        "wl",
        [1, 2],
        ["A"],
        parameter_unit="m",
        study_step_tag="wave",
        study_step_unit="m",
        csv_path=str(csv_path),
        max_retries=1,
        checkpoint_model_path=str(checkpoint),
    )

    assert result["success"] is True
    assert result["resolved_study_tag"] == "std1"
    assert type(result["resolved_study_tag"]) is str
    assert result["n_points"] == 2
    assert result["tail_rows"][0]["attempt"] == 2
    assert [row["status"] for row in read_csv(csv_path)] == ["ok", "ok"]
    assert result["manifest_path"] == str(csv_path) + ".manifest.json"
    assert Path(result["manifest_path"]).is_file()
    assert "rows" not in result
    assert len(model.java.saved) == 2
    assert all(saved[1] is False for saved in model.java.saved)


def test_staged_sweep_can_checkpoint_through_save_copy_overload(tmp_path):
    checkpoint = tmp_path / "attached-checkpoint.mph"
    model = FakeModel()

    result = run_staged_parametric_sweep(
        model,
        "wl",
        [1, 2],
        ["A"],
        parameter_unit="m",
        checkpoint_model_path=str(checkpoint),
        save_model_copy=True,
    )

    assert result["success"] is True
    assert len(model.java.saved) == 2
    assert all(saved[1] is True for saved in model.java.saved)


def test_staged_sweep_resumes_legacy_csv(tmp_path):
    csv_path = tmp_path / "legacy.csv"
    csv_path.write_text(
        "wl,parameter_value,solve_sec,A\n1,1[m],0.1,1.0\n",
        encoding="utf-8",
    )
    model = FakeModel()

    result = run_staged_parametric_sweep(
        model,
        "wl",
        [1, 2],
        ["A"],
        parameter_unit="m",
        csv_path=str(csv_path),
        resume_csv=True,
        allow_legacy_resume=True,
    )

    rows = read_csv(csv_path)
    assert result["n_skipped"] == 0
    assert result["n_invalid_existing"] == 1
    assert result["n_points"] == 2
    assert [row["parameter_value"] for row in rows] == ["1[m]", "1[m]", "2[m]"]
    assert [row["status"] for row in rows] == ["legacy_unverified", "ok", "ok"]


def test_staged_sweep_records_error_and_continues(tmp_path):
    csv_path = tmp_path / "errors.csv"
    model = FakeModel(failures={"2[m]": 1})

    result = run_staged_parametric_sweep(
        model,
        "wl",
        [1, 2, 3],
        ["A"],
        parameter_unit="m",
        csv_path=str(csv_path),
        continue_on_error=True,
    )

    assert result["success"] is False
    assert result["n_points"] == 2
    assert result["n_failed"] == 1
    assert [row["status"] for row in read_csv(csv_path)] == [
        "ok",
        "error",
        "ok",
    ]


def test_staged_sweep_manifest_rejects_changed_spec(tmp_path):
    csv_path = tmp_path / "sweep.csv"
    model = FakeModel()
    first = run_staged_parametric_sweep(
        model,
        "wl",
        [1, 2],
        ["A"],
        parameter_unit="m",
        csv_path=str(csv_path),
    )
    resumed = run_staged_parametric_sweep(
        model,
        "wl",
        [1, 2, 3],
        ["A"],
        parameter_unit="m",
        csv_path=str(csv_path),
        resume_csv=True,
    )

    assert first["success"] is True
    assert resumed["success"] is False
    assert resumed["error_type"] == "ValueError"
    assert "manifest mismatch" in resumed["error"].lower()
    assert model.java.study_node.run_count == 2


def test_staged_sweep_smoke_limit_keeps_full_manifest_and_resumes(tmp_path):
    csv_path = tmp_path / "smoke.csv"
    durable_rows = []
    first_model = FakeModel()
    smoke = run_staged_parametric_sweep(
        first_model,
        "wl",
        [1, 2, 3],
        ["A"],
        parameter_unit="m",
        csv_path=str(csv_path),
        max_new_points=1,
        on_durable_row=durable_rows.append,
    )
    resumed_model = FakeModel()
    broad = run_staged_parametric_sweep(
        resumed_model,
        "wl",
        [1, 2, 3],
        ["A"],
        parameter_unit="m",
        csv_path=str(csv_path),
        resume_csv=True,
    )

    manifest = json.loads(Path(smoke["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["spec"]["requested_values"] == ["1[m]", "2[m]", "3[m]"]
    assert smoke["stopped_early"] is True
    assert smoke["stop_reason"] == "max_new_points"
    assert smoke["n_processed"] == 1
    assert len(durable_rows) == 1
    assert broad["n_skipped"] == 1
    assert broad["n_points"] == 2
    assert [row["parameter_value"] for row in read_csv(csv_path)] == ["1[m]", "2[m]", "3[m]"]


def test_staged_sweep_cooperative_stop_is_checked_between_points(tmp_path):
    csv_path = tmp_path / "control.csv"
    model = FakeModel()
    durable_rows = []

    result = run_staged_parametric_sweep(
        model,
        "wl",
        [1, 2, 3],
        ["A"],
        parameter_unit="m",
        csv_path=str(csv_path),
        should_stop=lambda: bool(durable_rows),
        on_durable_row=durable_rows.append,
    )

    assert result["stopped_early"] is True
    assert result["stop_reason"] == "control_request"
    assert result["n_processed"] == 1
    assert model.java.study_node.run_count == 1


def test_staged_sweep_hook_actions_are_bounded_and_stop_before_solve(tmp_path):
    csv_path = tmp_path / "hook-stop.csv"
    model = FakeModel()
    contexts = []

    def before_point(context):
        contexts.append(context)
        actions = {
            "1[m]": "skip_completed",
            "2[m]": "checkpoint_no_start",
        }
        return {"action": actions[context["parameter_value"]]}

    result = run_staged_parametric_sweep(
        model,
        "wl",
        [1, 2, 3],
        ["A"],
        parameter_unit="m",
        csv_path=str(csv_path),
        before_point_hook=before_point,
    )

    assert result["stopped_early"] is True
    assert result["stop_reason"] == "before_point_checkpoint_no_start"
    assert result["n_skipped"] == 1
    assert result["n_hook_skipped"] == 1
    assert result["n_processed"] == 0
    assert result["hook_action_counts"]["before_point"]["skip_completed"] == 1
    assert result["hook_action_counts"]["before_point"]["checkpoint_no_start"] == 1
    assert model.java.study_node.run_count == 0
    assert [context["stage"] for context in contexts] == ["pre_solve", "pre_solve"]
    assert contexts[0]["point_id"] == _sweep_point_id("wl", "1[m]")


def test_invalid_after_durable_hook_does_not_retry_or_append_error_row(tmp_path):
    csv_path = tmp_path / "invalid-after-hook.csv"
    model = FakeModel()

    with pytest.raises(ValueError, match="unsupported action"):
        run_staged_parametric_sweep(
            model,
            "wl",
            [1],
            ["A"],
            parameter_unit="m",
            csv_path=str(csv_path),
            max_retries=2,
            after_durable_row_hook=lambda _context: {"action": "launch_anyway"},
        )

    assert model.java.study_node.run_count == 1
    assert [row["status"] for row in read_csv(csv_path)] == ["ok"]


@pytest.mark.parametrize(
    ("hook_result", "message"),
    [
        (None, "action object"),
        ({"action": "start_point", "unbounded": []}, "unsupported fields"),
        ({"action": "start_point", "stage": "post_solve"}, "mismatched stage"),
        ({"action": "start_point", "point_id": "point:wrong"}, "mismatched point_id"),
        ({"action": "start_point", "start_authorized": False}, "inconsistent start_authorized"),
        ({"action": "start_point", "journal_entries_appended": 3}, "out of bounds"),
        ({"action": "start_point", "next_attempt_sequence": 4097}, "out of bounds"),
        ({"action": "start_point", "latest_entry_sha256": "not-a-hash"}, "is invalid"),
    ],
)
def test_staged_sweep_hook_rejects_unbounded_or_mismatched_results(
    tmp_path,
    hook_result,
    message,
):
    model = FakeModel()

    with pytest.raises(ValueError, match=message):
        run_staged_parametric_sweep(
            model,
            "wl",
            [1],
            ["A"],
            parameter_unit="m",
            csv_path=str(tmp_path / "bounded-hook.csv"),
            before_point_hook=lambda _context: hook_result,
        )

    assert model.java.study_node.run_count == 0


def test_error_row_is_retried_but_valid_row_is_skipped(tmp_path):
    csv_path = tmp_path / "resume.csv"
    first_model = FakeModel(failures={"2[m]": 1})
    first = run_staged_parametric_sweep(
        first_model,
        "wl",
        [1, 2],
        ["A"],
        parameter_unit="m",
        csv_path=str(csv_path),
        continue_on_error=True,
    )
    second_model = FakeModel()
    resumed = run_staged_parametric_sweep(
        second_model,
        "wl",
        [1, 2],
        ["A"],
        parameter_unit="m",
        csv_path=str(csv_path),
        resume_csv=True,
    )

    assert first["success"] is False
    assert resumed["success"] is True
    assert resumed["n_skipped"] == 1
    assert resumed["n_points"] == 1
    assert resumed["n_invalid_existing"] == 1
    assert second_model.java.study_node.run_count == 1


def test_manifest_fingerprints_source_and_rows_record_wavelength_controls(tmp_path):
    csv_path = tmp_path / "provenance.csv"
    source = tmp_path / "baseline.mph"
    source.write_bytes(b"immutable fake model")
    model = FakeModel()

    result = run_staged_parametric_sweep(
        model,
        "wl",
        [1],
        ["A"],
        parameter_unit="m",
        csv_path=str(csv_path),
        source_model_path=str(source),
        physical_bounds={"A": [0, 1]},
        response_tail=1,
    )

    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    row = read_csv(csv_path)[0]
    assert manifest["spec"]["model"]["source_path"] == str(source.resolve())
    assert len(manifest["spec"]["model"]["source_sha256"]) == 64
    assert row["source_model_sha256"] == manifest["spec"]["model"]["source_sha256"]
    assert row["requested_wavelength"] == "1[m]"
    assert row["evaluated_wl"] == "2.0"
    assert row["evaluated_c_const_over_ewfd_freq"] == "3.0"
    assert result["last_point"] == result["tail_rows"][-1]


def test_source_identity_ignores_mutable_runtime_model_name(tmp_path):
    source = tmp_path / "baseline.mph"
    source.write_bytes(b"immutable fake model")
    model = FakeModel()

    first = _model_identity(model, str(source))
    model._name = "checkpoint_copy"
    resumed = _model_identity(model, str(source))

    assert first == resumed
    assert first["model_name"] is None


def test_resume_retries_nonfinite_row_instead_of_skipping(tmp_path):
    csv_path = tmp_path / "nonfinite.csv"
    first_model = FakeModel()
    run_staged_parametric_sweep(
        first_model,
        "wl",
        [1, 2],
        ["A"],
        parameter_unit="m",
        csv_path=str(csv_path),
    )
    rows = read_csv(csv_path)
    rows[0]["A"] = "nan"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    resumed_model = FakeModel()
    resumed = run_staged_parametric_sweep(
        resumed_model,
        "wl",
        [1, 2],
        ["A"],
        parameter_unit="m",
        csv_path=str(csv_path),
        resume_csv=True,
    )

    assert resumed["n_invalid_existing"] == 1
    assert resumed["n_skipped"] == 1
    assert resumed["n_points"] == 1
    assert resumed_model.java.study_node.run_count == 1


def test_source_baseline_cannot_be_used_as_checkpoint(tmp_path):
    source = tmp_path / "baseline.mph"
    source.write_bytes(b"keep me")
    result = run_staged_parametric_sweep(
        FakeModel(),
        "wl",
        [1],
        ["A"],
        source_model_path=str(source),
        checkpoint_model_path=str(source),
    )

    assert result["success"] is False
    assert "must not overwrite" in result["error"]
    assert source.read_bytes() == b"keep me"


def test_out_of_bounds_result_is_journaled_as_error(tmp_path):
    csv_path = tmp_path / "bounds.csv"
    result = run_staged_parametric_sweep(
        FakeModel(),
        "wl",
        [2],
        ["A"],
        parameter_unit="m",
        csv_path=str(csv_path),
        physical_bounds={"A": [0, 1]},
        continue_on_error=True,
    )

    row = read_csv(csv_path)[0]
    assert result["success"] is False
    assert result["n_failed"] == 1
    assert row["status"] == "error"
    assert row["error_type"] == "ValueError"
    assert "outside physical bounds" in row["error"]


def test_mesh_convergence_resumes_completed_levels(tmp_path):
    csv_path = tmp_path / "mesh.csv"
    model = FakeModel()
    first = run_mesh_convergence(
        model,
        [{"name": "coarse", "properties": {"hmax": "0.1"}}],
        ["A"],
        csv_path=str(csv_path),
    )
    resumed = run_mesh_convergence(
        model,
        [
            {"name": "coarse", "properties": {"hmax": "0.1"}},
            {"name": "fine", "properties": {"hmax": "0.05"}},
        ],
        ["A"],
        csv_path=str(csv_path),
        resume_csv=True,
    )

    assert first["success"] is True
    assert first["resolved_study_tag"] == "std1"
    assert type(first["resolved_study_tag"]) is str
    assert resumed["n_skipped"] == 1
    assert resumed["n_levels"] == 1
    assert [row["level"] for row in read_csv(csv_path)] == ["coarse", "fine"]


def test_complex_values_are_json_safe_and_csv_serializable():
    value = _scalarize(np.array([1.5 - 0.25j]))

    assert value == {"real": 1.5, "imag": -0.25}
    assert _csv_value(value) == "1.5+-0.25i"
