"""Bounded background process for Settings GUI manual-index generation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from collections import deque
from pathlib import Path
from typing import Any

MAX_EVENT_LINE_BYTES = 64 * 1024
MAX_STDERR_BYTES = 16 * 1024


class ManualIndexBuildTask:
    """Run one isolated index build and expose bounded progress events."""

    def __init__(self, *, pdf_root: str | Path, index_path: str | Path) -> None:
        self.pdf_root = Path(pdf_root)
        self.index_path = Path(index_path)
        token = uuid.uuid4().hex
        self.temporary_path = self.index_path.with_name(
            f"{self.index_path.name}.tmp-gui-{token}"
        )
        self._events: deque[dict[str, Any]] = deque(maxlen=64)
        self._events_lock = threading.Lock()
        self._cancel = threading.Event()
        self._process_lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("manual index task can only be started once")
        self._validate_request()
        self._thread = threading.Thread(target=self._run, name="manual-index-build", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()
        with self._process_lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()

    def drain_events(self) -> list[dict[str, Any]]:
        with self._events_lock:
            result = list(self._events)
            self._events.clear()
        return result

    def wait(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def _publish(self, event: dict[str, Any]) -> None:
        with self._events_lock:
            if event.get("event") in {"result", "error", "cancelled"}:
                self._events.clear()
            self._events.append(event)

    def _validate_request(self) -> None:
        if not self.pdf_root.is_absolute() or not self.pdf_root.is_dir():
            raise ValueError("PDF root must be an existing absolute folder")
        if not self.index_path.is_absolute() or not str(self.index_path).isascii():
            raise ValueError("SQLite index path must be an ASCII-only absolute path")
        if self.index_path.exists() and not self.index_path.is_file():
            raise ValueError("SQLite index destination must be a regular file")

    def _run(self) -> None:
        stderr_data = bytearray()
        request = json.dumps(
            {
                "pdf_root": str(self.pdf_root),
                "index_path": str(self.index_path),
                "temporary_path": str(self.temporary_path),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        saw_error = False
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "comsol_mcp.knowledge.lexical_build_worker"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
            )
            with self._process_lock:
                self._process = process
            assert process.stdin is not None
            assert process.stdout is not None
            assert process.stderr is not None

            def drain_stderr() -> None:
                while True:
                    chunk = process.stderr.read(4096)
                    if not chunk:
                        return
                    remaining = MAX_STDERR_BYTES - len(stderr_data)
                    if remaining > 0:
                        stderr_data.extend(chunk[:remaining])

            stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
            stderr_thread.start()
            process.stdin.write(request)
            process.stdin.close()
            for raw_line in process.stdout:
                if len(raw_line) > MAX_EVENT_LINE_BYTES:
                    raise RuntimeError("index worker returned an oversized event")
                try:
                    event = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise RuntimeError("index worker returned an invalid event") from exc
                if not isinstance(event, dict) or not isinstance(event.get("event"), str):
                    raise RuntimeError("index worker returned an invalid event")
                saw_error = saw_error or event["event"] == "error"
                self._publish(event)
            return_code = process.wait(timeout=5)
            stderr_thread.join(timeout=2)
            if self._cancel.is_set():
                self._publish({"event": "cancelled"})
            elif return_code != 0 and not saw_error:
                self._publish(
                    {
                        "event": "error",
                        "reason_code": "index_worker_failed",
                        "message": "The index worker stopped before completing the build.",
                    }
                )
        except (OSError, RuntimeError, subprocess.SubprocessError):
            self._publish(
                {
                    "event": "error",
                    "reason_code": "index_worker_failed",
                    "message": "The index worker could not complete. The previous index was preserved.",
                }
            )
        finally:
            with self._process_lock:
                self._process = None
            try:
                self.temporary_path.unlink(missing_ok=True)
            except OSError:
                self._publish(
                    {
                        "event": "error",
                        "reason_code": "temporary_cleanup_failed",
                        "message": "The temporary index file could not be removed safely.",
                    }
                )


__all__ = ["ManualIndexBuildTask"]
