"""Solver-free contracts for deterministic directional validation."""

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

import pytest

from development_kit.scripts import native_gradient_directional_licensed_gate as gate
from development_kit.tests.test_native_adjoint_licensed_gate import _inputs


@pytest.fixture
def gate_root(tmp_path: Path):
    root = (
        Path("D:/mcp_tests") / f"d{uuid.uuid4().hex[:8]}"
        if os.name == "nt"
        else tmp_path / "a71direction"
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
            "2",
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


def _receipt(source):
    return {
        "schema_name": "comsol_mcp.native_gradient_licensed_gate",
        "success": True,
        "mode": "full-vector",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "derivatives": [
            {"variable_id": "patch_length_x", "accepted_real": -1.0},
            {"variable_id": "patch_length_y", "accepted_real": 2.0},
        ],
    }


def test_direction_is_deterministic_normalized_and_dry_run_is_solver_free(tmp_path, gate_root):
    source, manifest, audit = _inputs(tmp_path)
    native = tmp_path / "native.json"
    native.write_text(json.dumps(_receipt(source)), encoding="utf-8")
    spec = gate._spec(_args(gate_root, source, manifest, audit, native))
    result = gate._dry_run(spec)
    assert result["direction"] == gate._direction(71004, 2)
    assert sum(item * item for item in result["direction"]) == pytest.approx(1.0)
    assert result["forward_point_count"] == 2
    assert result["solver_started"] is False
    assert result["paths_included"] is False


def test_directional_step_is_bounded(tmp_path, gate_root):
    source, manifest, audit = _inputs(tmp_path)
    native = tmp_path / "native.json"
    native.write_text(json.dumps(_receipt(source)), encoding="utf-8")
    args = _args(gate_root, source, manifest, audit, native)
    args.direction_relative_step = 0.02
    with pytest.raises(ValueError, match="direction relative step"):
        gate._spec(args)


def test_directional_reference_scale_is_a_nonzero_positive_magnitude():
    # A raw minimum could be zero (zero step, zero denominator) or negative
    # (swapped plus/minus labels); the scale must come from magnitudes.
    baselines = {"a": 260.0, "b": -3.5}
    assert gate._directional_reference_scale(baselines) == pytest.approx(3.5)
    assert gate._directional_reference_scale({"a": -1.0}) == 1.0


def test_directional_relative_error_rejects_small_magnitude_divergence():
    # observed=1e-3 against predicted=5e-2 is a 50x mismatch; the old unit
    # floor reported ~0.049 and passed the 0.1 check.
    error = gate._fd._relative_error(5e-2, 1e-3)
    assert error > 0.1
    assert gate._fd._relative_error(1.0, 1.0) == 0.0
