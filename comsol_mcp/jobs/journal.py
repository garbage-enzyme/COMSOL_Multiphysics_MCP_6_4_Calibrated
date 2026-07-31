"""Shared locking and crash-tail recovery for bounded JSONL journals."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
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

        window = min(end, max_row_bytes + 1)
        handle.seek(end - window)
        suffix = handle.read(window)
        boundary = suffix.rfind(b"\n")
        if boundary < 0:
            if end > max_row_bytes:
                raise ValueError("unterminated journal row exceeds its byte limit")
            record_start = 0
            tail = suffix
        else:
            record_start = end - window + boundary + 1
            tail = suffix[boundary + 1 :]
            if len(tail) > max_row_bytes:
                raise ValueError("unterminated journal row exceeds its byte limit")

        try:
            json.loads(tail.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            handle.truncate(record_start)
        else:
            handle.seek(0, os.SEEK_END)
            handle.write(b"\n")
        handle.flush()
        os.fsync(handle.fileno())


__all__ = ["locked_journal", "recover_jsonl_tail"]
