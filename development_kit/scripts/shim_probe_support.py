"""Shared helpers for solver-free probes that launch the Python host or the shim.

Every probe writes complete 1.4.0 settings: the transport value, the shim
binary identity (path, SHA-256, version, target triple reported by the
binary itself) and one literal runtime directory, because both entry points
now refuse anything less before admission.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LOCK_NAME = "host_admission.lock"
REASON = re.compile(r"refusing to start: \[([a-z_]+)\]")


def shim_identity(shim: Path) -> dict[str, str]:
    """The identity the settings must record for this exact shim binary."""
    output = subprocess.run(  # noqa: S603 - the probe's own build artifact
        [str(shim), "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
        creationflags=NO_WINDOW,
    ).stdout.strip()
    match = re.fullmatch(r"comsol-mcp-shim (\S+) \((\S+)\)", output)
    if match is None:
        raise RuntimeError(f"unexpected shim version report: {output!r}")
    return {
        "path": str(shim.resolve()),
        "sha256": hashlib.sha256(shim.read_bytes()).hexdigest(),
        "version": match.group(1),
        "platform": match.group(2),
    }


def settings_document(
    *,
    transport: str,
    runtime: Path,
    shim: Path | None = None,
    models: Path | None = None,
    binary: dict[str, str] | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_name": "comsol_mcp.settings",
        "schema_version": "1.4.0",
        "profile": {"name": "core"},
        "runtime": {"directory": str(runtime), "jobs_directory": None},
        "transport": transport,
    }
    if models is not None:
        document["paths"] = {
            "model_read_roots": [str(models)],
            "artifact_write_root": str(runtime / "artifacts"),
        }
    if binary is not None:
        document["shim_binary"] = dict(binary)
    elif shim is not None:
        document["shim_binary"] = shim_identity(shim)
    return document


def write_settings(path: Path, document: dict[str, Any] | str) -> bytes:
    payload = document if isinstance(document, str) else json.dumps(document, indent=2)
    data = payload.encode("utf-8")
    path.write_bytes(data)
    return data


def base_environment() -> dict[str, str]:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("COMSOL_MCP_")}
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["COMSOL_MCP_PROFILE"] = "core"
    return environment


class Host:
    """One launched host process with its own stderr log."""

    def __init__(
        self,
        argv: list[str],
        env: dict[str, str],
        log: Path,
        *,
        cwd: Path = ROOT,
        process_group: int | None = None,
    ):
        self.log = log
        self._log_handle = log.open("wb")
        # process_group (POSIX) starts the host as a process-group leader,
        # the launch shape of a shell job.
        extra: dict[str, Any] = {} if process_group is None else {"process_group": process_group}
        self.process = subprocess.Popen(  # noqa: S603 - fixed probe argv
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._log_handle,
            creationflags=NO_WINDOW,
            **extra,
        )
        self.started = time.time()

    @property
    def pid(self) -> int:
        return self.process.pid

    def alive(self) -> bool:
        return self.process.poll() is None

    def request(self, message: dict[str, Any], timeout: float = 90.0) -> dict[str, Any] | None:
        """Send one JSON-RPC line and read one reply line (None on timeout)."""
        import threading

        line = (json.dumps(message) + "\n").encode("utf-8")
        try:
            self.process.stdin.write(line)
            self.process.stdin.flush()
        except OSError:
            return None
        box: list[bytes] = []
        reader = threading.Thread(target=lambda: box.append(self.process.stdout.readline()))
        reader.daemon = True
        reader.start()
        reader.join(timeout)
        if not box or not box[0]:
            return None
        try:
            return json.loads(box[0])
        except ValueError:
            return None

    def initialize(self, timeout: float = 90.0) -> bool:
        reply = self.request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "startup-probe", "version": "1"},
                },
            },
            timeout,
        )
        if reply is None or "result" not in reply:
            return False
        self.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return True

    def notify(self, message: dict[str, Any]) -> bool:
        """Send one notification; no reply line is expected or read."""
        try:
            self.process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            self.process.stdin.flush()
        except OSError:
            return False
        return True

    def ping(self, request_id: int = 99) -> bool:
        reply = self.request({"jsonrpc": "2.0", "id": request_id, "method": "ping"}, timeout=30)
        return bool(reply) and reply.get("id") == request_id and "result" in reply

    def close(self, timeout: float = 60.0) -> int:
        """Normal EOF; returns the exit code (kills only after the timeout)."""
        import threading

        def drain() -> None:
            try:
                while self.process.stdout.read(65536):
                    pass
            except OSError, ValueError:
                pass

        thread = threading.Thread(target=drain, daemon=True)
        thread.start()
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            code = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            code = self.process.wait(timeout=10)
        thread.join(timeout=5)
        self._log_handle.close()
        return code

    def wait_exit(self, timeout: float = 60.0) -> int | None:
        try:
            code = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        self._log_handle.close()
        return code

    def stderr(self) -> str:
        try:
            self._log_handle.flush()
        except OSError, ValueError:
            pass
        return self.log.read_text(encoding="utf-8", errors="replace")

    def reason(self) -> str | None:
        match = REASON.search(self.stderr())
        return match.group(1) if match else None

    def kill_tree(self) -> None:
        import psutil

        try:
            root = psutil.Process(self.pid)
            for child in root.children(recursive=True):
                child.kill()
            root.kill()
        except psutil.Error:
            pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        try:
            self._log_handle.close()
        except OSError, ValueError:
            pass


def python_host(
    python: Path, env: dict[str, str], log: Path, *, process_group: int | None = None
) -> Host:
    return Host([str(python), "-m", "comsol_mcp.server"], env, log, process_group=process_group)


def shim_host(
    shim: Path, env: dict[str, str], log: Path, *, process_group: int | None = None
) -> Host:
    return Host([str(shim)], env, log, process_group=process_group)


def read_lock(runtime: Path) -> tuple[dict[str, Any] | None, bytes | None]:
    path = runtime / LOCK_NAME
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, None
    except PermissionError:
        return None, b"<access denied>"
    try:
        return json.loads(raw), raw
    except ValueError:
        return None, raw


def wait_for_lock(runtime: Path, timeout: float = 60.0) -> dict[str, Any] | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        lock, _raw = read_lock(runtime)
        if lock is not None and lock.get("schema_version") == "1.1.0":
            return lock
        time.sleep(0.05)
    return None


def wait_for_owner(runtime: Path, host: Host, timeout: float = 60.0) -> dict[str, Any] | None:
    """Wait until the admission record names this host (or the host exits)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        lock, _raw = read_lock(runtime)
        if lock_owned_by(lock, host):
            return lock
        if not host.alive():
            return read_lock(runtime)[0]
        time.sleep(0.05)
    return read_lock(runtime)[0]


def lock_owned_by(lock: dict[str, Any] | None, host: Host) -> bool:
    """The admission record names this host (directly or behind a launcher)."""
    if not lock:
        return False
    import psutil

    pid = lock.get("pid")
    if pid == host.pid:
        return True
    try:
        return psutil.Process(pid).ppid() == host.pid
    except psutil.Error:
        return False


def birth_identity(pid: int) -> dict[str, Any]:
    import psutil

    process = psutil.Process(pid)
    return {
        "pid": pid,
        "process_create_time": process.create_time(),
        "command_signature": hashlib.sha256(
            "\0".join(process.cmdline()).encode("utf-8", errors="replace")
        ).hexdigest(),
    }


def dead_identity(python: Path) -> dict[str, Any]:
    """The birth identity of a process that has verifiably exited."""
    import psutil

    child = subprocess.Popen(  # noqa: S603 - fixed interpreter argv
        [str(python), "-c", "import time; time.sleep(0.5)"],
        creationflags=NO_WINDOW,
    )
    identity = birth_identity(child.pid)
    child.wait(timeout=30)
    deadline = time.time() + 10
    while psutil.pid_exists(identity["pid"]) and time.time() < deadline:
        time.sleep(0.05)
    return identity


def sleeper(python: Path) -> subprocess.Popen:
    return subprocess.Popen(  # noqa: S603 - fixed interpreter argv
        [str(python), "-c", "import time; time.sleep(600)"],
        creationflags=NO_WINDOW,
    )


def admission_record(owner: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """A complete 1.1.0 record for fixtures that model a previous host."""
    return {
        "schema_name": "comsol_mcp.host_admission",
        "schema_version": "1.1.0",
        "state": "active",
        "transport_mode": "python",
        **owner,
        "executable": None,
        "client": None,
        "settings": {"path": "fixture", "sha256": None},
        "domain": None,
        "shim": None,
        "owned_processes": [],
        "recovered_from": None,
        "acquired_at_epoch": time.time(),
        **extra,
    }


def write_record(runtime: Path, record: dict[str, Any]) -> bytes:
    runtime.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (runtime / LOCK_NAME).write_bytes(payload)
    return payload


def source_identity() -> dict[str, Any]:
    def git(*arguments: str) -> str:
        return subprocess.run(  # noqa: S603 - fixed git argv
            ["git", "-C", str(ROOT), *arguments],
            capture_output=True,
            text=True,
            check=False,
            creationflags=NO_WINDOW,
        ).stdout.strip()

    return {
        "head": git("rev-parse", "HEAD"),
        "dirty_paths": [line for line in git("status", "--porcelain").splitlines() if line],
        "python": sys.executable,
    }


def generate_manifest(python: Path, settings: Path, output: Path) -> subprocess.CompletedProcess:
    """Generate a discovery manifest bound to these exact settings bytes."""
    environment = base_environment()
    environment["COMSOL_MCP_SETTINGS_PATH"] = str(settings)
    return subprocess.run(  # noqa: S603 - fixed interpreter argv
        [str(python), "-m", "comsol_mcp.discovery_manifest", "generate", "--output", str(output)],
        cwd=str(ROOT),
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        creationflags=NO_WINDOW,
    )
