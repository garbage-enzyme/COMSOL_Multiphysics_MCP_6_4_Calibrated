"""Content-derived package build identity shared by source and wheel installs."""

from __future__ import annotations

import hashlib
import stat
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from comsol_mcp import __version__
from comsol_mcp.durable import canonical_sha256_v1

_PACKAGE_CONTENT_FRAMING = b"comsol-mcp-package-content-v2\0"
_INFLIGHT_HASHES: dict[Path, Future[str]] = {}
_INFLIGHT_HASHES_LOCK = threading.Lock()


def _reject_linked_components(path: Path) -> None:
    for component in (path, *path.parents):
        if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
            raise ValueError("package_root must not contain a symlink or junction")


def _file_identity(path: Path) -> tuple[int, int, int, int]:
    try:
        value = path.lstat()
    except OSError as exc:
        raise ValueError("package content changed during identity collection") from exc
    if not stat.S_ISREG(value.st_mode):
        raise ValueError("package content must contain only regular files")
    return (int(value.st_dev), int(value.st_ino), int(value.st_size), int(value.st_mtime_ns))


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


def _hash_package_content(root: Path) -> str:
    files = _package_files(root)
    if not files:
        raise ValueError("package_root contains no package files")
    baseline = {}
    digest = hashlib.sha256()
    digest.update(_PACKAGE_CONTENT_FRAMING)
    for path in files:
        relative_text = path.relative_to(root).as_posix()
        relative = relative_text.encode("utf-8")
        before = _file_identity(path)
        baseline[relative_text] = before
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ValueError("package content changed during identity collection") from exc
        if _file_identity(path) != before or len(payload) != before[2]:
            raise ValueError("package content changed during identity collection")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    final_files = _package_files(root)
    final = {path.relative_to(root).as_posix(): _file_identity(path) for path in final_files}
    if final != baseline:
        raise ValueError("package content changed during identity collection")
    return digest.hexdigest()


def package_content_sha256(package_root: str | Path | None = None) -> str:
    """Hash sorted relative paths and bytes for all shipped package files."""
    candidate = Path(package_root) if package_root is not None else Path(__file__).parent
    _reject_linked_components(candidate.absolute())
    root = candidate.resolve()
    if not root.is_dir():
        raise ValueError("package_root must be a directory")

    with _INFLIGHT_HASHES_LOCK:
        future = _INFLIGHT_HASHES.get(root)
        if future is None:
            future = Future()
            _INFLIGHT_HASHES[root] = future
            owns_hash = True
        else:
            owns_hash = False
    if not owns_hash:
        return future.result()
    try:
        result = _hash_package_content(root)
    except BaseException as exc:
        future.set_exception(exc)
        raise
    else:
        future.set_result(result)
        return result
    finally:
        with _INFLIGHT_HASHES_LOCK:
            if _INFLIGHT_HASHES.get(root) is future:
                del _INFLIGHT_HASHES[root]


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
