"""Portable output locations for standalone repository recipes."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from src.settings import settings_environment


def _validated_output_root(value: str | Path) -> Path:
    text = str(value)
    if not text.isascii():
        raise ValueError("recipe output root must contain ASCII characters only")
    path = Path(text)
    if not path.is_absolute():
        raise ValueError("recipe output root must be absolute")
    root = path.resolve()
    checkout = Path(__file__).resolve().parents[1]
    try:
        root.relative_to(checkout)
    except ValueError:
        return root
    raise ValueError("recipe output root must remain outside the source checkout")


def recipe_output_dir() -> Path:
    """Return an ASCII-safe output directory outside the source tree."""
    environment = settings_environment()
    configured = environment.get("COMSOL_MCP_RUNTIME_DIR")
    if configured:
        root = _validated_output_root(configured)
    elif os.name == "nt" and Path("D:/").exists():
        root = _validated_output_root("D:/comsol_runtime")
    elif os.name == "nt":
        root = _validated_output_root(
            Path(environment.get("PROGRAMDATA", "C:/ProgramData")) / "comsol_mcp_runtime"
        )
    else:
        root = _validated_output_root(Path(tempfile.gettempdir()) / "comsol_runtime")
    output = root / "recipes"
    output.mkdir(parents=True, exist_ok=True)
    return output
