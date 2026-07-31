"""Native-runtime manifest and cold-import regression tests."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import src.native_runtime as native_runtime_module

from comsol_mcp.native_runtime import (
    NATIVE_RUNTIME_MANIFEST,
    NativeRuntimeImport,
    preload_mcp_native_runtime,
)

ROOT = Path(__file__).parents[2]
PACKAGE_ROOT = ROOT / "comsol_mcp"
AUDITED_NATIVE_ROOTS = {
    "fitz",
    "jpype",
    "matplotlib",
    "mph",
    "numpy",
    "psutil",
    "scipy",
    "sentence_transformers",
    "torch",
}


def test_native_runtime_manifest_separates_host_worker_and_offline_imports() -> None:
    modules = [item.module for item in NATIVE_RUNTIME_MANIFEST]
    assert len(modules) == len(set(modules))
    assert {item.module for item in NATIVE_RUNTIME_MANIFEST if item.preload_before_event_loop} == {
        "jpype",
        "mph",
        "numpy",
        "psutil",
        "pydantic_core",
        "scipy.interpolate",
        "scipy.optimize",
        "scipy.spatial",
    }
    assert all(
        item.scope == "mcp_main_process"
        for item in NATIVE_RUNTIME_MANIFEST
        if item.preload_before_event_loop
    )
    assert {item.module for item in NATIVE_RUNTIME_MANIFEST if item.scope == "isolated_worker"} == {
        "matplotlib.pyplot",
        "sentence_transformers",
        "torch",
    }
    assert {item.module for item in NATIVE_RUNTIME_MANIFEST if item.scope == "offline_cli"} == {
        "fitz"
    }


def test_every_direct_native_import_is_classified_by_the_manifest() -> None:
    imported_roots: set[str] = set()
    for path in PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])

    manifest_roots = {item.module.split(".", 1)[0] for item in NATIVE_RUNTIME_MANIFEST}
    assert imported_roots & AUDITED_NATIVE_ROOTS == AUDITED_NATIVE_ROOTS
    assert imported_roots & AUDITED_NATIVE_ROOTS <= manifest_roots


def test_native_runtime_preload_rejects_worker_thread() -> None:
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            preload_mcp_native_runtime()
        except BaseException as exc:  # noqa: BLE001 - the thread must report every failure
            errors.append(exc)

    worker = threading.Thread(target=invoke)
    worker.start()
    worker.join(timeout=5.0)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert "main thread" in str(errors[0])


def test_native_runtime_preload_validates_every_scope_before_import(monkeypatch) -> None:
    imports: list[str] = []
    monkeypatch.setattr(
        native_runtime_module,
        "NATIVE_RUNTIME_MANIFEST",
        (
            NativeRuntimeImport(
                module="numpy",
                distribution="numpy",
                scope="mcp_main_process",
                preload_before_event_loop=True,
                reason="test main-process preload",
            ),
            NativeRuntimeImport(
                module="matplotlib.pyplot",
                distribution="matplotlib",
                scope="isolated_worker",
                preload_before_event_loop=True,
                reason="invalid worker preload",
            ),
        ),
    )
    monkeypatch.setattr(
        native_runtime_module,
        "import_module",
        lambda name: imports.append(name),
    )

    with pytest.raises(RuntimeError, match="non-main-process scope"):
        native_runtime_module.preload_mcp_native_runtime()

    assert imports == []


def test_fresh_main_thread_preload_covers_representative_lazy_native_calls() -> None:
    code = r"""
import json
from pathlib import Path
import sys

from comsol_mcp.native_runtime import preload_mcp_native_runtime

receipt = preload_mcp_native_runtime()

import jpype
import numpy as np
from scipy.interpolate import griddata
from scipy.optimize import brentq, curve_fit


def native_extensions():
    loaded = set()
    for name, module in sys.modules.items():
        path = getattr(module, "__file__", None)
        if path and Path(path).suffix.casefold() in {".pyd", ".dll", ".so"}:
            loaded.add((name, str(path)))
    return loaded


before = native_extensions()
root = brentq(lambda value: value - 0.5, 0.0, 1.0)
parameters, _ = curve_fit(
    lambda value, slope, intercept: slope * value + intercept,
    np.array([0.0, 1.0, 2.0, 3.0]),
    np.array([1.0, 3.0, 5.0, 7.0]),
)
interpolated = griddata(
    np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]]),
    np.array([0.0, 1.0, 1.0]),
    np.array([[0.25, 0.25]]),
    method="linear",
)
after = native_extensions()

assert after == before, sorted(after - before)
assert root == 0.5
assert np.allclose(parameters, [2.0, 1.0])
assert np.allclose(interpolated, [0.5])
assert not jpype.isJVMStarted()
print(json.dumps({"receipt": receipt, "native_extension_count": len(after)}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert set(result["receipt"]) == {
        "jpype",
        "mph",
        "numpy",
        "psutil",
        "pydantic_core",
        "scipy.interpolate",
        "scipy.optimize",
        "scipy.spatial",
    }
    assert result["native_extension_count"] > 0
