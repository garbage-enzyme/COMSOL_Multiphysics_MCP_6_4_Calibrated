"""Solver-free contracts for the native gradient licensed runner."""

import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

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
