"""Solver-free contracts for the native gradient licensed runner."""

import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from development_kit.scripts import native_gradient_directional_licensed_gate as directional_gate
from development_kit.scripts import native_gradient_fd_licensed_gate as fd_gate
from development_kit.scripts import native_gradient_licensed_gate as gate
from development_kit.tests.test_native_adjoint_licensed_gate import (
    _args as structural_args,
)
from development_kit.tests.test_native_adjoint_licensed_gate import (
    _inputs,
)


@pytest.fixture
def gate_root(tmp_path: Path):
    root = (
        Path("D:/mcp_tests") / f"g{uuid.uuid4().hex[:8]}"
        if os.name == "nt"
        else tmp_path / "a71gradient"
    )
    root.mkdir(parents=True, exist_ok=False)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _args(root, source, manifest, audit, *, mode="full-vector"):
    args = structural_args(root, source, manifest, audit)
    args.mode = mode
    return args


@pytest.mark.parametrize(
    ("mode", "variables"),
    [
        ("one-variable", ["patch_length_x"]),
        ("full-vector", ["patch_length_x", "patch_length_y"]),
    ],
)
def test_dry_run_binds_native_identity_without_importing_mph(
    tmp_path,
    gate_root,
    monkeypatch,
    mode,
    variables,
):
    source, manifest, audit = _inputs(tmp_path)
    monkeypatch.setattr(gate._structural.os, "cpu_count", lambda: 4)
    sys.modules.pop("mph", None)
    spec = gate._spec(_args(gate_root, source, manifest, audit, mode=mode))

    result = gate._dry_run(spec)

    assert result["success"] is True
    assert result["selected_variables"] == variables
    assert result["objective_expression"] == "comp1.ewfd.Torder_0_0"
    assert result["wavelength_expression"] == "1.717657785e-6[m]"
    assert result["expected_solutions"] == ["sol1", "sol2", "sol3"]
    assert result["expected_derivative_dataset"] == {
        "dataset": "dset2",
        "solution": "sol2",
    }
    assert result["derivative_expression"] == "real(fsens(control_variable))"
    assert result["solver_started"] is False
    assert result["filesystem_modified"] is False
    assert "mph" not in sys.modules


def test_commit_preflight_is_caller_relative(monkeypatch):
    monkeypatch.setattr(gate.sys, "platform", "win32")
    monkeypatch.setattr(
        gate._resource_admission,
        "_windows_commit_bytes",
        lambda: (40, 100),
    )

    assert gate._resource_preflight(0.61)["admitted"] is True
    assert gate._resource_preflight(0.59)["admitted"] is False


def test_runtime_failure_is_path_redacted_and_cleans_client(
    tmp_path,
    gate_root,
    monkeypatch,
):
    source, manifest, audit = _inputs(tmp_path)
    monkeypatch.setattr(gate._structural.os, "cpu_count", lambda: 4)
    spec = gate._spec(_args(gate_root, source, manifest, audit))
    cleared = []

    class Client:
        def __init__(self, *, cores, version):
            assert (cores, version) == (3, "6.4")

        def load(self, _path):
            raise RuntimeError("private D:/fixture/model.mph detail")

        def clear(self):
            cleared.append(True)

    monkeypatch.setattr(gate, "_git_identity", lambda: {"revision": "a" * 40, "clean": True})
    monkeypatch.setattr(
        gate,
        "_resource_preflight",
        lambda _ceiling: {"available": True, "admitted": True},
    )
    monkeypatch.setitem(sys.modules, "mph", SimpleNamespace(Client=Client))

    receipt, private = gate._run(spec)

    assert receipt["success"] is False
    assert receipt["error"] == {"code": "native_gradient_failed", "type": "RuntimeError"}
    assert "D:/fixture/model.mph" not in json.dumps(receipt)
    assert "D:/fixture/model.mph" in private["error"]
    assert receipt["cleanup"] == {
        "model_removed": True,
        "client_clear": True,
        "source_unchanged": True,
    }
    assert cleared == [True]


def _runtime_spec(tmp_path, gate_root, monkeypatch):
    source, manifest, audit = _inputs(tmp_path)
    monkeypatch.setattr(gate._structural.os, "cpu_count", lambda: 4)
    spec = gate._spec(_args(gate_root, source, manifest, audit))
    spec.update(
        {
            "native_receipt": gate_root / "native.json",
            "native_receipt_sha256": "b" * 64,
            "steps": (0.01, 0.003, 0.001),
            "points": gate_root / "points.jsonl",
            "receipt": gate_root / "receipt.json",
            "direction_seed": 71004,
            "direction": [1.0, 0.0],
            "direction_relative_step": 0.001,
        }
    )
    monkeypatch.setattr(gate, "_git_identity", lambda: {"revision": "a" * 40, "clean": True})
    return spec


@pytest.mark.parametrize("runner", [gate, fd_gate, directional_gate])
def test_runtime_rejects_changed_source_before_native_or_output_side_effects(
    tmp_path, gate_root, monkeypatch, runner
):
    spec = _runtime_spec(tmp_path, gate_root, monkeypatch)
    spec["source"].write_bytes(b"replaced after compilation")
    for name in ("base_copy", "configured_copy", "points", "receipt"):
        spec[name].write_bytes(b"caller-owned sentinel")
    preflight = []

    def admit(ceiling):
        preflight.append(ceiling)
        return {"admitted": True}

    monkeypatch.setattr(gate, "_resource_preflight", admit)

    def unexpected_client(**kwargs):
        raise RuntimeError("native client must not be constructed")

    monkeypatch.setitem(sys.modules, "mph", SimpleNamespace(Client=unexpected_client))
    with pytest.raises(ValueError, match="source.*identity|source.*SHA-256"):
        runner._run(spec)
    assert preflight == []
    for name in ("base_copy", "configured_copy", "points", "receipt"):
        assert spec[name].read_bytes() == b"caller-owned sentinel"


@pytest.mark.parametrize("runner", [gate, fd_gate, directional_gate])
@pytest.mark.parametrize("clear_failure", [False, True])
def test_runtime_source_hash_failure_retains_primary_error_and_cleanup(
    tmp_path, gate_root, monkeypatch, runner, clear_failure
):
    spec = _runtime_spec(tmp_path, gate_root, monkeypatch)
    cleared = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def load(self, path):
            spec["source"].unlink()
            raise RuntimeError("primary model-load failure")

        def clear(self):
            cleared.append(True)
            if clear_failure:
                raise RuntimeError("client-clear failure")

    monkeypatch.setattr(gate, "_resource_preflight", lambda ceiling: {"admitted": True})
    monkeypatch.setitem(sys.modules, "mph", SimpleNamespace(Client=Client))
    monkeypatch.setattr(
        runner,
        "_parser",
        lambda: SimpleNamespace(parse_args=lambda argv: SimpleNamespace(dry_run=False)),
    )
    monkeypatch.setattr(runner, "_spec", lambda args: spec)
    assert runner.main([]) == 1
    receipt = json.loads(spec["receipt"].read_text(encoding="utf-8"))
    private = json.loads(spec["private_receipt"].read_text(encoding="utf-8"))
    assert receipt["success"] is False
    assert receipt["error"]["type"] == "RuntimeError"
    assert "primary model-load failure" in private["error"]
    assert receipt["cleanup"]["source_unchanged"] is False
    assert receipt["cleanup"]["client_clear"] is (not clear_failure)
    assert "source_hash_error" in private
    assert "source_hash_error" not in json.dumps(receipt)
    assert cleared == [True]
