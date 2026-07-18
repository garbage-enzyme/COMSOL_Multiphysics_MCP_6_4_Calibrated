"""Content-derived package build identity shared by source and wheel installs."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from comsol_mcp import __version__
from comsol_mcp.durable import canonical_sha256_v1


def _package_files(package_root: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in package_root.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix.casefold() not in {".pyc", ".pyo"}
        ),
        key=lambda path: path.relative_to(package_root).as_posix(),
    )


def package_content_sha256(package_root: str | Path | None = None) -> str:
    """Hash sorted relative paths and bytes for all shipped package files."""
    root = Path(package_root).resolve() if package_root is not None else Path(__file__).resolve().parent
    if not root.is_dir():
        raise ValueError("package_root must be a directory")
    files = _package_files(root)
    if not files:
        raise ValueError("package_root contains no package files")
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def get_build_identity(package_root: str | Path | None = None) -> dict[str, Any]:
    """Return a path-free identity that changes with any shipped package byte."""
    body = {
        "schema_name": "comsol_mcp.build_identity",
        "schema_version": "1.0.0",
        "package_name": "comsol-mcp",
        "package_version": __version__,
        "package_content_sha256": package_content_sha256(package_root),
        "content_scope": "sorted_relative_package_paths_and_file_bytes",
        "generated_files_included": False,
        "paths_included": False,
    }
    return {**body, "build_identity_sha256": canonical_sha256_v1(body)}


__all__ = ["get_build_identity", "package_content_sha256"]
