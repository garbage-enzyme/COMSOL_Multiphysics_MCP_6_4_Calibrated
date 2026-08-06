"""Bounded hashing, atomic replacement, and complete-row persistence."""

from __future__ import annotations

import csv
import ctypes
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

DEFAULT_REPLACE_RETRY_SECONDS = 3.0
DEFAULT_MAX_JSONL_BYTES = 256 * 1024 * 1024
WriteStageHook = Callable[[str, Path], None]

_DELETE = 0x00010000
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_DISPOSITION_INFO_CLASS = 4


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


def snapshot_file_bounded(
    path: str | Path,
    *,
    max_bytes: int,
    prefix_bytes: int = 0,
    chunk_bytes: int = 1024 * 1024,
) -> dict[str, Any]:
    """Hash and size one regular file from one descriptor, retaining a prefix."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    if isinstance(prefix_bytes, bool) or not isinstance(prefix_bytes, int) or prefix_bytes < 0:
        raise ValueError("prefix_bytes must be a non-negative integer")
    if isinstance(chunk_bytes, bool) or not isinstance(chunk_bytes, int) or chunk_bytes < 1:
        raise ValueError("chunk_bytes must be a positive integer")
    descriptor, opened = _open_regular_file_descriptor(path)
    digest = hashlib.sha256()
    observed = 0
    prefix = bytearray()
    try:
        if opened.st_size > max_bytes:
            raise ValueError("file exceeds the declared snapshot limit")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            while block := handle.read(min(chunk_bytes, max_bytes - observed + 1)):
                observed += len(block)
                if observed > max_bytes:
                    raise ValueError("file grew beyond the declared snapshot limit")
                digest.update(block)
                if len(prefix) < prefix_bytes:
                    prefix.extend(block[: prefix_bytes - len(prefix)])
    finally:
        os.close(descriptor)
    return {
        "sha256": digest.hexdigest(),
        "byte_count": observed,
        "prefix": bytes(prefix),
    }


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


def publish_file_exclusive(
    temporary: str | Path,
    target: str | Path,
    *,
    link_fn: Callable[[str | Path, str | Path], None] | None = None,
) -> tuple[int, int]:
    """Atomically publish a complete same-directory file without replacement."""
    source = Path(temporary)
    destination = Path(target)
    if source.parent.resolve() != destination.parent.resolve():
        raise ValueError("exclusive publication requires one directory")
    opened = os.stat(source, follow_symlinks=False)
    if not stat.S_ISREG(opened.st_mode) or source.is_symlink():
        raise ValueError("exclusive publication requires a regular temporary file")
    link = link_fn or os.link
    link(source, destination)
    published = os.stat(destination, follow_symlinks=False)
    identity = (published.st_dev, published.st_ino)
    if identity != (opened.st_dev, opened.st_ino):
        raise RuntimeError("exclusive publication produced a different file identity")
    source.unlink()
    fsync_directory(destination.parent)
    return identity


def _windows_unlink_opened_file_if(
    path: Path,
    predicate: Callable[[int, os.stat_result], bool],
) -> bool:
    """Delete the opened Windows file identity only when ``predicate`` accepts it."""
    if os.name != "nt":
        raise OSError("opened-identity deletion requires Windows")
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        _GENERIC_READ | _DELETE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in (None, invalid_handle):
        return False
    descriptor: int | None = None
    try:
        descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0))
        handle = None
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not predicate(descriptor, opened):
            return False

        class FileDispositionInfo(ctypes.Structure):
            _fields_ = [("delete_file", ctypes.c_int)]

        set_information = kernel32.SetFileInformationByHandle
        set_information.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        set_information.restype = ctypes.c_int
        disposition = FileDispositionInfo(1)
        os_handle = msvcrt.get_osfhandle(descriptor)
        return bool(
            set_information(
                ctypes.c_void_p(os_handle),
                _FILE_DISPOSITION_INFO_CLASS,
                ctypes.byref(disposition),
                ctypes.sizeof(disposition),
            )
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)
        elif handle not in (None, invalid_handle):
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (ctypes.c_void_p,)
            close_handle.restype = ctypes.c_int
            close_handle(ctypes.c_void_p(handle))


def _read_descriptor_bounded(descriptor: int, maximum: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = bytearray()
    while len(chunks) <= maximum:
        block = os.read(descriptor, min(64 * 1024, maximum - len(chunks) + 1))
        if not block:
            break
        chunks.extend(block)
    return bytes(chunks)


def unlink_if_content(path: str | Path, expected: bytes, *, allow_prefix: bool = False) -> bool:
    """Remove only an opened regular file whose bytes match the expected publication."""
    if not isinstance(expected, bytes):
        raise ValueError("expected content must be bytes")
    target = Path(path)

    def matches(descriptor: int, _opened: os.stat_result) -> bool:
        observed = _read_descriptor_bounded(descriptor, len(expected))
        return expected.startswith(observed) if allow_prefix else observed == expected

    return _windows_unlink_opened_file_if(target, matches)


def unlink_if_identity(path: str | Path, identity: tuple[int, int]) -> bool:
    """Remove only the exact file identity published by the current operation."""
    target = Path(path)
    return _windows_unlink_opened_file_if(
        target,
        lambda _descriptor, opened: (opened.st_dev, opened.st_ino) == identity,
    )


def atomic_write_bytes_exclusive(
    path: str | Path,
    payload: bytes,
    *,
    stage_hook: WriteStageHook | None = None,
    link_fn: Callable[[str | Path, str | Path], None] | None = None,
) -> tuple[int, int]:
    """Durably publish bytes only when the destination is still absent."""
    target = Path(path)
    if not isinstance(payload, bytes):
        raise ValueError("atomic payload must be bytes")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    published = False
    try:
        _notify(stage_hook, "before_temporary_write", target)
        with temporary.open("xb") as handle:
            handle.write(payload)
            _notify(stage_hook, "after_temporary_write", target)
            handle.flush()
            os.fsync(handle.fileno())
        _notify(stage_hook, "after_file_fsync", target)
        identity = publish_file_exclusive(temporary, target, link_fn=link_fn)
        published = True
        _notify(stage_hook, "after_publish", target)
        return identity
    finally:
        if not published:
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


def atomic_write_json_exclusive(
    path: str | Path,
    value: Any,
    *,
    stage_hook: WriteStageHook | None = None,
    link_fn: Callable[[str | Path, str | Path], None] | None = None,
) -> tuple[int, int]:
    """Write one finite JSON document without replacing an existing target."""
    return atomic_write_bytes_exclusive(
        path,
        json_document_bytes(value),
        stage_hook=stage_hook,
        link_fn=link_fn,
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
        try:
            versions = {
                record.get(version_field) if isinstance(record, dict) else None
                for record in records
            }
        except TypeError:
            versions = set()
            state = "corrupt"
            records = []
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
    "atomic_write_bytes_exclusive",
    "atomic_write_json",
    "atomic_write_json_exclusive",
    "fsync_directory",
    "json_document_bytes",
    "publish_file_exclusive",
    "read_file_bytes_bounded",
    "read_complete_jsonl",
    "sha256_file_bounded",
    "unlink_if_content",
    "unlink_if_identity",
]
