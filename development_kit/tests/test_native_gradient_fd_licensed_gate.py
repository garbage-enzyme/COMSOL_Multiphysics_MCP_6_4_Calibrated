"""Solver-free contracts for three-step finite-difference validation."""

import json
import os
import shutil
import uuid
from pathlib import Path

import pytest

from development_kit.scripts import native_gradient_fd_licensed_gate as gate
from development_kit.tests.test_native_adjoint_licensed_gate import _inputs


@pytest.fixture
def gate_root(tmp_path: Path):
    root = (
        Path("D:/mcp_tests") / f"f{uuid.uuid4().hex[:8]}" if os.name == "nt" else tmp_path / "a71fd"
    )
    root.mkdir(parents=True, exist_ok=False)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _args(root, source, manifest, audit, receipt):
    return gate._parser().parse_args(
        [
            "--test-root",
            str(root),
            "--source-model",
            str(source),
            "--manifest",
            str(manifest),
            "--tree-audit",
            str(audit),
            "--native-receipt",
            str(receipt),
            "--cores",
            "3",
            "--optimizer-method",
            "gcmma",
            "--max-solves",
            "20",
            "--max-iterations",
            "2",
            "--max-wall-time-seconds",
            "900",
            "--max-commit-fraction",
            "0.61",
            "--max-disk-bytes",
            "1048576",
            "--max-review-items",
            "9",
            "--mode",
            "full-vector",
            "--dry-run",
        ]
    )


def _native_receipt(source_hash):
    return {
        "schema_name": "comsol_mcp.native_gradient_licensed_gate",
        "success": True,
        "mode": "full-vector",
        "source_sha256": source_hash,
        "derivatives": [
            {"variable_id": "patch_length_x", "accepted_real": -1.0},
            {"variable_id": "patch_length_y", "accepted_real": 2.0},
        ],
    }


def test_fd_dry_run_freezes_three_steps_and_twelve_points(tmp_path, gate_root):
    source, manifest, audit = _inputs(tmp_path)
    native = tmp_path / "native.json"
    source_hash = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
    native.write_text(json.dumps(_native_receipt(source_hash)), encoding="utf-8")
    spec = gate._spec(_args(gate_root, source, manifest, audit, native))
    result = gate._dry_run(spec)
    assert result["relative_steps"] == [0.01, 0.003, 0.001]
    assert result["forward_point_count"] == 12
    assert result["solver_started"] is False
    assert result["paths_included"] is False


def test_fd_rejects_a_different_step_policy(tmp_path, gate_root):
    source, manifest, audit = _inputs(tmp_path)
    native = tmp_path / "native.json"
    source_hash = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
    native.write_text(json.dumps(_native_receipt(source_hash)), encoding="utf-8")
    args = _args(gate_root, source, manifest, audit, native)
    args.relative_steps = "0.02,0.003,0.001"
    with pytest.raises(ValueError, match="exactly 0.01"):
        gate._spec(args)


def test_fd_rejects_native_receipt_source_mismatch(tmp_path, gate_root):
    source, manifest, audit = _inputs(tmp_path)
    native = tmp_path / "native.json"
    native.write_text(json.dumps(_native_receipt("a" * 64)), encoding="utf-8")
    with pytest.raises(ValueError, match="exact successful full-vector source"):
        gate._spec(_args(gate_root, source, manifest, audit, native))


def test_fd_baselines_are_taken_from_si_support_values():
    support = {
        "variables": [
            {"variable_id": "patch_length_x", "baseline": 8.56e-7},
            {"variable_id": "patch_length_y", "baseline": 8.56e-7},
        ]
    }
    assert gate._baseline_values(support, ["patch_length_x", "patch_length_y"]) == {
        "patch_length_x": 8.56e-7,
        "patch_length_y": 8.56e-7,
    }


def test_forward_objective_accepts_mph_zero_dimensional_scalar(monkeypatch):
    class Model:
        def evaluate(self, expression, *, dataset, outer):
            assert expression == "comp1.ewfd.Torder_0_0"
            assert dataset == "dset1-wrapper"
            assert outer == 1
            return __import__("numpy").asarray(0.25)

    monkeypatch.setattr(gate, "_dataset_by_tag", lambda _model, _tag: "dset1-wrapper")
    assert gate._forward_objective(Model()) == 0.25


def test_baseline_values_reject_zero_and_nonfinite_baselines():
    support = {
        "variables": [
            {"variable_id": "a", "baseline": 100.0},
            {"variable_id": "b", "baseline": 0.0},
        ]
    }
    with pytest.raises(ValueError, match="finite and nonzero.*b"):
        gate._baseline_values(support, ["a", "b"])

    support["variables"][1]["baseline"] = float("nan")
    with pytest.raises(ValueError, match="finite and nonzero"):
        gate._baseline_values(support, ["a", "b"])


def test_baseline_values_accept_negative_and_report_exact_values():
    support = {
        "variables": [
            {"variable_id": "a", "baseline": 260.0},
            {"variable_id": "b", "baseline": -3.5},
        ]
    }
    assert gate._baseline_values(support, ["b", "a"]) == {"b": -3.5, "a": 260.0}


def test_relative_error_is_true_relative_even_for_small_gradient_magnitudes():
    # 5e-2 vs 1e-3 is a 50x mismatch: a unit floor would report 0.049 and
    # pass the 0.1 threshold; the true relative error must fail it.
    error = gate._relative_error(5e-2, 1e-3)
    assert error == pytest.approx(abs(5e-2 - 1e-3) / 5e-2)
    assert error > 0.1
    assert gate._relative_error(2.0, 2.0) == 0.0
    assert gate._relative_error(0.0, 0.0) == 0.0
