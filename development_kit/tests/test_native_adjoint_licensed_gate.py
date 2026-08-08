"""Solver-free tests for the native adjoint licensed gate entry point."""

import hashlib
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from development_kit.scripts import native_adjoint_licensed_gate as gate
from development_kit.tests.test_research_adapters import _audit, _manifest


@pytest.fixture
def gate_root(tmp_path: Path):
    root = (
        Path("D:/mcp_tests") / f"n{uuid.uuid4().hex[:8]}"
        if os.name == "nt"
        else tmp_path / "a71gate"
    )
    root.mkdir(parents=True, exist_ok=False)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _args(root, source, manifest, audit, *, cores="3"):
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
            "--cores",
            cores,
            "--optimizer-method",
            "gcmma",
            "--max-solves",
            "7",
            "--max-iterations",
            "2",
            "--max-wall-time-seconds",
            "600",
            "--max-commit-fraction",
            "0.61",
            "--max-disk-bytes",
            "1048576",
            "--max-review-items",
            "9",
        ]
    )


def _inputs(tmp_path):
    source = tmp_path / "source.mph"
    source.write_bytes(b"fixture")
    manifest = _manifest()
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest["source_identity"]["source_sha256"] = source_hash
    audit = _audit(manifest)
    manifest_path = tmp_path / "manifest.json"
    audit_path = tmp_path / "tree-audit.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    return source, manifest_path, audit_path


def test_dry_run_binds_inputs_without_importing_mph(tmp_path, gate_root, monkeypatch):
    source, manifest, audit = _inputs(tmp_path)
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 4)
    args = _args(gate_root, source, manifest, audit)
    args.dry_run = True
    spec = gate._spec(args)
    receipt = gate._dry_run(spec)
    assert receipt["success"] is True
    assert receipt["solver_started"] is False
    assert receipt["requested_cores"] == 3
    assert receipt["optimizer_method"] == "gcmma"
    assert receipt["budget"] == {
        "cores": 3,
        "max_solves": 7,
        "max_iterations": 2,
        "max_wall_time_seconds": 600,
        "max_commit_fraction": 0.61,
        "max_disk_bytes": 1048576,
        "max_review_items": 9,
    }
    assert receipt["paths_included"] is False


def test_gate_rejects_undeclared_host_capacity(tmp_path, gate_root, monkeypatch):
    source, manifest, audit = _inputs(tmp_path)
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 2)
    args = _args(gate_root, source, manifest, audit)

    with pytest.raises(ValueError, match="live host capacity"):
        gate._spec(args)


def test_gate_rejects_invalid_caller_budget_before_solver_start(tmp_path, gate_root, monkeypatch):
    source, manifest, audit = _inputs(tmp_path)
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 4)
    args = _args(gate_root, source, manifest, audit)
    args.max_commit_fraction = 1.01

    with pytest.raises(ValueError, match="max_commit_fraction"):
        gate._spec(args)


def test_runtime_failure_retains_private_diagnostics_and_public_cleanup(
    tmp_path, gate_root, monkeypatch
):
    source, manifest, audit = _inputs(tmp_path)
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 4)
    spec = gate._spec(_args(gate_root, source, manifest, audit))
    cleared = []

    class FakeClient:
        def __init__(self, *, cores, version):
            assert (cores, version) == (3, "6.4")

        def load(self, _path):
            raise RuntimeError("private D:/fixture/model.mph detail")

        def clear(self):
            cleared.append(True)

    monkeypatch.setattr(gate, "_git_identity", lambda: {"revision": "a" * 40, "clean": True})
    monkeypatch.setitem(sys.modules, "mph", SimpleNamespace(Client=FakeClient))

    receipt, private = gate._run(spec)

    assert receipt["success"] is False
    assert receipt["error"] == {
        "code": "native_configuration_failed",
        "type": "RuntimeError",
    }
    assert receipt["cleanup"] == {"client_clear": True, "source_unchanged": True}
    assert "D:/fixture/model.mph" not in json.dumps(receipt)
    assert "D:/fixture/model.mph" in private["error"]
    assert cleared == [True]
