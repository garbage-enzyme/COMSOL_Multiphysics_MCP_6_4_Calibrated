"""Auditable native-import policy for the MCP host and isolated workers."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import version
from typing import Any, Literal

NativeRuntimeScope = Literal["mcp_main_process", "isolated_worker", "offline_cli"]


@dataclass(frozen=True)
class NativeRuntimeImport:
    """Declare where one native-backed runtime may first be imported."""

    module: str
    distribution: str
    scope: NativeRuntimeScope
    preload_before_event_loop: bool
    reason: str


NATIVE_RUNTIME_MANIFEST = (
    NativeRuntimeImport(
        "numpy",
        "numpy",
        "mcp_main_process",
        True,
        "Shared array runtime used by solver-free evidence and COMSOL adapters.",
    ),
    NativeRuntimeImport(
        "scipy",
        "scipy",
        "mcp_main_process",
        True,
        "Spectral characterization imports the SciPy package before its numerical helpers.",
    ),
    NativeRuntimeImport(
        "scipy.optimize",
        "scipy",
        "mcp_main_process",
        True,
        "Spectral characterization uses brentq and curve_fit in the MCP process.",
    ),
    NativeRuntimeImport(
        "scipy.interpolate",
        "scipy",
        "mcp_main_process",
        True,
        "Field evidence uses griddata in the MCP process.",
    ),
    NativeRuntimeImport(
        "scipy.spatial",
        "scipy",
        "mcp_main_process",
        True,
        "Field interpolation handles Qhull results in the MCP process.",
    ),
    NativeRuntimeImport(
        "jpype",
        "jpype1",
        "mcp_main_process",
        True,
        "MPh and clientapi tools share JPype without starting the JVM at import time.",
    ),
    NativeRuntimeImport(
        "mph",
        "mph",
        "mcp_main_process",
        True,
        "Session tools use one process-global MPh wrapper.",
    ),
    NativeRuntimeImport(
        "psutil",
        "psutil",
        "mcp_main_process",
        True,
        "Ownership and resource controls use psutil in the MCP process.",
    ),
    NativeRuntimeImport(
        "pydantic_core",
        "pydantic-core",
        "mcp_main_process",
        True,
        "FastMCP request validation loads Pydantic's native core before dispatch.",
    ),
    NativeRuntimeImport(
        "matplotlib",
        "matplotlib",
        "isolated_worker",
        False,
        "Field PNG rendering selects the Matplotlib backend only in field_plot_worker.",
    ),
    NativeRuntimeImport(
        "matplotlib.colors",
        "matplotlib",
        "isolated_worker",
        False,
        "Field PNG logarithmic normalization is isolated in field_plot_worker.",
    ),
    NativeRuntimeImport(
        "matplotlib.pyplot",
        "matplotlib",
        "isolated_worker",
        False,
        "Field PNG rendering selects its backend only in field_plot_worker.",
    ),
    NativeRuntimeImport(
        "torch",
        "torch",
        "isolated_worker",
        False,
        "Optional semantic inference is contained in semantic_worker.",
    ),
    NativeRuntimeImport(
        "sentence_transformers",
        "sentence-transformers",
        "isolated_worker",
        False,
        "Optional semantic model loading is contained in semantic_worker.",
    ),
    NativeRuntimeImport(
        "fitz",
        "pymupdf",
        "offline_cli",
        False,
        "PDF extraction is an offline index-build operation, not an MCP tool path.",
    ),
)


def preload_mcp_native_runtime() -> dict[str, str]:
    """Load every main-process native runtime before MCP event-loop dispatch."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("MCP native runtime preload must run on the main thread")

    preload_items = tuple(
        item for item in NATIVE_RUNTIME_MANIFEST if item.preload_before_event_loop
    )
    if any(item.scope != "mcp_main_process" for item in preload_items):
        raise RuntimeError("MCP native runtime preload contains a non-main-process scope")

    jpype: Any = import_module("jpype")
    if jpype.isJVMStarted():
        raise RuntimeError("MCP native runtime preload found an already-started JVM")

    loaded: dict[str, str] = {}
    for item in preload_items:
        import_module(item.module)
        loaded[item.module] = version(item.distribution)

    if jpype.isJVMStarted():
        raise RuntimeError("Import-only MCP native runtime preload unexpectedly started the JVM")
    return loaded


__all__ = [
    "NATIVE_RUNTIME_MANIFEST",
    "NativeRuntimeImport",
    "NativeRuntimeScope",
    "preload_mcp_native_runtime",
]
