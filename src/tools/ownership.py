"""Cross-process solver ownership, collision detection, and preflight checks."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import psutil
from mcp.server.fastmcp import FastMCP

from src.settings import settings_environment
from src.utils.runtime_paths import default_runtime_dir as _shared_default_runtime_dir
from src.utils.control_plane import measured_call


LEASE_SCHEMA_VERSION = "3"
CREATE_TIME_TOLERANCE_SECONDS = 0.05
LEASE_IO_TIMEOUT_SECONDS = 1.0
LEASE_IO_POLL_SECONDS = 0.02
PROCESS_INVENTORY_STATUS_TIMEOUT_SECONDS = 0.5
PROCESS_INVENTORY_MUTATION_TIMEOUT_SECONDS = 10.0
PROCESS_INVENTORY_CACHE_SECONDS = 2.0


def _lease_io_deadline() -> float:
    return time.monotonic() + LEASE_IO_TIMEOUT_SECONDS


def _read_bytes_retry(path: Path, *, deadline: float | None = None) -> bytes:
    deadline = _lease_io_deadline() if deadline is None else deadline
    while True:
        try:
            return path.read_bytes()
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(LEASE_IO_POLL_SECONDS)


def _unlink_retry(
    path: Path,
    *,
    missing_ok: bool,
    expected_bytes: bytes | None = None,
) -> tuple[bool, str | None]:
    """Unlink one lease artifact after bounded exact-content validation."""
    deadline = _lease_io_deadline()
    while True:
        if expected_bytes is not None:
            try:
                if _read_bytes_retry(path, deadline=deadline) != expected_bytes:
                    return False, "changed"
            except FileNotFoundError:
                return (True, None) if missing_ok else (False, "missing")
            except PermissionError:
                return False, "sharing_violation_timeout"
        try:
            path.unlink(missing_ok=missing_ok)
            return True, None
        except FileNotFoundError:
            return (True, None) if missing_ok else (False, "missing")
        except PermissionError:
            if time.monotonic() >= deadline:
                return False, "sharing_violation_timeout"
            time.sleep(LEASE_IO_POLL_SECONDS)


def _replace_retry_if_unchanged(
    temporary: Path,
    destination: Path,
    expected_bytes: bytes,
) -> tuple[bool, str | None]:
    """Atomically replace a lease only while its exact pre-state is unchanged."""
    deadline = _lease_io_deadline()
    while True:
        try:
            if _read_bytes_retry(destination, deadline=deadline) != expected_bytes:
                return False, "changed"
        except FileNotFoundError:
            return False, "missing"
        except PermissionError:
            return False, "sharing_violation_timeout"
        try:
            os.replace(temporary, destination)
            return True, None
        except PermissionError:
            if time.monotonic() >= deadline:
                return False, "sharing_violation_timeout"
            time.sleep(LEASE_IO_POLL_SECONDS)


def _is_ascii_path(path: Path) -> bool:
    try:
        str(path.resolve()).encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def _default_runtime_dir() -> Path:
    return _shared_default_runtime_dir()


def _command_signature(command_line: list[str]) -> str:
    canonical = "\0".join(str(part) for part in command_line)
    return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()


def _process_record(process: psutil.Process) -> dict[str, Any]:
    with process.oneshot():
        try:
            command_line = list(process.cmdline())
        except (psutil.AccessDenied, psutil.ZombieProcess):
            command_line = []
        try:
            executable = process.exe()
        except (psutil.AccessDenied, psutil.ZombieProcess):
            executable = None
        return {
            "pid": process.pid,
            "parent_pid": process.ppid(),
            "name": process.name(),
            "create_time": process.create_time(),
            "command_line": command_line,
            "executable": executable,
        }


def _system_processes() -> list[dict[str, Any]]:
    records = []
    for process in psutil.process_iter():
        try:
            records.append(_process_record(process))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return records


def _system_listeners() -> list[dict[str, Any]]:
    listeners = []
    for connection in psutil.net_connections(kind="tcp"):
        if connection.status != psutil.CONN_LISTEN or connection.pid is None:
            continue
        host = getattr(connection.laddr, "ip", None)
        port = getattr(connection.laddr, "port", None)
        if host is not None and port is not None:
            listeners.append({"host": str(host), "port": int(port), "pid": int(connection.pid)})
    return listeners


class _BoundedProcessInventory:
    """Keep at most one daemon host scan in flight and expose bounded evidence."""

    def __init__(self, collector: Callable[[], list[dict[str, Any]]]):
        self._collector = collector
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._generation = 0
        self._cache_records: list[dict[str, Any]] | None = None
        self._cache_started_monotonic: float | None = None
        self._cache_completed_monotonic: float | None = None
        self._cache_latency_seconds: float | None = None
        self._last_error: str | None = None

    def _start_locked(self) -> None:
        self._generation += 1
        generation = self._generation
        started = time.monotonic()

        def run() -> None:
            try:
                records = self._collector()
                error = None
            except Exception as exc:
                records = None
                error = f"{type(exc).__name__}: {exc}"
            completed = time.monotonic()
            with self._lock:
                if generation != self._generation:
                    return
                self._last_error = error
                if records is not None:
                    self._cache_records = list(records)
                    self._cache_started_monotonic = started
                    self._cache_completed_monotonic = completed
                    self._cache_latency_seconds = completed - started

        self._thread = threading.Thread(
            target=run,
            name=f"comsol-process-inventory-{generation}",
            daemon=True,
        )
        self._thread.start()

    def collect(self, *, require_fresh: bool, timeout: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        requested = time.monotonic()
        timeout = max(0.01, float(timeout))
        with self._lock:
            cache_age = (
                requested - self._cache_completed_monotonic
                if self._cache_completed_monotonic is not None
                else None
            )
            if (
                not require_fresh
                and self._cache_records is not None
                and cache_age is not None
                and cache_age <= PROCESS_INVENTORY_CACHE_SECONDS
            ):
                return list(self._cache_records), {
                    "complete": True,
                    "state": "complete",
                    "fresh": False,
                    "source": "recent_complete_cache",
                    "cache_age_seconds": round(cache_age, 6),
                    "scan_latency_seconds": self._cache_latency_seconds,
                    "timeout_seconds": timeout,
                    "error": None,
                }
            if self._thread is None or not self._thread.is_alive():
                self._start_locked()
            thread = self._thread
        if thread is None:
            raise RuntimeError("process inventory thread failed to initialize")
        thread.join(timeout=timeout)
        completed = time.monotonic()
        with self._lock:
            fresh_complete = (
                self._cache_records is not None
                and self._cache_started_monotonic is not None
                and self._cache_completed_monotonic is not None
                and self._cache_started_monotonic >= requested
                and self._cache_completed_monotonic <= completed
            )
            if fresh_complete:
                return list(self._cache_records or []), {
                    "complete": True,
                    "state": "complete",
                    "fresh": True,
                    "source": "fresh_scan",
                    "cache_age_seconds": round(completed - self._cache_completed_monotonic, 6),
                    "scan_latency_seconds": self._cache_latency_seconds,
                    "timeout_seconds": timeout,
                    "error": self._last_error,
                }
            cache_age = (
                completed - self._cache_completed_monotonic
                if self._cache_completed_monotonic is not None
                else None
            )
            return list(self._cache_records or []), {
                "complete": False,
                "state": "unavailable" if self._last_error and not thread.is_alive() else "pending",
                "fresh": False,
                "source": "stale_cache_after_timeout" if self._cache_records is not None else "unavailable_after_timeout",
                "cache_age_seconds": round(cache_age, 6) if cache_age is not None else None,
                "scan_latency_seconds": self._cache_latency_seconds,
                "timeout_seconds": timeout,
                "error": self._last_error or "process inventory deadline exceeded",
            }


def _agent_owner_label() -> str:
    configured = settings_environment().get("COMSOL_MCP_OWNER")
    if configured:
        return configured
    try:
        parent_command = " ".join(psutil.Process(os.getppid()).cmdline()).casefold()
    except (psutil.Error, OSError):
        parent_command = ""
    if "opencode" in parent_command:
        return "opencode-mcp"
    if "codex" in parent_command:
        return "codex-mcp"
    return "comsol-mcp"


class SolverOwnership:
    """Coordinate one local COMSOL solver owner across agent processes."""

    def __init__(
        self,
        runtime_dir: str | Path | None = None,
        *,
        process_provider: Optional[Callable[[], list[dict[str, Any]]]] = None,
        pid: Optional[int] = None,
        parent_pid: Optional[int] = None,
        create_time: Optional[float] = None,
        command_line: Optional[list[str]] = None,
        owner: Optional[str] = None,
    ):
        self.runtime_dir = Path(runtime_dir) if runtime_dir else _default_runtime_dir()
        if not _is_ascii_path(self.runtime_dir):
            raise ValueError("COMSOL runtime/lease path must contain ASCII characters only")
        self.lease_path = self.runtime_dir / "solver_owner.json"
        self._process_provider = process_provider or _system_processes
        self._process_inventory = (
            None
            if process_provider is not None
            else _BoundedProcessInventory(self._process_provider)
        )
        self.pid = int(pid if pid is not None else os.getpid())
        self.parent_pid = int(parent_pid if parent_pid is not None else os.getppid())
        if create_time is None:
            create_time = psutil.Process(self.pid).create_time()
        self.create_time = float(create_time)
        if command_line is None:
            try:
                command_line = list(psutil.Process(self.pid).cmdline())
            except (psutil.Error, OSError):
                command_line = [sys.executable, *sys.argv]
        self.command_line = list(command_line)
        self.command_signature = _command_signature(self.command_line)
        self.owner = owner or _agent_owner_label()

    def _collect_processes(
        self,
        *,
        require_fresh: bool,
        timeout: float,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if self._process_inventory is not None:
            return self._process_inventory.collect(
                require_fresh=require_fresh,
                timeout=timeout,
            )
        started = time.monotonic()
        try:
            records = list(self._process_provider())
        except Exception as exc:
            return [], {
                "complete": False,
                "state": "unavailable",
                "fresh": False,
                "source": "custom_provider_error",
                "cache_age_seconds": None,
                "scan_latency_seconds": round(time.monotonic() - started, 6),
                "timeout_seconds": timeout,
                "error": f"{type(exc).__name__}: {exc}",
            }
        return records, {
            "complete": True,
            "state": "complete",
            "fresh": True,
            "source": "custom_provider",
            "cache_age_seconds": 0.0,
            "scan_latency_seconds": round(time.monotonic() - started, 6),
            "timeout_seconds": timeout,
            "error": None,
        }

    def _read_lease_with_bytes(
        self,
    ) -> tuple[Optional[dict[str, Any]], Optional[bytes], Optional[str]]:
        if not self.lease_path.is_file():
            return None, None, None
        try:
            raw = _read_bytes_retry(self.lease_path)
            return json.loads(raw.decode("utf-8")), raw, None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, None, f"Cannot read solver lease: {exc}"

    def _read_lease(self) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        lease, _raw, error = self._read_lease_with_bytes()
        return lease, error

    def _lease_state_from_process(
        self,
        lease: dict[str, Any], process: dict[str, Any] | None
    ) -> dict[str, Any]:
        required = {"pid", "process_create_time", "command_signature"}
        if not required <= set(lease):
            return {"state": "uncertain", "reason": "lease is missing process identity fields"}
        if process is None:
            return {"state": "stale", "reason": "lease PID no longer exists"}
        actual_time = process.get("create_time")
        if actual_time is None:
            return {"state": "uncertain", "reason": "process creation time is unavailable"}
        if abs(float(actual_time) - float(lease["process_create_time"])) > CREATE_TIME_TOLERANCE_SECONDS:
            return {"state": "stale", "reason": "PID was reused with a different creation time"}
        actual_command = list(process.get("command_line") or [])
        if actual_command and _command_signature(actual_command) != lease["command_signature"]:
            return {"state": "stale", "reason": "PID command line no longer matches the lease"}
        return {
            "state": "active",
            "reason": "PID, creation time, and command line match",
            "owned_by_current_process": (
                int(lease["pid"]) == self.pid
                and abs(float(lease["process_create_time"]) - self.create_time)
                <= CREATE_TIME_TOLERANCE_SECONDS
            ),
        }

    def _lease_state(self, lease: dict[str, Any], processes: list[dict[str, Any]]) -> dict[str, Any]:
        match = next((item for item in processes if item.get("pid") == int(lease.get("pid", -1))), None)
        return self._lease_state_from_process(lease, match)

    def _targeted_lease_state(self, lease: dict[str, Any]) -> dict[str, Any] | None:
        """Prove the recorded lease identity without waiting for a host-wide scan.

        A cold Windows process inventory can exceed the status budget even when
        the recorded lease PID is immediately inspectable.  This targeted read
        is diagnostic only; acquisition and external-collision decisions still
        require a complete inventory.
        """
        if self._process_inventory is None:
            # Injected providers are deterministic test/control surfaces.  Their
            # snapshots remain the authority so tests cannot accidentally probe
            # the host running the test suite.
            return None
        required = {"pid", "process_create_time", "command_signature"}
        if not required <= set(lease):
            return {"state": "uncertain", "reason": "lease is missing process identity fields"}
        try:
            process = psutil.Process(int(lease["pid"]))
            with process.oneshot():
                record = {
                    "pid": process.pid,
                    "create_time": process.create_time(),
                    "command_line": list(process.cmdline()),
                }
        except psutil.NoSuchProcess:
            return {"state": "stale", "reason": "lease PID no longer exists"}
        except (psutil.AccessDenied, psutil.ZombieProcess, OSError) as exc:
            return {"state": "uncertain", "reason": f"targeted lease identity unavailable: {exc}"}
        return self._lease_state_from_process(lease, record)

    @staticmethod
    def _is_descendant(pid: int, parent_map: dict[int, int], ancestor: int) -> bool:
        seen = set()
        current = pid
        while current and current not in seen:
            if current == ancestor:
                return True
            seen.add(current)
            current = parent_map.get(current, 0)
        return False

    def _external_solver_processes(
        self, processes: list[dict[str, Any]], lease: Optional[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        parent_map = {
            int(item["pid"]): int(item.get("parent_pid") or 0)
            for item in processes
            if item.get("pid") is not None
        }
        ancestor_pids = set()
        current = self.pid
        while current and current not in ancestor_pids:
            ancestor_pids.add(current)
            current = parent_map.get(current, 0)
        owned_pids = {self.pid}
        allowed_external_roots = set()
        if lease and int(lease.get("pid", -1)) == self.pid:
            owned_pids.update(int(pid) for pid in lease.get("comsol_server_pids", []))
            attached = lease.get("attached_server")
            if isinstance(attached, dict) and attached.get("owned") is False:
                attached_pid = attached.get("server_pid")
                match = next(
                    (item for item in processes if item.get("pid") == attached_pid),
                    None,
                )
                if match is not None:
                    actual_command = [str(part) for part in match.get("command_line") or []]
                    actual_time = match.get("create_time")
                    if (
                        actual_time is not None
                        and abs(float(actual_time) - float(attached.get("process_create_time", -1)))
                        <= CREATE_TIME_TOLERANCE_SECONDS
                        and _command_signature(actual_command)
                        == attached.get("command_signature")
                    ):
                        allowed_external_roots.add(int(attached_pid))
        evidence = []
        for item in processes:
            pid = int(item.get("pid") or 0)
            if (
                not pid
                or pid in owned_pids
                or pid in ancestor_pids
                or self._is_descendant(pid, parent_map, self.pid)
                or any(
                    self._is_descendant(pid, parent_map, root)
                    for root in allowed_external_roots
                )
            ):
                continue
            name = str(item.get("name") or "").casefold()
            command_line = [str(part) for part in item.get("command_line") or []]
            command = " ".join(command_line).casefold()
            kind = None
            if "mphserver" in name or "comsolmphserver" in name:
                kind = "comsol-server"
            elif name in {"java", "java.exe"} and "comsol" in command and "server" in command:
                kind = "comsol-java-server"
            elif any(pattern in command for pattern in ("mph.client", "import mph", "from mph", "-m mph")):
                kind = "python-mph-client"
            elif name in {"python", "python.exe", "pythonw", "pythonw.exe"}:
                for argument in command_line[1:]:
                    script = Path(argument.strip('"'))
                    if script.suffix.casefold() != ".py" or not script.is_file():
                        continue
                    try:
                        if script.stat().st_size <= 2 * 1024 * 1024:
                            source = script.read_text(encoding="utf-8", errors="ignore").casefold()
                            if "mph.client" in source:
                                kind = "python-mph-client-script"
                    except OSError:
                        pass
                    break
            if kind:
                evidence.append(
                    {
                        "pid": pid,
                        "parent_pid": item.get("parent_pid"),
                        "process_create_time": item.get("create_time"),
                        "kind": kind,
                        "command_signature": _command_signature(command_line),
                        "command_line": command_line[:32],
                    }
                )
        return evidence

    def status(
        self,
        session_state: Optional[dict[str, Any]] = None,
        *,
        require_fresh_inventory: bool = False,
        inventory_timeout: float | None = None,
    ) -> dict[str, Any]:
        timeout = (
            PROCESS_INVENTORY_MUTATION_TIMEOUT_SECONDS
            if require_fresh_inventory
            else PROCESS_INVENTORY_STATUS_TIMEOUT_SECONDS
        ) if inventory_timeout is None else float(inventory_timeout)
        lease, lease_error = self._read_lease()
        targeted_lease_state = (
            self._targeted_lease_state(lease)
            if lease is not None and not lease_error
            else None
        )
        processes, inventory = self._collect_processes(
            require_fresh=require_fresh_inventory,
            timeout=timeout,
        )
        if lease_error:
            lease_status = {"state": "uncertain", "reason": lease_error}
        elif lease is None:
            lease_status = {"state": "absent", "reason": "no solver lease exists"}
        elif not inventory["complete"]:
            if targeted_lease_state and targeted_lease_state["state"] in {"active", "stale"}:
                lease_status = {
                    **targeted_lease_state,
                    "identity_source": "targeted_process_probe",
                    "lease": lease,
                }
            else:
                lease_status = {
                    "state": "uncertain",
                    "reason": "process inventory is incomplete and targeted lease identity is unavailable",
                    "identity_source": "targeted_process_probe_unavailable",
                    "lease": lease,
                }
        else:
            lease_status = {**self._lease_state(lease, processes), "lease": lease}
        external = self._external_solver_processes(processes, lease)
        full_collision_inventory = {
            "state": inventory.get("state", "complete")
            if inventory["complete"]
            else inventory.get("state", "pending"),
            "complete": bool(inventory["complete"]),
            "targeted_lease_identity": (
                targeted_lease_state.get("state")
                if targeted_lease_state is not None
                else "not_requested"
            ),
            "collision_decision": (
                "verified" if inventory["complete"] else "fail_closed_until_complete"
            ),
        }
        try:
            from src.jobs.manager import JobManager

            durable_jobs = JobManager(self.runtime_dir / "jobs", reconcile_on_start=False).summaries()
        except Exception as exc:
            durable_jobs = {
                "available": False,
                "reason": f"Cannot inspect durable jobs: {type(exc).__name__}: {exc}",
            }
        return {
            "success": True,
            "session": session_state or {"connected": False, "starting": False},
            "lease_path": str(self.lease_path),
            "lease": lease_status,
            "process_inventory": inventory,
            "full_collision_inventory": full_collision_inventory,
            "external_solver_processes": external,
            "collision": not inventory["complete"] or bool(external) or (
                lease_status["state"] in {"active", "uncertain"}
                and not lease_status.get("owned_by_current_process", False)
            ),
            "durable_jobs": durable_jobs,
        }

    def preflight(
        self,
        *,
        session_state: Optional[dict[str, Any]] = None,
        model_path: Optional[str] = None,
        output_path: Optional[str] = None,
        requested_version: Optional[str] = None,
        minimum_free_gb: float = 2.0,
    ) -> dict[str, Any]:
        import mph

        ownership = self.status(
            session_state=session_state,
            require_fresh_inventory=True,
        )
        blockers = []
        warnings = []
        lease = ownership["lease"]
        if not ownership["process_inventory"]["complete"]:
            blockers.append("host process inventory is incomplete")
        if ownership["external_solver_processes"]:
            blockers.append("external COMSOL/MPh solver process detected")
        if lease["state"] == "stale":
            blockers.append("stale solver lease requires explicit recovery")
        elif lease["state"] == "uncertain":
            blockers.append("solver lease cannot be validated safely")
        elif lease["state"] == "active" and not lease.get("owned_by_current_process", False):
            blockers.append("solver lease belongs to another active process")

        pointer_bits = 8 * __import__("struct").calcsize("P")
        if pointer_bits != 64:
            blockers.append("COMSOL requires a 64-bit Python architecture")
        effective_environment = settings_environment()
        java_home = effective_environment.get("JAVA_HOME") or effective_environment.get("JDK_HOME")
        try:
            backends = mph.discovery.find_backends()
        except Exception as exc:
            backends = []
            warnings.append(f"COMSOL backend discovery failed: {exc}")
        detected_backends = [
            {
                "name": str(item.get("name")),
                "root": str(item.get("root")),
                "jvm": str(item.get("jvm")),
            }
            for item in backends
        ]
        usable_jre = bool(java_home and Path(java_home).exists()) or any(
            Path(item["jvm"]).is_file() for item in detected_backends
        )
        if not usable_jre:
            blockers.append("No existing COMSOL JRE was found through the environment or MPh discovery")
        memory = psutil.virtual_memory()
        free_gb = memory.available / (1024**3)
        if free_gb < float(minimum_free_gb):
            blockers.append(f"available memory {free_gb:.2f} GiB is below {minimum_free_gb:.2f} GiB")
        if model_path and not Path(model_path).expanduser().is_file():
            blockers.append(f"model baseline does not exist: {model_path}")
        if output_path:
            parent = Path(output_path).expanduser().resolve().parent
            existing_parent = parent
            while not existing_parent.exists() and existing_parent != existing_parent.parent:
                existing_parent = existing_parent.parent
            if not existing_parent.is_dir() or not os.access(existing_parent, os.W_OK):
                blockers.append(f"output directory is not writable: {parent}")
        if requested_version:
            if not str(requested_version).startswith("6.4"):
                warnings.append(f"requested COMSOL version {requested_version!r} is outside the verified 6.4 target")
            if detected_backends and not any(
                item["name"].startswith(str(requested_version)) for item in detected_backends
            ):
                blockers.append(f"requested COMSOL version {requested_version!r} was not discovered")

        return {
            "success": not blockers,
            "ready": not blockers,
            "blockers": blockers,
            "warnings": warnings,
            "environment": {
                "platform": platform.platform(),
                "machine": platform.machine(),
                "pointer_bits": pointer_bits,
                "python": sys.executable,
                "mph_version": getattr(mph, "__version__", None),
                "java_home": java_home,
                "detected_comsol_backends": detected_backends,
                "requested_comsol_version": requested_version,
                "available_memory_gb": round(free_gb, 3),
                "model_path": model_path,
                "output_path": output_path,
            },
            "ownership": ownership,
        }

    def acquire(self, *, mode: str, model_path: Optional[str] = None) -> dict[str, Any]:
        status = self.status(require_fresh_inventory=True)
        lease_status = status["lease"]
        if (
            lease_status["state"] == "active"
            and lease_status.get("owned_by_current_process")
            and not status["collision"]
        ):
            return {"success": True, "acquired": False, "reused": True, "lease": lease_status["lease"]}
        if status["collision"] or lease_status["state"] == "stale":
            return {
                "success": False,
                "acquired": False,
                "error": "Solver ownership is not available; inspect solver_status.",
                "status": status,
            }
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        payload = {
            "schema_version": LEASE_SCHEMA_VERSION,
            "owner": self.owner,
            "mode": mode,
            "pid": self.pid,
            "parent_pid": self.parent_pid,
            "process_create_time": self.create_time,
            "command_line": self.command_line,
            "command_signature": self.command_signature,
            "model_path": model_path,
            "heartbeat_epoch": now,
            "created_at_epoch": now,
            "acquisition_id": uuid.uuid4().hex,
            "resource_ownership": "mcp_owned",
            "attached_server": None,
            "comsol_server_pids": [],
            "comsol_server_processes": [],
            "comsol_server_port": None,
        }
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        return self._create_lease(payload)

    def _create_lease(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        try:
            descriptor = os.open(self.lease_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            return {
                "success": False,
                "acquired": False,
                "error": "Another process acquired the solver lease concurrently.",
                "status": self.status(),
            }
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            self.lease_path.unlink(missing_ok=True)
            raise
        return {"success": True, "acquired": True, "reused": False, "lease": payload}

    def acquire_attached(
        self,
        attached_server: Any,
        *,
        listener_provider: Callable[[], list[dict[str, Any]]] = _system_listeners,
    ) -> dict[str, Any]:
        """Acquire the existing lease while preserving one exact external server."""
        from src.shared_session.contracts import (
            summarize_shared_listener_bindings,
        )
        from src.shared_session.identity import AttachedServerIdentity

        if not isinstance(attached_server, AttachedServerIdentity):
            raise ValueError("attached_server must be a normalized AttachedServerIdentity")
        processes, inventory = self._collect_processes(
            require_fresh=True,
            timeout=PROCESS_INVENTORY_MUTATION_TIMEOUT_SECONDS,
        )
        if not inventory["complete"]:
            return {
                "success": False,
                "acquired": False,
                "error": "Fresh process inventory is incomplete.",
                "process_inventory": inventory,
            }
        lease, lease_error = self._read_lease()
        if lease_error or lease is not None:
            return {
                "success": False,
                "acquired": False,
                "error": lease_error or "A solver lease already exists.",
            }
        server = next(
            (item for item in processes if item.get("pid") == attached_server.server_pid),
            None,
        )
        command = [str(part) for part in (server or {}).get("command_line") or []]
        if (
            server is None
            or server.get("create_time") is None
            or abs(float(server["create_time"]) - attached_server.server_process_create_time)
            > CREATE_TIME_TOLERANCE_SECONDS
            or _command_signature(command) != attached_server.server_command_signature
        ):
            return {
                "success": False,
                "acquired": False,
                "error": "Attached server process identity changed before lease acquisition.",
            }
        try:
            listeners = list(listener_provider())
        except Exception as exc:
            return {
                "success": False,
                "acquired": False,
                "error": f"Attached listener inventory failed: {type(exc).__name__}",
            }
        endpoint = attached_server.endpoint
        listener = summarize_shared_listener_bindings(
            listeners, endpoint=endpoint
        )
        if (
            not listener["stable"]
            or listener["owner_pid"] != attached_server.server_pid
            or listener["bind_scope"] != attached_server.listener_bind_scope
        ):
            return {
                "success": False,
                "acquired": False,
                "error": "Attached listener ownership changed before lease acquisition.",
            }
        attached_payload = {
            "owned": False,
            "identity_sha256": attached_server.identity_sha256,
            "host": endpoint.host,
            "port": endpoint.port,
            "listener_bind_scope": attached_server.listener_bind_scope,
            "server_pid": attached_server.server_pid,
            "process_create_time": attached_server.server_process_create_time,
            "command_signature": attached_server.server_command_signature,
        }
        synthetic_lease = {
            "pid": self.pid,
            "attached_server": attached_payload,
            "comsol_server_pids": [],
        }
        external = self._external_solver_processes(processes, synthetic_lease)
        if external:
            return {
                "success": False,
                "acquired": False,
                "error": "Unclassified external COMSOL/MPh process remains.",
                "external_solver_processes": external,
            }
        now = time.time()
        payload = {
            "schema_version": LEASE_SCHEMA_VERSION,
            "owner": self.owner,
            "mode": "attached-server",
            "pid": self.pid,
            "parent_pid": self.parent_pid,
            "process_create_time": self.create_time,
            "command_line": self.command_line,
            "command_signature": self.command_signature,
            "model_path": None,
            "heartbeat_epoch": now,
            "created_at_epoch": now,
            "acquisition_id": uuid.uuid4().hex,
            "resource_ownership": "external_user_owned_server",
            "attached_server": attached_payload,
            "comsol_server_pids": [],
            "comsol_server_processes": [],
            "comsol_server_port": None,
        }
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        return self._create_lease(payload)

    def heartbeat(
        self, *, model_path: Optional[str] = None, refresh_server_processes: bool = False
    ) -> bool:
        lease, original, error = self._read_lease_with_bytes()
        if error or not lease or int(lease.get("pid", -1)) != self.pid:
            return False
        if abs(float(lease.get("process_create_time", -1)) - self.create_time) > CREATE_TIME_TOLERANCE_SECONDS:
            return False
        acquisition_id = lease.get("acquisition_id")
        if not acquisition_id:
            return False
        if original is None:
            return False
        lease["heartbeat_epoch"] = time.time()
        if model_path is not None:
            lease["model_path"] = model_path
        if refresh_server_processes:
            processes, inventory = self._collect_processes(
                require_fresh=True,
                timeout=PROCESS_INVENTORY_MUTATION_TIMEOUT_SECONDS,
            )
            if not inventory["complete"]:
                return False
            parent_map = {
                int(item["pid"]): int(item.get("parent_pid") or 0)
                for item in processes
                if item.get("pid") is not None
            }
            servers = []
            for item in processes:
                pid = int(item.get("pid") or 0)
                if not pid or not self._is_descendant(pid, parent_map, self.pid):
                    continue
                name = str(item.get("name") or "").casefold()
                command = " ".join(str(part) for part in item.get("command_line") or []).casefold()
                if "mphserver" in name or (
                    name in {"java", "java.exe"} and "comsol" in command and "server" in command
                ):
                    servers.append(
                        {
                            "pid": pid,
                            "process_create_time": item.get("create_time"),
                            "command_signature": _command_signature(
                                [str(part) for part in item.get("command_line") or []]
                            ),
                        }
                    )
            servers.sort(key=lambda item: int(item["pid"]))
            lease["comsol_server_processes"] = servers
            # Keep v2 PID-only evidence readable, but never use it to act.
            lease["comsol_server_pids"] = [item["pid"] for item in servers]
        temporary = self.lease_path.with_name(f".{self.lease_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(lease, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            replaced, _reason = _replace_retry_if_unchanged(
                temporary, self.lease_path, original
            )
            return replaced
        finally:
            removed, reason = _unlink_retry(temporary, missing_ok=True)
            if not removed:
                raise RuntimeError(
                    f"Cannot clean solver lease temporary file: {reason}"
                )

    def release(self) -> dict[str, Any]:
        lease, original, error = self._read_lease_with_bytes()
        if error:
            return {"success": False, "released": False, "error": error}
        if lease is None:
            return {"success": True, "released": False, "message": "No solver lease exists."}
        if int(lease.get("pid", -1)) != self.pid or abs(
            float(lease.get("process_create_time", -1)) - self.create_time
        ) > CREATE_TIME_TOLERANCE_SECONDS:
            return {"success": False, "released": False, "error": "Refusing to release a foreign solver lease."}
        if not lease.get("acquisition_id"):
            return {"success": False, "released": False, "error": "Refusing to release a legacy lease without acquisition ID."}
        if original is None:
            return {"success": True, "released": False, "message": "No solver lease exists."}
        removed, reason = _unlink_retry(
            self.lease_path, missing_ok=True, expected_bytes=original
        )
        if not removed:
            detail = "Lease changed before release; retry status." if reason == "changed" else f"Cannot release solver lease: {reason}"
            return {"success": False, "released": False, "error": detail}
        return {"success": True, "released": True}

    def recover_stale(self) -> dict[str, Any]:
        lease, original, error = self._read_lease_with_bytes()
        if error:
            return {"success": False, "recovered": False, "error": error}
        if lease is None:
            return {"success": True, "recovered": False, "message": "No solver lease exists."}
        if original is None:
            return {"success": True, "recovered": False, "message": "No solver lease exists."}
        processes, inventory = self._collect_processes(
            require_fresh=True,
            timeout=PROCESS_INVENTORY_MUTATION_TIMEOUT_SECONDS,
        )
        if not inventory["complete"]:
            return {
                "success": False,
                "recovered": False,
                "error": "Process inventory is incomplete; refusing stale lease recovery.",
                "process_inventory": inventory,
            }
        state = self._lease_state(lease, processes)
        if state["state"] != "stale":
            return {
                "success": False,
                "recovered": False,
                "error": f"Lease is {state['state']}; only a proven stale lease may be removed.",
                "lease_state": state,
            }
        if not lease.get("acquisition_id"):
            return {"success": False, "recovered": False, "error": "Lease has no acquisition ID; refusing stale recovery."}
        removed, reason = _unlink_retry(
            self.lease_path, missing_ok=False, expected_bytes=original
        )
        if not removed:
            detail = "Lease changed during recovery; retry status." if reason == "changed" else f"Cannot recover stale solver lease: {reason}"
            return {"success": False, "recovered": False, "error": detail}
        return {"success": True, "recovered": True, "reason": state["reason"]}


ownership_manager = SolverOwnership()


def register_ownership_tools(mcp: FastMCP) -> None:
    """Register read-only ownership/preflight and explicit stale recovery tools."""

    @mcp.tool()
    def solver_status() -> dict:
        """Report MCP session, solver lease, process collisions, and job availability without starting COMSOL."""
        from .session import session_manager

        return measured_call(
            "solver_status",
            lambda: ownership_manager.status(
                session_state=session_manager.get_status()
            ),
        )

    @mcp.tool()
    def solver_preflight(
        model_path: Optional[str] = None,
        output_path: Optional[str] = None,
        requested_version: Optional[str] = None,
        minimum_free_gb: float = 2.0,
    ) -> dict:
        """Validate architecture, JRE, memory, paths, and ownership without starting COMSOL."""
        from .session import session_manager

        return measured_call(
            "solver_preflight",
            lambda: ownership_manager.preflight(
                session_state=session_manager.get_status(),
                model_path=model_path,
                output_path=output_path,
                requested_version=requested_version,
                minimum_free_gb=minimum_free_gb,
            ),
        )

    @mcp.tool()
    def solver_recover_stale_lease() -> dict:
        """Remove only a lease proven stale by PID and process-creation evidence; never kill a process."""
        return measured_call(
            "solver_recover_stale_lease",
            ownership_manager.recover_stale,
        )
