"""Shared locking and crash-tail recovery for bounded JSONL journals."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .store import JobLock


def _lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


@contextmanager
def locked_journal(path: str | Path) -> Iterator[Path]:
    """Hold the process-safe lock associated with one journal path."""
    journal = Path(path)
    journal.parent.mkdir(parents=True, exist_ok=True)
    with JobLock(_lock_path(journal)):
        yield journal


def recover_jsonl_tail(path: str | Path, *, max_row_bytes: int) -> None:
    """Repair only an unterminated final record while a journal lock is held.

    A complete JSON value without its final newline is retained and terminated.
    An incomplete final value is truncated. Newline-terminated corruption is
    deliberately left for the journal validator to reject.
    """
    journal = Path(path)
    if not journal.exists() or journal.stat().st_size == 0:
        return
    with journal.open("r+b") as handle:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        handle.seek(end - 1)
        if handle.read(1) == b"\n":
            return

        boundary = -1
        scan_end = end - 1
        # Scan exactly one cap-sized window per step: then any tail behind a
        # found newline is within max_row_bytes, and only the no-newline case
        # reaches the oversize branch, where dropping the single unterminated
        # record is the documented repair.
        chunk_size = max_row_bytes + 1
        while scan_end > 0 and boundary < 0:
            window_start = max(0, scan_end - chunk_size)
            handle.seek(window_start)
            chunk = handle.read(scan_end - window_start)
            found = chunk.rfind(b"\n")
            if found >= 0:
                boundary = window_start + found
            scan_end = window_start
        record_start = boundary + 1
        handle.seek(record_start)
        tail = handle.read(end - record_start)

        if len(tail) > max_row_bytes:
            handle.truncate(record_start)
        else:
            try:
                json.loads(tail.decode("utf-8"))
            except UnicodeDecodeError, json.JSONDecodeError:
                handle.truncate(record_start)
            else:
                handle.seek(0, os.SEEK_END)
                handle.write(b"\n")
        handle.flush()
        os.fsync(handle.fileno())


__all__ = ["locked_journal", "recover_jsonl_tail"]
