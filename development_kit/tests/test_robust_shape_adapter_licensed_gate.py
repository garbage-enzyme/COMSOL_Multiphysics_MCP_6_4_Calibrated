"""Solver-free tests for the robust shape adapter licensed gate."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from development_kit.scripts import robust_shape_adapter_licensed_gate as gate
from development_kit.tests.test_research_adapters import _audit, _manifest


@pytest.fixture
def gate_root(tmp_path: Path):
    root = (
        Path("D:/mcp_tests") / f"r{uuid.uuid4().hex[:8]}" if os.name == "nt" else tmp_path / "a72s3"
    )
    root.mkdir(parents=True, exist_ok=False)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _inputs(tmp_path: Path):
    source = tmp_path / "source.mph"
    source.write_bytes(b"fixture")
    manifest = _manifest()
    manifest["source_identity"]["source_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_path = tmp_path / "manifest.json"
    audit_path = tmp_path / "tree-audit.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    audit_path.write_text(json.dumps(_audit(manifest)), encoding="utf-8")
    return source, manifest_path, audit_path


def _args(root: Path, source: Path, manifest: Path, audit: Path):
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
            "3",
            "--max-elements-per-model",
            "300000",
            "--minimum-element-quality",
            "0.1",
        ]
    )


def test_dry_run_binds_only_caller_owned_limits_without_importing_mph(
    tmp_path, gate_root, monkeypatch
):
    source, manifest, audit = _inputs(tmp_path)
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 4)
    spec = gate._spec(_args(gate_root, source, manifest, audit))
    receipt = gate._dry_run(spec)
    assert receipt["solver_started"] is False
    assert receipt["requested_cores"] == 3
    assert receipt["max_elements_per_model"] == 300_000
    assert receipt["minimum_element_quality"] == 0.1
    assert receipt["paths_included"] is False


def test_shape_policy_records_minimum_gap_as_not_requested_for_s3_structural_gate(
    tmp_path, gate_root, monkeypatch
):
    source, manifest, audit = _inputs(tmp_path)
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 4)
    spec = gate._spec(_args(gate_root, source, manifest, audit))
    assert gate._shape_policy(spec)["minimum_gap"]["mode"] == "not_requested"


def test_runtime_failure_redacts_paths_and_still_clears_client(tmp_path, gate_root, monkeypatch):
    source, manifest, audit = _inputs(tmp_path)
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 4)
    spec = gate._spec(_args(gate_root, source, manifest, audit))
    cleared = []

    class FakeClient:
        def __init__(self, *, cores, version):
            assert (cores, version) == (3, "6.4")

        def load(self, _path):
            raise RuntimeError("private D:/fixture/source.mph detail")

        def clear(self):
            cleared.append(True)

    monkeypatch.setattr(gate, "_git_identity", lambda: {"revision": "a" * 40, "clean": True})
    monkeypatch.setitem(sys.modules, "mph", SimpleNamespace(Client=FakeClient))
    receipt, private = gate._run(spec)
    assert receipt["error"] == {"code": "robust_shape_adapter_failed", "type": "RuntimeError"}
    assert receipt["cleanup"] == {"client_clear": True, "source_unchanged": True}
    assert "D:/fixture/source.mph" not in json.dumps(receipt)
    assert "D:/fixture/source.mph" in private["error"]
    assert cleared == [True]
