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


def _create_recipe_output(root: str | Path) -> Path:
    output = _validated_output_root(root) / "recipes"
    output.mkdir(parents=True, exist_ok=True)
    return output


def _automatic_output_roots(environment: dict[str, str]) -> tuple[Path, ...]:
    candidates = []
    if os.name == "nt" and Path("D:/").exists():
        candidates.append(Path("D:/comsol_runtime"))
    if os.name == "nt":
        candidates.append(
            Path(environment.get("PROGRAMDATA", "C:/ProgramData")) / "comsol_mcp_runtime"
        )
    candidates.append(Path(tempfile.gettempdir()) / "comsol_runtime")
    return tuple(candidates)


def recipe_output_dir() -> Path:
    """Return an ASCII-safe output directory outside the source tree."""
    environment = settings_environment()
    configured = environment.get("COMSOL_MCP_RUNTIME_DIR")
    if configured:
        return _create_recipe_output(configured)

    failures = []
    for root in _automatic_output_roots(environment):
        try:
            return _create_recipe_output(root)
        except (OSError, ValueError) as exc:
            failures.append(exc)
    raise OSError("no writable ASCII recipe output root is available") from failures[-1]
