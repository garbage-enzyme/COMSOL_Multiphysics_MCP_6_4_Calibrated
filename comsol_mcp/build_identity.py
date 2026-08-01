"""Content-derived package build identity shared by source and wheel installs."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from comsol_mcp import __version__
from comsol_mcp.durable import canonical_sha256_v1

_PACKAGE_CONTENT_FRAMING = b"comsol-mcp-package-content-v2\0"


def _package_files(package_root: Path) -> list[Path]:
    files = []
    for path in package_root.rglob("*"):
        is_junction = getattr(path, "is_junction", lambda: False)
        if path.is_symlink() or is_junction():
            raise ValueError("package content must not contain symlinks or junctions")
        if (
            path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix.casefold() not in {".pyc", ".pyo"}
        ):
            files.append(path)
    return sorted(files, key=lambda path: path.relative_to(package_root).as_posix())


def package_content_sha256(package_root: str | Path | None = None) -> str:
    """Hash sorted relative paths and bytes for all shipped package files."""
    candidate = Path(package_root) if package_root is not None else Path(__file__).parent
    is_junction = getattr(candidate, "is_junction", lambda: False)
    if candidate.is_symlink() or is_junction():
        raise ValueError("package_root must not be a symlink or junction")
    root = candidate.resolve()
    if not root.is_dir():
        raise ValueError("package_root must be a directory")
    files = _package_files(root)
    if not files:
        raise ValueError("package_root contains no package files")
    digest = hashlib.sha256()
    digest.update(_PACKAGE_CONTENT_FRAMING)
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def get_build_identity(package_root: str | Path | None = None) -> dict[str, Any]:
    """Return a path-free identity that changes with any shipped package byte."""
    body = {
        "schema_name": "comsol_mcp.build_identity",
        "schema_version": "1.0.0",
        "package_name": "comsol-mcp",
        "package_version": __version__,
        "package_content_sha256": package_content_sha256(package_root),
        "content_scope": ("length_prefixed_sorted_relative_non_cache_package_paths_and_file_bytes"),
        "generated_files_included": True,
        "paths_included": False,
    }
    return {**body, "build_identity_sha256": canonical_sha256_v1(body)}


__all__ = ["get_build_identity", "package_content_sha256"]
