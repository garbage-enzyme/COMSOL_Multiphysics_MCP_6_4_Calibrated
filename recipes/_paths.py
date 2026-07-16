"""Portable output locations for standalone repository recipes."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile


def recipe_output_dir() -> Path:
    """Return an ASCII-safe output directory outside the source tree."""
    configured = os.environ.get("COMSOL_MCP_RUNTIME_DIR")
    if configured:
        root = Path(configured)
    elif os.name == "nt" and Path("D:/").exists():
        root = Path("D:/comsol_runtime")
    elif os.name == "nt":
        root = Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "comsol_mcp_runtime"
    else:
        root = Path(tempfile.gettempdir()) / "comsol_runtime"
    output = root / "recipes"
    output.mkdir(parents=True, exist_ok=True)
    return output
