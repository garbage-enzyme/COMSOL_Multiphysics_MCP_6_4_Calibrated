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
    ownership_events = []

    class FakeOwnership:
        def acquire(self, **kwargs):
            ownership_events.append(("acquire", kwargs))
            return {"success": True, "acquired": True}

        def heartbeat(self, **kwargs):
            ownership_events.append(("heartbeat", kwargs))
            return True

        def release(self):
            ownership_events.append(("release", {}))
            return {"success": True, "released": True}

    class FakeClient:
        def __init__(self, *, cores, version):
            assert (cores, version) == (3, "6.4")

        def load(self, _path):
            raise RuntimeError("private D:/fixture/source.mph detail")

        def clear(self):
            cleared.append(True)

    monkeypatch.setattr(gate, "_git_identity", lambda: {"revision": "a" * 40, "clean": True})
    monkeypatch.setattr(gate, "SolverOwnership", FakeOwnership)
    monkeypatch.setitem(sys.modules, "mph", SimpleNamespace(Client=FakeClient))
    receipt, private = gate._run(spec)
    assert receipt["error"] == {"code": "robust_shape_adapter_failed", "type": "RuntimeError"}
    assert receipt["cleanup"] == {
        "client_clear": True,
        "lease_released": True,
        "source_unchanged": True,
    }
    assert "D:/fixture/source.mph" not in json.dumps(receipt)
    assert "D:/fixture/source.mph" in private["error"]
    assert cleared == [True]
    assert ownership_events == [
        ("acquire", {"mode": "alpha7.2_s3_licensed_gate", "model_path": str(source)}),
        (
            "heartbeat",
            {"model_path": str(source), "refresh_server_processes": True},
        ),
        ("release", {}),
    ]


def test_pre_try_failure_still_writes_a_terminal_receipt(tmp_path, gate_root, monkeypatch):
    # A failing git identity happens before the old try block and previously
    # bypassed receipt persistence entirely.
    source, manifest, audit = _inputs(tmp_path)
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 4)
    spec = gate._spec(_args(gate_root, source, manifest, audit))

    def broken_git():
        raise RuntimeError("git executable is unavailable")

    class RefusingOwnership:
        def acquire(self, **_kwargs):
            raise AssertionError("ownership must not be acquired after pre-try failure")

        def release(self):
            raise AssertionError("foreign lease must not be released")

    monkeypatch.setattr(gate, "_git_identity", broken_git)
    monkeypatch.setattr(gate, "SolverOwnership", RefusingOwnership)
    receipt, private = gate._run(spec)
    assert receipt["success"] is False
    assert receipt["error"] == {
        "code": "robust_shape_adapter_failed",
        "type": "RuntimeError",
    }
    assert "git executable is unavailable" in private["error"]
    assert receipt["cleanup"] == {
        "client_clear": False,
        "lease_released": True,
        "source_unchanged": False,
    }


def test_raising_ownership_release_cannot_mask_the_original_failure(
    tmp_path, gate_root, monkeypatch
):
    source, manifest, audit = _inputs(tmp_path)
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 4)
    spec = gate._spec(_args(gate_root, source, manifest, audit))
    cleared = []

    class ExplodingReleaseOwnership:
        def acquire(self, **_kwargs):
            return {"success": True, "acquired": True}

        def heartbeat(self, **_kwargs):
            return True

        def release(self):
            raise RuntimeError("release transport failed")

    class FakeClient:
        def __init__(self, *, cores, version):
            pass

        def load(self, _path):
            raise RuntimeError("original load failure")

        def clear(self):
            cleared.append(True)

    monkeypatch.setattr(gate, "_git_identity", lambda: {"revision": "a" * 40, "clean": True})
    monkeypatch.setattr(gate, "SolverOwnership", ExplodingReleaseOwnership)
    monkeypatch.setitem(sys.modules, "mph", SimpleNamespace(Client=FakeClient))
    receipt, private = gate._run(spec)
    assert receipt["error"] == {"code": "robust_shape_adapter_failed", "type": "RuntimeError"}
    assert "original load failure" in private["error"]
    assert cleared == [True]
    assert receipt["cleanup"]["lease_released"] is False
    assert "RuntimeError: release transport failed" in private["lease_cleanup_error"]
    assert receipt["success"] is False


def test_snapshot_copy_is_independent_of_later_backend_mutation():
    class AliasingBackend:
        def __init__(self):
            self.state = {"physics": ["dg_a71"], "variables": {"x": [1.0, 2.0]}}

        def snapshot(self):
            return self.state

        def restore(self, _snapshot):
            self.state["variables"]["x"][0] = 99.0

    backend = AliasingBackend()
    baseline = gate._snapshot_copy(backend)
    backend.restore(baseline)
    # The shallow dict() copy used before would alias the nested list and the
    # mutated value would compare equal to its own baseline.
    assert backend.snapshot() != baseline
    assert baseline["variables"]["x"] == [1.0, 2.0]


def test_runtime_refuses_client_start_when_solver_lease_is_unavailable(
    tmp_path, gate_root, monkeypatch
):
    source, manifest, audit = _inputs(tmp_path)
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 4)
    spec = gate._spec(_args(gate_root, source, manifest, audit))

    class RefusingOwnership:
        def acquire(self, **_kwargs):
            return {"success": False, "acquired": False}

        def release(self):
            raise AssertionError("foreign lease must not be released")

    class ForbiddenClient:
        def __init__(self, **_kwargs):
            raise AssertionError("COMSOL client must not start without ownership")

    monkeypatch.setattr(gate, "_git_identity", lambda: {"revision": "a" * 40, "clean": True})
    monkeypatch.setattr(gate, "SolverOwnership", RefusingOwnership)
    monkeypatch.setitem(sys.modules, "mph", SimpleNamespace(Client=ForbiddenClient))
    receipt, _private = gate._run(spec)
    assert receipt["error"] == {"code": "robust_shape_adapter_failed", "type": "RuntimeError"}
    assert receipt["cleanup"]["lease_released"] is True
