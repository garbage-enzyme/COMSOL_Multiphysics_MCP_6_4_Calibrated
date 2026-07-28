"""Bounded hashing, atomic replacement, and complete-row persistence."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import stat
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Sequence

from .canonical import validate_finite_json

DEFAULT_REPLACE_RETRY_SECONDS = 1.0
DEFAULT_MAX_JSONL_BYTES = 256 * 1024 * 1024
WriteStageHook = Callable[[str, Path], None]


class _FileSizeLimitError(ValueError):
    pass


def _open_regular_file_descriptor(path: str | Path) -> tuple[int, os.stat_result]:
    candidate = Path(path)
    before = os.stat(candidate, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("bounded reading requires a regular file without links")
    flags = os.O_RDONLY
    for name in ("O_BINARY", "O_NOINHERIT", "O_NONBLOCK", "O_NOFOLLOW"):
        flags |= getattr(os, name, 0)
    descriptor = os.open(candidate, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("bounded reading requires a regular file")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("file identity changed before bounded reading")
        return descriptor, opened
    except BaseException:
        os.close(descriptor)
        raise


def read_file_bytes_bounded(path: str | Path, *, max_bytes: int) -> bytes:
    """Read at most one regular file's declared byte limit from one descriptor."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    descriptor, opened = _open_regular_file_descriptor(path)
    try:
        if opened.st_size > max_bytes:
            raise _FileSizeLimitError("file exceeds the declared reading limit")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise _FileSizeLimitError("file grew beyond the declared reading limit")
        return data
    finally:
        os.close(descriptor)


def _notify(hook: WriteStageHook | None, stage: str, path: Path) -> None:
    if hook is not None:
        hook(stage, path)


def fsync_directory(path: str | Path) -> None:
    path = Path(path)
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sha256_file_bounded(
    path: str | Path,
    *,
    max_bytes: int,
    chunk_bytes: int = 1024 * 1024,
) -> dict[str, Any]:
    """Hash one regular file while refusing a caller-declared size overflow."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    if isinstance(chunk_bytes, bool) or not isinstance(chunk_bytes, int) or chunk_bytes < 1:
        raise ValueError("chunk_bytes must be a positive integer")
    descriptor, opened = _open_regular_file_descriptor(path)
    digest = hashlib.sha256()
    observed = 0
    try:
        if opened.st_size > max_bytes:
            raise ValueError("file exceeds the declared hashing limit")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            while block := handle.read(min(chunk_bytes, max_bytes - observed + 1)):
                observed += len(block)
                if observed > max_bytes:
                    raise ValueError("file grew beyond the declared hashing limit")
                digest.update(block)
    finally:
        os.close(descriptor)
    return {"sha256": digest.hexdigest(), "byte_count": observed}


def atomic_write_bytes(
    path: str | Path,
    payload: bytes,
    *,
    retry_seconds: float = DEFAULT_REPLACE_RETRY_SECONDS,
    stage_hook: WriteStageHook | None = None,
    replace_fn: Callable[
        [
            str | bytes | os.PathLike[str] | os.PathLike[bytes],
            str | bytes | os.PathLike[str] | os.PathLike[bytes],
        ],
        None,
    ]
    | None = None,
    compact_temporary: bool = False,
) -> None:
    """Durably replace one file with complete same-directory temporary bytes."""
    target = Path(path)
    if not isinstance(payload, bytes):
        raise ValueError("atomic payload must be bytes")
    if (
        isinstance(retry_seconds, bool)
        or not isinstance(retry_seconds, (int, float))
        or not math.isfinite(float(retry_seconds))
        or retry_seconds < 0
    ):
        raise ValueError("retry_seconds must be a finite non-negative number")
    retry_seconds = float(retry_seconds)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".tmp-{uuid.uuid4().hex[:8]}"
        if compact_temporary
        else f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    replaced = False
    replace = replace_fn or os.replace
    try:
        _notify(stage_hook, "before_temporary_write", target)
        with temporary.open("xb") as handle:
            handle.write(payload)
            _notify(stage_hook, "after_temporary_write", target)
            handle.flush()
            os.fsync(handle.fileno())
        _notify(stage_hook, "after_file_fsync", target)
        deadline = time.monotonic() + retry_seconds
        while True:
            try:
                replace(temporary, target)
                replaced = True
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        _notify(stage_hook, "after_replace", target)
        fsync_directory(target.parent)
        _notify(stage_hook, "after_directory_fsync", target)
    finally:
        if not replaced:
            temporary.unlink(missing_ok=True)


def json_document_bytes(value: Any) -> bytes:
    """Return the legacy pretty JSON document bytes used by durable state."""
    validate_finite_json(value)
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def atomic_write_json(
    path: str | Path,
    value: Any,
    *,
    stage_hook: WriteStageHook | None = None,
    replace_fn: Callable[
        [
            str | bytes | os.PathLike[str] | os.PathLike[bytes],
            str | bytes | os.PathLike[str] | os.PathLike[bytes],
        ],
        None,
    ]
    | None = None,
    compact_temporary: bool = False,
) -> None:
    """Write one finite pretty JSON document through atomic replacement."""
    atomic_write_bytes(
        path,
        json_document_bytes(value),
        stage_hook=stage_hook,
        replace_fn=replace_fn,
        compact_temporary=compact_temporary,
    )


def _append_complete_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    fsync_directory(path.parent)


def append_jsonl_record(path: str | Path, value: Any) -> None:
    """Append one finite compact JSON value followed by one newline and fsync."""
    validate_finite_json(value)
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    _append_complete_bytes(Path(path), payload)


def append_csv_row(path: str | Path, row: Sequence[Any]) -> None:
    """Append one quoted CSV row followed by one newline and fsync."""
    validate_finite_json(list(row))
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="\n").writerow(row)
    _append_complete_bytes(Path(path), buffer.getvalue().encode("utf-8"))


def read_complete_jsonl(
    path: str | Path,
    *,
    max_bytes: int = DEFAULT_MAX_JSONL_BYTES,
    version_field: str | None = None,
    current_version: str | None = None,
    legacy_versions: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Classify and return complete JSONL records without rewriting the source."""
    candidate = Path(path)
    if not candidate.exists():
        return {"state": "absent", "records": [], "complete_byte_count": 0}
    try:
        data = read_file_bytes_bounded(candidate, max_bytes=max_bytes)
    except _FileSizeLimitError:
        return {"state": "oversized", "records": [], "complete_byte_count": 0}
    complete_end = data.rfind(b"\n") + 1
    complete = data[:complete_end]
    trailing = data[complete_end:]
    records = []
    try:
        for line_number, line in enumerate(complete.splitlines(), start=1):
            if not line:
                raise ValueError(f"empty JSONL record at line {line_number}")
            value = json.loads(line.decode("utf-8"))
            validate_finite_json(value)
            records.append(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return {
            "state": "corrupt",
            "records": [],
            "complete_byte_count": 0,
            "error_type": type(exc).__name__,
        }
    state = "incomplete" if trailing else "current_valid"
    if version_field is not None:
        if not current_version:
            raise ValueError("current_version is required for versioned JSONL recovery")
        versions = {
            record.get(version_field) if isinstance(record, dict) else None for record in records
        }
        if versions == {current_version}:
            state = "incomplete" if trailing else "current_valid"
        elif not trailing and len(versions) == 1 and versions <= set(legacy_versions):
            state = "legacy_valid"
        else:
            state = "corrupt"
            records = []
    return {
        "state": state,
        "records": records,
        "complete_byte_count": complete_end,
        "trailing_byte_count": len(trailing),
    }


__all__ = [
    "DEFAULT_MAX_JSONL_BYTES",
    "DEFAULT_REPLACE_RETRY_SECONDS",
    "append_csv_row",
    "append_jsonl_record",
    "atomic_write_bytes",
    "atomic_write_json",
    "fsync_directory",
    "json_document_bytes",
    "read_file_bytes_bounded",
    "read_complete_jsonl",
    "sha256_file_bounded",
]
