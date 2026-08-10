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
        Path("D:/mcp_tests") / f"f{uuid.uuid4().hex[:8]}"
        if os.name == "nt"
        else tmp_path / "a71fd"
    )
    root.mkdir(parents=True, exist_ok=False)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _args(root, source, manifest, audit, receipt):
    return gate._parser().parse_args(
        [
            "--test-root", str(root),
            "--source-model", str(source),
            "--manifest", str(manifest),
            "--tree-audit", str(audit),
            "--native-receipt", str(receipt),
            "--cores", "3",
            "--optimizer-method", "gcmma",
            "--max-solves", "20",
            "--max-iterations", "2",
            "--max-wall-time-seconds", "900",
            "--max-commit-fraction", "0.61",
            "--max-disk-bytes", "1048576",
            "--max-review-items", "9",
            "--mode", "full-vector",
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
