"""Regression tests for licensed-gate evidence, without starting COMSOL."""

from __future__ import annotations

import importlib.util
import json
import math
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rows():
    return [{"a1": float(i), "a2": float(i + 1), "qoi": float(i * 2)} for i in range(48)]


def test_frozen_split_has_exact_disjoint_membership_before_training(tmp_path):
    oracle = load_script("surrogate_gate_oracles")
    split = oracle.frozen_split(rows())
    ids = split["row_ids"]
    assert ids["test"] == [34, 11, 18, 23, 19, 26, 33]
    assert [len(ids[k]) for k in ("train", "validation", "test")] == [34, 7, 7]
    assert set(ids["train"]).isdisjoint(ids["validation"])
    assert set(ids["train"]).isdisjoint(ids["test"])
    assert set(ids["validation"]).isdisjoint(ids["test"])
    assert sorted(sum(ids.values(), [])) == list(range(48))
    path = tmp_path / "train.csv"
    oracle.write_rows(path, split["rows"]["train"])
    written = [[float(v) for v in line.split(",")] for line in path.read_text().splitlines()]
    assert [int(row[0]) for row in written] == ids["train"]
    assert not any(int(row[0]) in ids["test"] for row in written)
    changed = rows()
    changed[0]["qoi"] += 1
    assert oracle.frozen_split(changed)["split_sha256"] != split["split_sha256"]


@pytest.mark.parametrize("count", [0, 47, 49])
def test_frozen_split_rejects_changed_fixture_size(count):
    oracle = load_script("surrogate_gate_oracles")
    with pytest.raises(ValueError, match="exactly 48"):
        oracle.frozen_split([rows()[0]] * count)


@pytest.mark.parametrize("version", ["1.3.1", "1.3.2", "1.4.1", "", None])
def test_mph14_runner_rejects_wrong_version_without_running_pytest(tmp_path, monkeypatch, version):
    runner = load_script("mph14_lane_probe")
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    module = overlay / "__init__.py"
    module.write_text("fixture")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "version": version,
                    "module": str(module),
                    "interpreter": sys.executable,
                }
            ),
            "",
        )

    monkeypatch.setattr(runner.subprocess, "run", run)
    output = tmp_path / "receipt.json"
    assert runner.main(["--overlay", str(overlay), "--output", str(output)]) == 1
    assert len(calls) == 1
    assert json.loads(output.read_text())["success"] is False


@pytest.mark.parametrize(
    "bad", ["outside_overlay", "different_interpreter", "missing_overlay", "not_json", "json_array"]
)
def test_mph14_runner_rejects_ambiguous_identity(tmp_path, monkeypatch, bad):
    runner = load_script("mph14_lane_probe")
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    module = overlay / "mph.py"
    module.write_text("fixture")
    identity = {"version": "1.4.0", "module": str(module), "interpreter": sys.executable}
    if bad == "outside_overlay":
        identity["module"] = str(tmp_path / "mph.py")
    if bad == "different_interpreter":
        identity["interpreter"] = str(tmp_path / "python.exe")
    if bad == "missing_overlay":
        overlay = tmp_path / "absent"
    stdout = (
        "invalid" if bad == "not_json" else "[]" if bad == "json_array" else json.dumps(identity)
    )
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(runner.subprocess, "run", run)
    output = tmp_path / "receipt.json"
    assert runner.main(["--overlay", str(overlay), "--output", str(output)]) == 1
    assert len(calls) == 1


@pytest.mark.parametrize("returncode", [0, 1])
def test_mph14_runner_binds_real_test_process_and_propagates_failure(
    tmp_path, monkeypatch, returncode
):
    runner = load_script("mph14_lane_probe")
    module = tmp_path / "mph.py"
    module.write_text("fixture")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, "a" * 40, "")
        if "pytest.main" in command[2]:
            assert "mph.__version__ == '1.4.0'" in command[2]
            assert "is_relative_to" in command[2]
            assert kwargs["env"]["PYTHONPATH"] == str(tmp_path)
            return subprocess.CompletedProcess(command, returncode, "test summary", "")
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "version": "1.4.0",
                    "module": str(module),
                    "interpreter": sys.executable,
                }
            ),
            "",
        )

    monkeypatch.setattr(runner.subprocess, "run", run)
    output = tmp_path / "receipt.json"
    assert runner.main(["--overlay", str(tmp_path), "--output", str(output)]) == returncode
    report = json.loads(output.read_text())
    assert report["success"] is (returncode == 0)
    assert len(report["mph_module_sha256"]) == 64
    assert len(report["suite_sha256"]) == 7
    assert len(calls) == 3


@pytest.mark.parametrize(
    "name", ["surrogate_training_licensed_gate", "surrogate_export_licensed_gate"]
)
def test_worker_dispatch_preserves_requested_mph_lane(name, tmp_path, monkeypatch):
    gate = load_script(name)
    captured = []
    monkeypatch.setattr(gate, "_run_worker", lambda *args: captured.append(args) or 0)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            name,
            "--worker",
            "--confirm",
            "RUN_REAL_COMSOL",
            "--worker-output",
            str(tmp_path / "receipt.json"),
            "--cores",
            "1",
            "--mph-lane",
            "1.4.0",
        ],
    )
    assert gate.main() == 0
    assert captured[0][-1] == "1.4.0"


def test_table_readback_corruption_fails_before_training(monkeypatch):
    oracle = load_script("surrogate_gate_oracles")
    fake_jpype = SimpleNamespace(JArray=lambda *_: lambda v: v, JString=str, JDouble=float)
    monkeypatch.setitem(sys.modules, "jpype", fake_jpype)
    table = SimpleNamespace(
        setColumnHeaders=lambda v: None, setTableData=lambda v: None, getReal=lambda: [[0, 0, 0]]
    )
    java = SimpleNamespace(
        result=lambda: SimpleNamespace(table=lambda: SimpleNamespace(create=lambda *_: table))
    )
    backend = SimpleNamespace(
        write_scalar=lambda *_: pytest.fail("corruption must reject before binding")
    )
    with pytest.raises(RuntimeError, match="table readback differs"):
        oracle.bind_split_tables(
            SimpleNamespace(java=java), backend, object(), oracle.frozen_split(rows())
        )


def test_holdout_rmse_uses_actual_predictions_and_exact_sample_order():
    oracle = load_script("surrogate_gate_oracles")
    split = oracle.frozen_split(rows())
    expected = [[row["qoi"]] for row in split["rows"]["test"]]
    predictions = [[row[0] + 2] for row in expected]
    score = oracle.score_holdout(split, predictions)
    assert score["metrics"]["rmse"] == 2.0  # MSE is 4, not the reported RMSE.
    assert score["targets"] == [[68], [22], [36], [46], [38], [52], [66]]
    reversed_score = oracle.score_holdout(split, list(reversed(expected)))
    independent = math.sqrt(
        sum((a[0] - b[0]) ** 2 for a, b in zip(expected, reversed(expected))) / 7
    )
    assert reversed_score["metrics"]["rmse"] == pytest.approx(independent)
    assert reversed_score["metrics"]["rmse"] > 0


def test_holdout_refuses_tampered_split_missing_or_nonfinite_predictions():
    oracle = load_script("surrogate_gate_oracles")
    split = oracle.frozen_split(rows())
    with pytest.raises(ValueError):
        oracle.score_holdout(split, [[0.0]])
    with pytest.raises(ValueError):
        oracle.score_holdout(split, [[float("nan")]] * 7)
    split["row_ids"]["test"][0] = split["row_ids"]["train"][0]
    with pytest.raises(ValueError, match="identity mismatch"):
        oracle.score_holdout(split, [[0.0]] * 7)
