"""Portable output locations for standalone repository recipes."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from src.settings import settings_environment


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    try:
        return bool(path.lstat().st_file_attributes & 0x400)
    except (AttributeError, FileNotFoundError, OSError):
        return False


def _reject_reparse_ancestry(path: Path) -> None:
    for candidate in (path, *path.parents):
        if _is_reparse_point(candidate):
            raise ValueError("recipe output root must not traverse links or reparse points")


def _validated_output_root(value: str | Path) -> Path:
    text = str(value)
    if not text.isascii():
        raise ValueError("recipe output root must contain ASCII characters only")
    path = Path(text)
    if not path.is_absolute():
        raise ValueError("recipe output root must be absolute")
    _reject_reparse_ancestry(path)
    root = path.resolve()
    _reject_reparse_ancestry(root)
    checkout = Path(__file__).resolve().parents[1]
    try:
        root.relative_to(checkout)
    except ValueError:
        return root
    raise ValueError("recipe output root must remain outside the source checkout")


def _create_recipe_output(root: str | Path) -> Path:
    validated_root = _validated_output_root(root)
    output = validated_root / "recipes"
    _reject_reparse_ancestry(output)
    output.mkdir(parents=True, exist_ok=True)
    _reject_reparse_ancestry(output)
    resolved_output = output.resolve()
    if resolved_output.parent != validated_root:
        raise ValueError("recipe output directory escaped its validated root")
    return resolved_output


def _automatic_output_roots(environment: dict[str, str]) -> tuple[Path, ...]:
    candidates = []
    if os.name == "nt":
        program_data = environment.get("PROGRAMDATA")
        if program_data:
            candidates.append(Path(program_data) / "comsol_mcp_runtime")
    candidates.append(Path(tempfile.gettempdir()) / "comsol_runtime")
    return tuple(candidates)


def recipe_output_dir() -> Path:
    """Return an ASCII-safe output directory outside the source tree."""
    environment = settings_environment()
    configured = environment.get("COMSOL_MCP_RUNTIME_DIR")
    if configured is not None:
        return _create_recipe_output(configured)

    failures = []
    for root in _automatic_output_roots(environment):
        try:
            return _create_recipe_output(root)
        except (OSError, ValueError) as exc:
            failures.append(exc)
    raise OSError("no writable ASCII recipe output root is available") from failures[-1]
