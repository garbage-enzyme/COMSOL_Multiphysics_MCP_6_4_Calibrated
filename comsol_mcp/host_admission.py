"""Host admission lock for transport selection (0.8.2 §10 startup gate).

One host per canonical ownership domain (the runtime directory). Both the
Python entry point and the Rust shim acquire this lock before serving MCP
requests. The lock is separate from the solver lease: the shim never
acquires, refreshes, releases or recovers a solver lease.

File: ``<runtime>/host_admission.lock``.
Schema: ``comsol_mcp.host_admission`` version ``1.1.0``; ``1.0.0`` records
from development builds stay readable and are judged by identity only.

Rules (shared with ``shim/src/admission.rs``):

* Publication is exclusive (temporary file + hard link), so a reader never
  observes a partial record.
* Release removes only the exact bytes and file identity this process
  published. A replacement record is never deleted.
* Owner identity is ``pid`` + ``process_create_time`` (+ the command
  signature when readable). A matching birth time with a different command
  signature is ``uncertain``, never ``stale``: the two entry points compute
  signatures with different argument parsers.
* Stale recovery requires verified cleanup: every recorded owned process is
  absent, no live direct child of a recorded process remains except a
  verified durable job worker, and the solver lease shows no live
  non-job owner and no live recorded COMSOL server. Missing evidence is
  ``uncertain`` and refuses admission; it is never treated as absence.
* A stale record whose containment cannot be read or is absent cannot
  prove its descendants are gone: a record without a containment identity
  (only 1.0.0 development builds wrote those), or a Windows record whose
  job name no longer opens (the object dies with its last handle, not its
  last member). Automatic recovery, takeover and dependent startup are
  refused with ``admission_state_uncertain`` on every platform (caller
  decision 2026-11-10); the record is kept, nothing is stopped, and the
  diagnostic names the explicit operator recovery flow (``--show`` /
  ``--recover``).
* The operator's confirmation covers only that gap. Explicit recovery still
  refuses every descendant the process inventory can attribute to the
  previous host (parent links; on POSIX also its own session), and any other
  unreadable evidence stays UNKNOWN. Its receipt records machine-verified
  evidence apart from what the confirmation covers.
* A fresh admission also checks the solver lease, so a live retained-version
  host that holds the lease blocks a new host in the same domain.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from comsol_mcp.durable.io import atomic_write_bytes_exclusive, unlink_if_content

HOST_ADMISSION_SCHEMA = "comsol_mcp.host_admission"
HOST_ADMISSION_VERSION = "1.1.0"
HOST_ADMISSION_RECOVERY_SCHEMA = "comsol_mcp.host_admission_recovery"
# 1.1.0 keeps machine evidence and the operator confirmation apart.
HOST_ADMISSION_RECOVERY_VERSION = "1.1.0"
# Shared with shim/src/admission.rs: the reason code and these texts are the
# operator-facing contract; the details carry the exact values.
OPERATOR_RECOVERY_HINT = (
    "the record was kept; confirm independently that nothing from the previous "
    "host remains, then run "
    "'python -m comsol_mcp.host_admission --show <runtime-dir>' for the exact "
    "recovery command"
)
LEGACY_RECORD_UNKNOWN_MESSAGE = (
    "the stale admission record has no containment identity; a descendant of "
    "the previous host that outlived its own parent cannot be ruled out, so "
    + OPERATOR_RECOVERY_HINT
)
READABLE_VERSIONS = ("1.0.0", HOST_ADMISSION_VERSION)
HOST_ADMISSION_FILE = "host_admission.lock"
SOLVER_LEASE_FILE = "solver_owner.json"
CREATE_TIME_TOLERANCE_SECONDS = 0.05
MAX_DOCUMENT_BYTES = 64 * 1024
MAX_JOB_DIRECTORIES = 4096
LEASE_READ_ATTEMPTS = 3
LEASE_READ_RETRY_SECONDS = 0.05
TRANSPORT_MODES = ("python", "shim")

WORKER_FLAG_ENV = "COMSOL_MCP_SHIM_WORKER"
WORKER_HOST_PID_ENV = "COMSOL_MCP_SHIM_HOST_PID"
WORKER_TOKEN_ENV = "COMSOL_MCP_SHIM_WORKER_TOKEN"  # noqa: S105 - a variable name

# The shim binary this package accepts. ``shim/Cargo.toml`` carries the
# same value; a development-kit test pins the two together.
EXPECTED_SHIM_VERSION = "0.8.2-dev"

_HEX64 = re.compile(r"[0-9a-f]{64}")
_JOB_TERMINAL_STATES = frozenset({"completed", "failed", "interrupted", "cancelled"})
# Console hosts serve a console and hold no work of their own.
_CONSOLE_HOSTS = frozenset({"conhost.exe", "openconsole.exe"})


class HostAdmissionError(RuntimeError):
    """Host admission or worker binding was refused."""

    def __init__(self, reason_code: str, message: str, details: dict[str, Any] | None = None):
        self.reason_code = reason_code
        self.details = details or {}
        super().__init__(message)


@dataclass(frozen=True)
class AdmissionClaim:
    """The exact record this process published, needed for a safe release.

    ``containment_handle`` keeps the host's named job object alive for the
    host lifetime (Windows). It is not equated: identity is the record.
    """

    path: Path
    document: dict[str, Any]
    payload: bytes
    file_identity: tuple[int, int]
    containment_handle: Any = None

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AdmissionClaim):
            return NotImplemented
        return (
            self.path,
            self.payload,
            self.file_identity,
            self.document,
        ) == (other.path, other.payload, other.file_identity, other.document)

    def __hash__(self) -> int:
        return hash((str(self.path), self.payload, self.file_identity))


def expected_shim_platforms() -> tuple[str, ...]:
    """Target triples this host can execute, in preference order."""
    import platform

    machine = platform.machine().lower()
    if sys.platform == "win32" and machine in {"amd64", "x86_64"}:
        return ("x86_64-pc-windows-msvc", "x86_64-pc-windows-gnu")
    if sys.platform.startswith("linux") and machine in {"x86_64", "amd64"}:
        return ("x86_64-unknown-linux-gnu",)
    return ()


def lock_path_for(runtime_dir: str | Path) -> Path:
    return Path(runtime_dir) / HOST_ADMISSION_FILE


def command_signature(cmdline: list[str]) -> str:
    """SHA-256 of the argument vector joined by NUL bytes (contract §3)."""
    return hashlib.sha256("\0".join(cmdline).encode("utf-8", errors="replace")).hexdigest()


def same_path(left: str | os.PathLike[str] | None, right: str | os.PathLike[str] | None) -> bool:
    """Compare two filesystem paths by their resolved, case-normalized form."""
    if not left or not right:
        return False
    try:
        first = os.path.normcase(os.path.realpath(os.fspath(left)))
        second = os.path.normcase(os.path.realpath(os.fspath(right)))
    except OSError, ValueError:
        return False
    return first == second


def _psutil() -> Any:
    try:
        import psutil
    except ImportError as exc:  # pragma: no cover - psutil is a hard dependency
        raise HostAdmissionError(
            "admission_state_uncertain",
            "process identity evidence requires psutil",
        ) from exc
    return psutil


def process_identity(pid: int | None = None) -> dict[str, Any]:
    """Capture the birth identity and executable of one process."""
    psutil = _psutil()
    target = os.getpid() if pid is None else pid
    try:
        proc = psutil.Process(target)
        with proc.oneshot():
            create_time = proc.create_time()
            cmdline = list(proc.cmdline() or [])
            try:
                executable = proc.exe() or None
            except psutil.AccessDenied, psutil.ZombieProcess:
                executable = None
    except psutil.Error as exc:
        raise HostAdmissionError(
            "admission_state_uncertain",
            f"cannot read the identity of process {target}: {exc}",
        ) from exc
    if not cmdline and target == os.getpid():
        cmdline = [sys.executable, *sys.argv]
    if not cmdline:
        raise HostAdmissionError(
            "admission_state_uncertain",
            f"the command line of process {target} is unavailable",
        )
    return {
        "pid": target,
        "process_create_time": create_time,
        "command_signature": command_signature(cmdline),
        "executable": executable,
    }


def _client_identity(pid: int) -> dict[str, Any] | None:
    """Best-effort identity of the launching client (the parent process)."""
    psutil = _psutil()
    try:
        parent = psutil.Process(pid).parent()
        if parent is None:
            return None
        with parent.oneshot():
            return {
                "pid": parent.pid,
                "process_create_time": parent.create_time(),
                "name": parent.name(),
            }
    except psutil.Error:
        return None


def _identity_fields(record: Mapping[str, Any]) -> tuple[int, float, str | None] | None:
    pid = record.get("pid")
    created = record.get("process_create_time")
    signature = record.get("command_signature")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if (
        isinstance(created, bool)
        or not isinstance(created, (int, float))
        or not math.isfinite(float(created))
    ):
        return None
    if signature is not None and (
        not isinstance(signature, str) or _HEX64.fullmatch(signature) is None
    ):
        return None
    return pid, float(created), signature


def classify_process(
    record: Mapping[str, Any],
    *,
    signature_mismatch: str = "uncertain",
    check_signature: bool = True,
) -> tuple[str, str]:
    """Classify one recorded process identity: (state, reason).

    ``state`` is ``active``, ``stale`` or ``uncertain``. Access denial and
    unreadable evidence are ``uncertain``; only a missing PID, a reused PID
    or an exited process is ``stale``.
    """
    fields = _identity_fields(record)
    if fields is None:
        return "uncertain", "the recorded process identity is invalid"
    pid, created, signature = fields
    psutil = _psutil()
    try:
        proc = psutil.Process(pid)
        with proc.oneshot():
            actual = proc.create_time()
            if abs(actual - created) > CREATE_TIME_TOLERANCE_SECONDS:
                return "stale", "the PID now belongs to a different process"
            if proc.status() == psutil.STATUS_ZOMBIE:
                return "stale", "the process exited and awaits reaping"
            if check_signature and signature is not None:
                cmdline = list(proc.cmdline() or [])
                if not cmdline:
                    return "uncertain", "the process command line is unavailable"
                if command_signature(cmdline) != signature:
                    return signature_mismatch, "the process command line differs from the record"
    except psutil.NoSuchProcess:
        return "stale", "the process no longer exists"
    except psutil.ZombieProcess:
        return "stale", "the process exited and awaits reaping"
    except psutil.AccessDenied:
        return "uncertain", "access to the process identity was denied"
    return "active", "PID, creation time and command line match"


def _read_bytes(path: Path, *, reason: str) -> bytes | None:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HostAdmissionError(
            reason,
            f"{path} cannot be read: {exc}",
            {"path": str(path)},
        ) from exc
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise HostAdmissionError(
            "admission_lock_corrupt",
            f"{path} exceeds {MAX_DOCUMENT_BYTES} bytes",
            {"path": str(path)},
        )
    return raw


def parse_lock(raw: bytes, *, path: Path | None = None) -> dict[str, Any]:
    """Parse one admission record; refuse anything but the declared schema."""
    try:
        lock = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError, ValueError:
        raise HostAdmissionError(
            "admission_lock_corrupt",
            "the host admission lock is not valid UTF-8 JSON; inspect it before starting a host",
            {"path": str(path) if path else None},
        ) from None
    if not isinstance(lock, dict):
        raise HostAdmissionError(
            "admission_lock_corrupt",
            "the host admission lock is not a JSON object",
            {"path": str(path) if path else None},
        )
    if (
        lock.get("schema_name") != HOST_ADMISSION_SCHEMA
        or lock.get("schema_version") not in READABLE_VERSIONS
    ):
        raise HostAdmissionError(
            "admission_lock_schema_unsupported",
            f"the host admission lock declares {lock.get('schema_name')!r} "
            f"{lock.get('schema_version')!r}; this host reads "
            f"{HOST_ADMISSION_SCHEMA} {', '.join(READABLE_VERSIONS)}",
            {"path": str(path) if path else None},
        )
    return lock


def document_bytes(document: Mapping[str, Any]) -> bytes:
    """Canonical record bytes: indent 2, sorted keys, final newline."""
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def owner_summary(lock: Mapping[str, Any]) -> dict[str, Any]:
    """User-facing identity of a recorded host."""
    return {
        "owner_pid": lock.get("pid"),
        "owner_process_create_time": lock.get("process_create_time"),
        "owner_transport": lock.get("transport_mode"),
        "owner_executable": lock.get("executable"),
        "owner_client": lock.get("client"),
        "owner_settings": lock.get("settings"),
        "owner_acquired_at": lock.get("acquired_at_epoch"),
    }


def _process_inventory() -> list[tuple[int, int | None, float | None, str]]:
    """Every live process as (pid, ppid, create_time, name)."""
    psutil = _psutil()
    rows: list[tuple[int, int | None, float | None, str]] = []
    try:
        for proc in psutil.process_iter(["pid", "ppid", "create_time", "name"]):
            info = proc.info
            rows.append(
                (
                    int(info["pid"]),
                    info.get("ppid"),
                    info.get("create_time"),
                    str(info.get("name") or ""),
                )
            )
    except psutil.Error as exc:
        raise HostAdmissionError(
            "admission_state_uncertain",
            f"the process inventory is incomplete: {exc}",
        ) from exc
    return rows


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > MAX_DOCUMENT_BYTES:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError, ValueError:
        return None
    return value if isinstance(value, dict) else None


def independent_owner_identities(runtime_dir: str | Path) -> dict[int, tuple[float, str]]:
    """Recorded independent owners of one domain: ``pid -> (create_time, role)``.

    Durable job workers of non-terminal jobs, cancel coordinators and wall
    watchdogs outlive their host by contract, so a stale host's live children
    that match these exact birth identities are not cleanup debt.
    """
    owners: dict[int, tuple[float, str]] = {}
    jobs = Path(runtime_dir) / "jobs"
    if not jobs.is_dir():
        return owners
    try:
        entries = list(itertools.islice(jobs.iterdir(), MAX_JOB_DIRECTORIES + 1))
    except OSError as exc:
        raise HostAdmissionError(
            "admission_state_uncertain",
            f"the durable job store cannot be listed: {exc}",
        ) from exc
    if len(entries) > MAX_JOB_DIRECTORIES:
        raise HostAdmissionError(
            "admission_state_uncertain",
            f"the durable job store holds more than {MAX_JOB_DIRECTORIES} entries",
        )

    def add(record: Any, pid_key: str, time_key: str, role: str) -> None:
        if not isinstance(record, dict):
            return
        pid = record.get(pid_key)
        created = record.get(time_key)
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return
        if isinstance(created, bool) or not isinstance(created, (int, float)):
            return
        owners[pid] = (float(created), role)

    for directory in entries:
        state = _read_json_object(directory / "state.json")
        if state is not None:
            if state.get("status") not in _JOB_TERMINAL_STATES:
                add(state, "worker_pid", "worker_process_create_time", "durable_job_worker")
            cancel = state.get("cancel")
            if isinstance(cancel, dict):
                add(cancel.get("coordinator"), "pid", "process_create_time", "cancel_coordinator")
        watchdog = _read_json_object(directory / "wall-watchdog.json")
        if watchdog is not None:
            add(watchdog.get("watchdog_identity"), "pid", "process_create_time", "wall_watchdog")
    return owners


def _command_line(pid: int) -> list[str]:
    psutil = _psutil()
    try:
        return list(psutil.Process(pid).cmdline() or [])
    except psutil.NoSuchProcess:
        return []
    except psutil.Error as exc:
        raise HostAdmissionError(
            "admission_state_uncertain",
            f"the command line of process {pid} cannot be read: {exc}",
            {"pid": pid},
        ) from exc


def _independent_role(
    pid: int,
    created: float,
    name: str,
    owners: Mapping[int, tuple[float, str]],
    children: Mapping[int, list[tuple[int, float | None]]],
) -> str | None:
    """Why one live child of a stale host is not cleanup debt, or None.

    Only exact recorded birth identities qualify (plus a console host, which
    serves a console and holds no work). A virtual-environment launcher
    qualifies when its single child is a recorded owner started with the
    launcher's exact command line. Anything else stays cleanup debt, for
    example a settings window opened by the stale host: close it first.
    """
    if name.lower() in _CONSOLE_HOSTS:
        return "console_host"
    owner = owners.get(pid)
    if owner is not None and abs(owner[0] - created) <= CREATE_TIME_TOLERANCE_SECONDS:
        return owner[1]
    kids = children.get(pid, [])
    if len(kids) != 1:
        return None
    kid_pid, kid_created = kids[0]
    kid_owner = owners.get(kid_pid)
    if (
        kid_created is None
        or kid_owner is None
        or abs(kid_owner[0] - kid_created) > CREATE_TIME_TOLERANCE_SECONDS
    ):
        return None
    cmdline = _command_line(pid)
    if not cmdline or _command_line(kid_pid) != cmdline:
        return None
    return f"{kid_owner[1]}_launcher"


def solver_lease_evidence(runtime_dir: str | Path) -> dict[str, Any]:
    """Read-only solver-lease check shared by fresh admission and recovery.

    A live non-job lease owner is another host (for example a retained
    0.8.1 host) and refuses admission. A live durable job is an independent
    owner and is reported, not refused. A stale lease is acceptable only when
    every recorded COMSOL server is verifiably absent; the lease itself stays
    in place for the explicit 0.8.1 recovery tool.
    """
    path = Path(runtime_dir) / SOLVER_LEASE_FILE
    lease: Any = None
    problem = "unknown"
    for attempt in range(LEASE_READ_ATTEMPTS):
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return {"state": "absent"}
        except OSError as exc:
            problem = str(exc)
        else:
            try:
                lease = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                problem = f"not valid JSON: {exc}"
            else:
                if isinstance(lease, dict):
                    break
                problem = "not a JSON object"
        if attempt + 1 < LEASE_READ_ATTEMPTS:
            time.sleep(LEASE_READ_RETRY_SECONDS)
    else:
        raise HostAdmissionError(
            "admission_state_uncertain",
            f"the solver lease cannot be read ({problem}); ownership cannot be verified",
            {"path": str(path)},
        )
    fields = _identity_fields(lease)
    if fields is None or lease.get("command_signature") is None:
        raise HostAdmissionError(
            "admission_state_uncertain",
            "the solver lease identity is invalid; ownership cannot be verified",
            {"path": str(path)},
        )
    state, reason = classify_process(lease, signature_mismatch="stale")
    summary = {
        "pid": lease.get("pid"),
        "mode": lease.get("mode"),
        "owner": lease.get("owner"),
        "reason": reason,
    }
    if state == "uncertain":
        raise HostAdmissionError(
            "admission_state_uncertain",
            f"the solver lease owner cannot be verified: {reason}",
            {"path": str(path), **summary},
        )
    if state == "active":
        if lease.get("mode") == "durable-job":
            return {"state": "active_independent_job", **summary}
        raise HostAdmissionError(
            "admission_domain_busy",
            f"a live solver owner (PID {lease.get('pid')}, mode {lease.get('mode')}) "
            "holds this ownership domain; close its client normally first",
            {"path": str(path), **summary},
        )
    servers = lease.get("comsol_server_processes") or []
    if not isinstance(servers, list):
        raise HostAdmissionError(
            "admission_state_uncertain",
            "the stale solver lease records an invalid server list",
            {"path": str(path)},
        )
    verified: list[int] = []
    for server in servers:
        if not isinstance(server, dict):
            raise HostAdmissionError(
                "admission_state_uncertain",
                "the stale solver lease records an invalid server identity",
                {"path": str(path)},
            )
        server_state, server_reason = classify_process(server, signature_mismatch="stale")
        if server_state == "active":
            raise HostAdmissionError(
                "admission_cleanup_unverified",
                f"COMSOL server PID {server.get('pid')} recorded by the stale solver "
                "lease is still running",
                {"path": str(path), "server_pid": server.get("pid")},
            )
        if server_state == "uncertain":
            raise HostAdmissionError(
                "admission_state_uncertain",
                f"COMSOL server PID {server.get('pid')} cannot be verified: {server_reason}",
                {"path": str(path), "server_pid": server.get("pid")},
            )
        verified.append(int(server["pid"]))
    return {
        "state": "stale",
        **summary,
        "servers_verified_absent": verified,
        "note": "the stale lease remains until explicit recovery (solver_recover_stale_lease)",
    }


# What the inventory cannot attribute once the containment is out of reach.
_UNATTRIBUTABLE_WINDOWS = (
    "a descendant whose own parent exited before it and that does not keep the "
    "previous host's own console in use: its parent link names a process that is "
    "gone, so the inventory cannot attribute it to the previous host"
)
_CONSOLE_IN_USE = (
    "a console host lives only while a process is attached to its console, so the "
    "previous host's console is still in use by a live process"
)
_UNATTRIBUTABLE_POSIX = (
    "a descendant that left the previous host's session: orphans are reparented "
    "here, so the inventory cannot attribute it to the previous host"
)


def _attributable_descendants(
    roots: list[dict[str, Any]],
    *,
    ignore_pids: frozenset[int],
    owners: Mapping[int, tuple[float, str]],
    context: str,
    console_in_use_is_evidence: bool = False,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Live descendants the process inventory can attribute to a stale host.

    Used when no containment can be read. Windows keeps a dead parent's PID
    in its children's records, so every live direct child of the host or of
    a recorded owned process is found. ``console_in_use_is_evidence`` (the
    explicit recovery past a lost containment) also refuses a live console
    host among them: it outlives its console's last attached process by no
    more than a moment (measured), and independent owners always start with
    their own console, so a live one means a process still attached to the
    previous host's console. POSIX reparents orphans, so the host's
    own session is checked too: a session id equal to the host PID can only
    have been created by the host, and Linux never reuses a PID that is
    still in use as a session id. A live process whose parent, birth time or
    session cannot be read is UNKNOWN, never absent.

    Returns ``(scope, descendants, independent_owners)``.
    """
    inventory = _process_inventory()
    for pid, ppid, _created, name in inventory:
        if isinstance(ppid, int) or pid in ignore_pids:
            continue
        raise HostAdmissionError(
            "admission_state_uncertain",
            f"process {pid} ({name}) has an unreadable parent; ancestry cannot "
            f"be established {context}",
            {"pid": pid},
        )
    current_birth = {pid: created for pid, _ppid, created, _name in inventory}
    children: dict[int, list[tuple[int, float | None]]] = {}
    for pid, ppid, created, _name in inventory:
        if isinstance(ppid, int):
            children.setdefault(ppid, []).append((pid, created))
    descendants: list[dict[str, Any]] = []
    independent: list[dict[str, Any]] = []
    for pid, ppid, created, name in inventory:
        if pid in ignore_pids:
            continue
        for root in roots:
            if ppid != root["pid"]:
                continue
            if created is None:
                raise HostAdmissionError(
                    "admission_state_uncertain",
                    f"process {pid} ({name}) has an unreadable creation time; it "
                    "cannot be ordered against the previous host",
                    {"pid": pid},
                )
            if created + CREATE_TIME_TOLERANCE_SECONDS < root["process_create_time"]:
                continue  # Born before the recorded process: not its child.
            holder = current_birth.get(root["pid"])
            if (
                holder is not None
                and abs(holder - root["process_create_time"]) > CREATE_TIME_TOLERANCE_SECONDS
                and created + CREATE_TIME_TOLERANCE_SECONDS >= holder
            ):
                continue  # A child of the process that now reuses the PID.
            in_use = console_in_use_is_evidence and name.lower() in _CONSOLE_HOSTS
            role = (
                None if in_use else _independent_role(pid, float(created), name, owners, children)
            )
            if role is not None:
                independent.append({"pid": pid, "name": name, "role": role})
                continue
            descendants.append(
                {
                    "pid": pid,
                    "name": name,
                    "process_create_time": created,
                    "parent_pid": root["pid"],
                    "parent_role": root["role"],
                    **({"role": "console_host_in_use"} if in_use else {}),
                }
            )
    scope = "direct children of the host and of every recorded owned process"
    if os.name == "nt":
        if console_in_use_is_evidence:
            scope += ", including a console host that keeps the host's own console in use"
        return scope, descendants, independent
    host = roots[0]
    if host["pid"] in current_birth:
        # A live process holds the host PID with another birth (the host is
        # stale): any session with that id is the reuser's, since the PID
        # could not have been reused while the host's session had members.
        return (
            f"{scope}; the host PID now belongs to another process, so no live "
            "session is the host's",
            descendants,
            independent,
        )
    getsid = getattr(os, "getsid", None)
    if getsid is None:
        raise HostAdmissionError(
            "admission_state_uncertain",
            "this platform cannot read process sessions; descendants of the "
            "previous host cannot be attributed",
        )
    listed = {item["pid"] for item in descendants}
    for pid, _ppid, created, name in inventory:
        if pid in ignore_pids or pid in listed:
            continue
        try:
            session = getsid(pid)
        except ProcessLookupError:
            continue  # Exited since the inventory.
        except OSError as exc:
            raise HostAdmissionError(
                "admission_state_uncertain",
                f"the session of process {pid} ({name}) cannot be read: {exc}",
                {"pid": pid},
            ) from exc
        if session != host["pid"]:
            continue
        if created is None:
            raise HostAdmissionError(
                "admission_state_uncertain",
                f"process {pid} ({name}) has an unreadable creation time; it "
                "cannot be ordered against the previous host",
                {"pid": pid},
            )
        if created + CREATE_TIME_TOLERANCE_SECONDS < host["process_create_time"]:
            continue  # Older than the host: not started by it.
        role = _independent_role(pid, float(created), name, owners, children)
        if role is not None:
            independent.append({"pid": pid, "name": name, "role": role})
            continue
        descendants.append(
            {
                "pid": pid,
                "name": name,
                "process_create_time": created,
                "session": host["pid"],
            }
        )
    return f"{scope}, and every member of the host's own session", descendants, independent


def verify_stale_cleanup(
    lock: Mapping[str, Any],
    runtime_dir: str | Path,
    *,
    ignore_pids: frozenset[int] = frozenset(),
    for_explicit_recovery: bool = False,
) -> dict[str, Any]:
    """Prove that a stale host left nothing running. Raises on any gap.

    A record with a readable containment identity is judged by kernel
    membership. A record without one is ``admission_state_uncertain`` on
    every platform (caller decision 2026-11-10: only 1.0.0 development
    builds wrote those), as is a Windows record whose job name no longer
    opens.

    ``for_explicit_recovery`` (after independent operator confirmation)
    proceeds past exactly those two gaps, and only after checking every
    descendant the inventory can still attribute to the host (parent links
    of the host and its recorded processes; on POSIX also the host's own
    session; on Windows a console host keeping the host's own console in
    use); any such descendant still refuses, and every other unreadable
    observation stays UNKNOWN. The evidence then names what was not
    machine-verified. ``ignore_pids`` exempts the observing process and its
    ancestors.
    """
    runtime = Path(runtime_dir)
    owner = _identity_fields(lock)
    if owner is None:
        raise HostAdmissionError(
            "admission_state_uncertain",
            "the stale admission record has an invalid owner identity",
        )
    roots: list[dict[str, Any]] = [
        {"pid": owner[0], "process_create_time": owner[1], "role": "host"}
    ]
    owned = lock.get("owned_processes") or []
    if not isinstance(owned, list):
        raise HostAdmissionError(
            "admission_state_uncertain",
            "the stale admission record lists owned processes in an invalid form",
        )
    owned_evidence: list[dict[str, Any]] = []
    for record in owned:
        if not isinstance(record, dict) or _identity_fields(record) is None:
            raise HostAdmissionError(
                "admission_state_uncertain",
                "the stale admission record contains an invalid owned-process identity",
            )
        state, reason = classify_process(record, check_signature=False)
        if state == "active":
            raise HostAdmissionError(
                "admission_cleanup_unverified",
                f"{record.get('role', 'owned process')} PID {record['pid']} of the "
                "previous host is still running",
                {"pid": record["pid"], "role": record.get("role")},
            )
        if state == "uncertain":
            raise HostAdmissionError(
                "admission_state_uncertain",
                f"{record.get('role', 'owned process')} PID {record['pid']} cannot be "
                f"verified: {reason}",
                {"pid": record["pid"], "role": record.get("role")},
            )
        owned_evidence.append(
            {"pid": record["pid"], "role": record.get("role"), "state": state, "reason": reason}
        )
        roots.append(
            {
                "pid": record["pid"],
                "process_create_time": float(record["process_create_time"]),
                "role": record.get("role", "owned"),
            }
        )

    from comsol_mcp.host_containment import (
        ContainmentNameLost,
        UnknownContainment,
        containment_members,
    )

    containment = lock.get("containment")
    has_containment = isinstance(containment, dict) and bool(containment.get("kind"))
    if not has_containment and not for_explicit_recovery:
        # Caller decision 2026-11-10: identical on every platform. Missing
        # evidence, not a finding: the owned-process checks above have
        # already refused any positive evidence.
        raise HostAdmissionError(
            "admission_state_uncertain",
            LEGACY_RECORD_UNKNOWN_MESSAGE,
            {
                "path": str(runtime / HOST_ADMISSION_FILE),
                "containment": None,
                "recovery": "independent confirmation, then --show and --recover",
            },
        )

    owners = independent_owner_identities(runtime)
    descendants: list[dict[str, Any]] = []
    independent: list[dict[str, Any]] = []
    members: set[int] | None = None
    # Why the descendant evidence is incomplete. Only explicit recovery gets
    # past one of these two gaps, and only after the attributable check below.
    unavailable: str | None = None
    unattributable = _UNATTRIBUTABLE_WINDOWS if os.name == "nt" else _UNATTRIBUTABLE_POSIX
    if isinstance(containment, dict) and has_containment:
        try:
            members = containment_members(containment)
        except ContainmentNameLost as exc:
            # The expected state after a crash: the host held the only handle.
            if not for_explicit_recovery:
                raise HostAdmissionError(
                    "admission_state_uncertain",
                    f"the previous host's containment cannot be read: {exc}; "
                    + OPERATOR_RECOVERY_HINT,
                    {
                        "path": str(runtime / HOST_ADMISSION_FILE),
                        "containment": containment,
                        "recovery": "independent confirmation, then --show and --recover",
                    },
                ) from exc
            unavailable = f"the recorded containment cannot be read: {exc}"
        except UnknownContainment as exc:
            # Not the gap an operator confirmation covers: UNKNOWN for the
            # automatic and the explicit path alike.
            raise HostAdmissionError(
                "admission_state_uncertain",
                f"the previous host's containment cannot be read: {exc}",
            ) from exc
    else:
        unavailable = "the record has no containment identity"
    if members is not None:
        # Kernel-authoritative membership: every descendant the host kept, at
        # any depth, regardless of ppid readability or reparenting.
        scope = "kernel membership of the recorded host containment"
        inventory_by_pid = {
            pid: (created, name) for pid, _ppid, created, name in _process_inventory()
        }
        for pid in sorted(members):
            if pid in ignore_pids:
                continue
            created, name = inventory_by_pid.get(pid, (None, f"pid-{pid}"))
            if created is None:
                # A member the inventory cannot see is still in the
                # containment: alive and unnamed, so it is cleanup debt.
                descendants.append({"pid": pid, "name": name, "role": "member"})
                continue
            role = _independent_role(pid, float(created), name, owners, {})
            if role is not None:
                independent.append({"pid": pid, "name": name, "role": role})
                continue
            descendants.append({"pid": pid, "name": name, "role": "member"})
    else:
        # No readable containment: every descendant the inventory can still
        # attribute to the host is positive evidence, explicit recovery or not.
        scope, descendants, independent = _attributable_descendants(
            roots,
            ignore_pids=ignore_pids,
            owners=owners,
            context=(
                "while the recorded containment cannot be read"
                if has_containment
                else "for a record without containment evidence"
            ),
            console_in_use_is_evidence=unavailable is not None,
        )
    if descendants:
        listed = ", ".join(f"PID {item['pid']} ({item['name']})" for item in descendants)
        message = f"processes started by the previous host are still running: {listed}"
        if any(item.get("role") == "console_host_in_use" for item in descendants):
            message += f"; {_CONSOLE_IN_USE}"
        raise HostAdmissionError(
            "admission_cleanup_unverified",
            message,
            {"descendants": descendants},
        )
    lease = solver_lease_evidence(runtime)
    evidence: dict[str, Any] = {
        "owner": {"pid": owner[0], "process_create_time": owner[1]},
        "owned_processes": owned_evidence,
        "descendants": {
            "live": [],
            "independent_owners": independent,
            "scope": scope,
        },
        "solver_lease": lease,
        "listeners": {
            "method": "owned_process_absence",
            "note": "no recorded or descendant process is alive, so none can hold a listener",
        },
    }
    if unavailable is not None:
        # Explicit recovery only: say what the machine did not verify, and
        # do not infer listener absence from an incomplete descendant list.
        evidence["descendants"]["unavailable"] = unavailable
        evidence["descendants"]["not_machine_verified"] = unattributable
        evidence["listeners"] = {
            "method": "not_machine_verified",
            "note": (
                "the descendant evidence is incomplete, so listener absence is not "
                "machine-verified; it rests on the operator confirmation"
            ),
        }
    return evidence


def acquire(
    runtime_dir: str | Path,
    *,
    transport_mode: str,
    settings: Mapping[str, Any],
    pid: int | None = None,
    containment_handle_factory: Callable[[dict[str, Any]], Any] | None = None,
) -> AdmissionClaim:
    """Acquire host admission for one ownership domain.

    ``settings`` is the identity (``path`` and ``sha256``) of the one
    settings snapshot the caller validated. Raises HostAdmissionError on
    refusal; never terminates or takes over another host. The host adopts a
    containment identity (a named job object on Windows, a session on POSIX)
    and records it, so a later recovery enumerates every descendant from the
    kernel instead of trusting parent links. ``containment_handle_factory``
    (tests) replaces the adoption step; the claim keeps the handle alive.
    """
    if transport_mode not in TRANSPORT_MODES:
        raise ValueError(f"unknown transport mode {transport_mode!r}")
    runtime = Path(runtime_dir)
    runtime.mkdir(parents=True, exist_ok=True)
    path = runtime / HOST_ADMISSION_FILE
    me = process_identity(pid)

    raw = _read_bytes(path, reason="admission_lock_unreadable")
    recovered: dict[str, Any] | None = None
    if raw is not None:
        lock = parse_lock(raw, path=path)
        state, reason = classify_process(lock, signature_mismatch="uncertain")
        if state == "active":
            raise HostAdmissionError(
                "admission_domain_busy",
                f"another {lock.get('transport_mode')} host (PID {lock.get('pid')}) is "
                "active in this ownership domain; close its client normally and retry",
                owner_summary(lock),
            )
        if state == "uncertain":
            raise HostAdmissionError(
                "admission_state_uncertain",
                f"the recorded host (PID {lock.get('pid')}) cannot be verified: {reason}",
                owner_summary(lock),
            )
        evidence = verify_stale_cleanup(lock, runtime)
        if not unlink_if_content(path, raw):
            raise HostAdmissionError(
                "admission_domain_busy",
                "the admission record changed during stale recovery; another host may "
                "have been admitted",
                owner_summary(lock),
            )
        recovered = {
            "pid": lock.get("pid"),
            "process_create_time": lock.get("process_create_time"),
            "transport_mode": lock.get("transport_mode"),
            "reason": reason,
            "cleanup": evidence,
        }
    else:
        solver_lease_evidence(runtime)

    from comsol_mcp import host_containment

    factory = containment_handle_factory or host_containment.adopt_host_containment
    try:
        containment = host_containment.host_containment_id(pid=me["pid"])
        handle = factory(containment)
    except OSError as exc:
        raise HostAdmissionError(
            "admission_state_uncertain",
            f"this host cannot adopt its containment identity: {exc}",
        ) from exc
    try:
        # Prove the registry answers before the record names it.
        host_containment.containment_members(containment)
    except host_containment.UnknownContainment as exc:
        raise HostAdmissionError(
            "admission_state_uncertain",
            f"this host's containment identity cannot be read back: {exc}",
        ) from exc
    document: dict[str, Any] = {
        "schema_name": HOST_ADMISSION_SCHEMA,
        "schema_version": HOST_ADMISSION_VERSION,
        "state": "active",
        "transport_mode": transport_mode,
        "pid": me["pid"],
        "process_create_time": me["process_create_time"],
        "command_signature": me["command_signature"],
        "executable": me["executable"],
        "client": _client_identity(me["pid"]),
        "settings": {"path": settings.get("path"), "sha256": settings.get("sha256")},
        "domain": str(runtime.absolute()),
        "containment": containment,
        "shim": None,
        "owned_processes": [],
        "recovered_from": recovered,
        "acquired_at_epoch": time.time(),
    }
    payload = document_bytes(document)
    try:
        identity = atomic_write_bytes_exclusive(path, payload)
    except FileExistsError:
        raise HostAdmissionError(
            "admission_domain_busy",
            "another host acquired admission for this domain concurrently",
        ) from None
    return AdmissionClaim(
        path=path,
        document=document,
        payload=payload,
        file_identity=identity,
        containment_handle=handle,
    )


def release(claim: AdmissionClaim) -> bool:
    """Remove only the exact record (bytes and file identity) this host published."""
    return bool(unlink_if_content(claim.path, claim.payload, expected_identity=claim.file_identity))


def observer_lineage() -> frozenset[int]:
    """This process and its ancestors: an observer is never cleanup debt."""
    psutil = _psutil()
    lineage = {os.getpid()}
    try:
        process = psutil.Process()
        for _ in range(8):
            parent = process.parent()
            if parent is None:
                break
            lineage.add(parent.pid)
            process = parent
    except psutil.Error:
        pass
    return frozenset(lineage)


def status(runtime_dir: str | Path, *, assess_cleanup: bool = False) -> dict[str, Any]:
    """Read-only admission state for diagnostics and the settings GUI."""
    path = lock_path_for(runtime_dir)
    try:
        raw = _read_bytes(path, reason="admission_lock_unreadable")
    except HostAdmissionError as exc:
        return {
            "state": "uncertain",
            "reason_code": exc.reason_code,
            "reason": str(exc),
            "path": str(path),
        }
    if raw is None:
        return {"state": "absent", "path": str(path)}
    try:
        lock = parse_lock(raw, path=path)
    except HostAdmissionError as exc:
        return {
            "state": "uncertain",
            "reason_code": exc.reason_code,
            "reason": str(exc),
            "path": str(path),
        }
    try:
        state, reason = classify_process(lock, signature_mismatch="uncertain")
    except HostAdmissionError as exc:
        state, reason = "uncertain", str(exc)
    result: dict[str, Any] = {
        "state": state,
        "reason": reason,
        "path": str(path),
        "transport_mode": lock.get("transport_mode"),
        "pid": lock.get("pid"),
        "process_create_time": lock.get("process_create_time"),
        "executable": lock.get("executable"),
        "client": lock.get("client"),
        "settings": lock.get("settings"),
        "owned_processes": lock.get("owned_processes") or [],
        "acquired_at_epoch": lock.get("acquired_at_epoch"),
    }
    if state == "stale" and assess_cleanup:
        try:
            result["cleanup"] = {
                "state": "verified",
                "evidence": verify_stale_cleanup(lock, runtime_dir, ignore_pids=observer_lineage()),
            }
        except HostAdmissionError as exc:
            result["cleanup"] = {
                "state": "unverified",
                "reason_code": exc.reason_code,
                "reason": str(exc),
            }
    return result


def transport_setting_errors(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Settings errors that make the configured transport ambiguous."""
    errors = report.get("errors") or []
    return [
        error
        for error in errors
        if isinstance(error, dict)
        and (
            error.get("path") == "settings"
            or str(error.get("path", "")).startswith(("settings.transport", "settings.shim_binary"))
        )
    ]


def validate_transport_mode(
    settings: dict[str, Any],
    *,
    actual_mode: str,
    settings_source: str = "",
) -> None:
    """Refuse a host whose actual mode differs from the configured mode."""
    configured = settings.get("transport", "python")
    if configured == actual_mode:
        return
    raise HostAdmissionError(
        "transport_mode_mismatch",
        f"settings select transport '{configured}' but this host is "
        f"'{actual_mode}'. Correct the client launch command or change the "
        f"transport setting.",
        {
            "configured_mode": configured,
            "actual_mode": actual_mode,
            "settings_source": settings_source,
        },
    )


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_worker_binding(
    *,
    settings: Mapping[str, Any],
    settings_identity: Mapping[str, Any],
    runtime_dir: str | Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Prove that this process is the contained worker of the admitted shim.

    The worker exemption from host admission is granted only when every
    link verifies: the shim host recorded in this domain's admission lock is
    alive with the recorded birth time, it is this process's parent (or the
    parent of one interpreter launcher), its executable is the configured
    shim binary whose SHA-256, version and platform match the settings, the
    launch token matches the hash the host recorded, and the host admitted
    with the same settings snapshot this worker read. Any missing evidence
    refuses; an environment flag alone never bypasses admission.
    """
    environment = os.environ if environ is None else environ
    host_pid_text = environment.get(WORKER_HOST_PID_ENV, "")
    token = environment.get(WORKER_TOKEN_ENV, "")
    if not host_pid_text or not token:
        raise HostAdmissionError(
            "worker_binding_missing",
            f"{WORKER_FLAG_ENV} is set but the shim host identity or launch token is "
            "missing; only an admitted shim may launch its worker",
        )
    try:
        host_pid = int(host_pid_text)
    except ValueError:
        host_pid = 0
    if host_pid <= 0 or _HEX64.fullmatch(token) is None:
        raise HostAdmissionError(
            "worker_binding_invalid",
            "the shim host PID or launch token is malformed",
        )
    if settings.get("transport") != "shim":
        raise HostAdmissionError(
            "worker_binding_invalid",
            "the settings do not select the shim transport, so no shim worker is valid",
        )
    binary = settings.get("shim_binary") or {}
    for key in ("path", "sha256", "version", "platform"):
        if not binary.get(key):
            raise HostAdmissionError(
                "worker_binding_invalid",
                f"settings.shim_binary.{key} is missing",
            )

    path = lock_path_for(runtime_dir)
    raw = _read_bytes(path, reason="worker_binding_unverifiable")
    if raw is None:
        raise HostAdmissionError(
            "worker_binding_invalid",
            "no admitted shim host holds this ownership domain",
            {"path": str(path)},
        )
    lock = parse_lock(raw, path=path)
    shim = lock.get("shim") or {}
    checks = {
        "transport": lock.get("transport_mode") == "shim",
        "state": lock.get("state") == "active",
        "host_pid": lock.get("pid") == host_pid,
        "token": isinstance(shim, dict)
        and shim.get("worker_token_sha256") == hashlib.sha256(token.encode("ascii")).hexdigest(),
        "settings_sha256": (lock.get("settings") or {}).get("sha256")
        == settings_identity.get("sha256"),
        "settings_path": same_path(
            (lock.get("settings") or {}).get("path"), settings_identity.get("path")
        ),
        "binary_sha256": isinstance(shim, dict)
        and shim.get("executable_sha256") == binary.get("sha256"),
        "binary_version": isinstance(shim, dict) and shim.get("version") == binary.get("version"),
        "binary_platform": isinstance(shim, dict)
        and shim.get("platform") == binary.get("platform"),
        "binary_path": same_path(lock.get("executable"), binary.get("path")),
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise HostAdmissionError(
            "worker_binding_invalid",
            "the admission record does not bind this worker: " + ", ".join(failed),
            {"failed_checks": failed},
        )

    psutil = _psutil()
    try:
        state, reason = classify_process(lock, check_signature=False)
        if state != "active":
            raise HostAdmissionError(
                "worker_binding_invalid" if state == "stale" else "worker_binding_unverifiable",
                f"the recorded shim host is not verifiably alive: {reason}",
            )
        host = psutil.Process(host_pid)
        if not same_path(host.exe(), binary.get("path")):
            raise HostAdmissionError(
                "worker_binding_invalid",
                "the recorded shim host does not run the configured shim binary",
            )
        me = psutil.Process()
        parent = me.parent()
        if parent is None:
            raise HostAdmissionError("worker_binding_invalid", "this worker has no parent")
        chain = [parent.pid]
        if parent.pid != host_pid:
            launcher_name = Path(parent.exe() or "").name.lower()
            if not launcher_name.startswith("python"):
                raise HostAdmissionError(
                    "worker_binding_invalid",
                    "this worker's parent is neither the shim host nor an interpreter launcher",
                )
            if parent.ppid() != host_pid:
                raise HostAdmissionError(
                    "worker_binding_invalid",
                    "this worker does not descend from the admitted shim host",
                )
            if parent.create_time() + CREATE_TIME_TOLERANCE_SECONDS < float(
                lock["process_create_time"]
            ):
                raise HostAdmissionError(
                    "worker_binding_invalid",
                    "the interpreter launcher predates the shim host",
                )
            chain.append(host_pid)
        if me.create_time() + CREATE_TIME_TOLERANCE_SECONDS < float(lock["process_create_time"]):
            raise HostAdmissionError(
                "worker_binding_invalid",
                "this worker predates the shim host",
            )
    except psutil.AccessDenied as exc:
        raise HostAdmissionError(
            "worker_binding_unverifiable",
            f"process identity evidence was denied: {exc}",
        ) from exc
    except psutil.NoSuchProcess as exc:
        raise HostAdmissionError(
            "worker_binding_invalid",
            f"a process in the launch chain no longer exists: {exc}",
        ) from exc
    try:
        actual_sha256 = _sha256_file(binary["path"])
    except OSError as exc:
        raise HostAdmissionError(
            "worker_binding_unverifiable",
            f"the configured shim binary cannot be read: {exc}",
        ) from exc
    if actual_sha256 != binary["sha256"]:
        raise HostAdmissionError(
            "worker_binding_invalid",
            "the configured shim binary no longer matches its recorded SHA-256",
        )
    return {
        "host_pid": host_pid,
        "host_process_create_time": lock.get("process_create_time"),
        "chain": chain,
        "executable": lock.get("executable"),
        "executable_sha256": actual_sha256,
        "version": binary.get("version"),
        "platform": binary.get("platform"),
        "settings": dict(settings_identity),
        "token_verified": True,
    }


# ---------------------------------------------------------------------------
# Explicit operator recovery (caller decision 2026-11-10)
# ---------------------------------------------------------------------------

RECOVERY_ARCHIVE_DIRECTORY = "admission_recovered"


def recovery_preview(runtime_dir: str | Path) -> dict[str, Any]:
    """Read-only: the record's exact identity and its explicit recovery command."""
    runtime = Path(runtime_dir)
    path = lock_path_for(runtime)
    raw = _read_bytes(path, reason="admission_lock_unreadable")
    if raw is None:
        return {"state": "absent", "path": str(path)}
    digest = hashlib.sha256(raw).hexdigest()
    view = status(runtime, assess_cleanup=True)
    preview: dict[str, Any] = {
        "state": view.get("state"),
        "reason": view.get("reason"),
        "path": str(path),
        "record_sha256": digest,
        "previous_host": {key: view.get(key) for key in ("pid", "transport_mode")},
        "cleanup": view.get("cleanup"),
        "recovery_command": (
            f'python -m comsol_mcp.host_admission --recover "{runtime}" {digest} '
            '--confirmed-by "<who confirmed that nothing from the previous host remains>"'
        ),
    }
    if view.get("state") == "stale":
        # What --recover would decide now, so the operator sees any process
        # still attributable to the previous host before confirming anything.
        try:
            evidence = verify_stale_cleanup(
                parse_lock(raw, path=path),
                runtime,
                ignore_pids=observer_lineage(),
                for_explicit_recovery=True,
            )
        except HostAdmissionError as exc:
            preview["explicit_recovery"] = {
                "state": "refused",
                "reason_code": exc.reason_code,
                "reason": str(exc),
                "details": exc.details,
            }
        else:
            preview["explicit_recovery"] = {
                "state": "ready",
                "operator_confirmation_covers": confirmation_coverage(evidence),
            }
    return preview


def confirmation_coverage(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What an operator confirmation stands in for: the machine's gaps only.

    Empty when the machine verified every check itself.
    """
    descendants = evidence.get("descendants")
    if not isinstance(descendants, Mapping) or "unavailable" not in descendants:
        return []
    return [
        {
            "evidence": "descendants",
            "unavailable": descendants["unavailable"],
            "not_machine_verified": descendants.get("not_machine_verified"),
            "machine_verified_scope": descendants.get("scope"),
        },
        {
            "evidence": "listeners",
            "not_machine_verified": (
                "listener absence follows only from complete descendant evidence"
            ),
        },
    ]


def recover_after_independent_confirmation(
    runtime_dir: str | Path,
    *,
    record_sha256: str,
    confirmed_by: str,
) -> dict[str, Any]:
    """Archive and remove one stale record after independent confirmation.

    The confirmation covers only the evidence the machine cannot obtain: a
    Windows job name lost with its host, or a POSIX record without a
    containment identity. Every observable finding still refuses: a live or
    unverifiable recorded host, a live owned process, a live descendant that
    the containment or the process inventory still attributes to the host
    (parent links; on POSIX its own session), a live solver-lease owner or
    recorded server. Any other unreadable evidence stays UNKNOWN. The record
    must still hold the exact bytes the operator inspected. It is archived
    before it is removed, nothing is terminated, and the receipt keeps the
    machine-verified evidence apart from what the confirmation covers.
    """
    if not isinstance(confirmed_by, str) or not confirmed_by.strip():
        raise HostAdmissionError(
            "admission_recovery_unconfirmed",
            "explicit recovery needs --confirmed-by naming who confirmed that nothing "
            "from the previous host remains",
        )
    runtime = Path(runtime_dir)
    path = lock_path_for(runtime)
    raw = _read_bytes(path, reason="admission_lock_unreadable")
    if raw is None:
        return {
            "schema_name": HOST_ADMISSION_RECOVERY_SCHEMA,
            "schema_version": HOST_ADMISSION_RECOVERY_VERSION,
            "outcome": "absent",
            "path": str(path),
            "terminated_processes": [],
        }
    digest = hashlib.sha256(raw).hexdigest()
    if digest != str(record_sha256).strip().lower():
        raise HostAdmissionError(
            "admission_recovery_record_mismatch",
            "the admission record is absent or no longer holds the inspected bytes; "
            "run --show again and inspect the current record",
            {"path": str(path), "record_sha256": digest},
        )
    lock = parse_lock(raw, path=path)
    state, reason = classify_process(lock, signature_mismatch="uncertain")
    if state == "active":
        raise HostAdmissionError(
            "admission_domain_busy",
            f"the recorded host (PID {lock.get('pid')}) is running; close its client normally",
            owner_summary(lock),
        )
    if state == "uncertain":
        raise HostAdmissionError(
            "admission_state_uncertain",
            f"the recorded host (PID {lock.get('pid')}) cannot be verified: {reason}",
            owner_summary(lock),
        )
    automatic: dict[str, Any]
    try:
        verify_stale_cleanup(lock, runtime, ignore_pids=observer_lineage())
        automatic = {"state": "verified"}
    except HostAdmissionError as exc:
        if exc.reason_code != "admission_state_uncertain":
            raise  # positive evidence is never lifted by a confirmation
        automatic = {"state": "unknown", "reason_code": exc.reason_code, "reason": str(exc)}
    evidence = verify_stale_cleanup(
        lock, runtime, ignore_pids=observer_lineage(), for_explicit_recovery=True
    )
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    archive_dir = runtime / RECOVERY_ARCHIVE_DIRECTORY
    stem = f"host_admission-{stamp}-{digest[:16]}"
    archive = archive_dir / f"{stem}.lock"
    atomic_write_bytes_exclusive(archive, raw)
    if archive.read_bytes() != raw:
        raise HostAdmissionError(
            "admission_state_uncertain",
            "the archived copy does not match the record; nothing was removed",
            {"archive": archive.name},
        )
    removed = bool(unlink_if_content(path, raw))
    receipt = {
        "schema_name": HOST_ADMISSION_RECOVERY_SCHEMA,
        "schema_version": HOST_ADMISSION_RECOVERY_VERSION,
        "recorded_at_epoch": time.time(),
        "outcome": "recovered" if removed else "refused_record_changed",
        "record": {"sha256": digest, "archived_as": archive.name},
        "previous_host": owner_summary(lock),
        "owner_state": {"state": state, "reason": reason},
        "automatic_verification": automatic,
        "machine_verification": evidence,
        "operator_confirmation": {
            "confirmed_by": confirmed_by.strip(),
            "covers": confirmation_coverage(evidence),
        },
        "terminated_processes": [],
    }
    atomic_write_bytes_exclusive(archive_dir / f"{stem}.recovery.json", document_bytes(receipt))
    if not removed:
        raise HostAdmissionError(
            "admission_domain_busy",
            "the admission record changed during recovery; nothing was removed",
            {"archive": archive.name},
        )
    return receipt


def main(argv: list[str] | None = None) -> int:
    """``--show <runtime-dir>`` or ``--recover <runtime-dir> <sha256> --confirmed-by <text>``."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m comsol_mcp.host_admission",
        description="Inspect or explicitly recover a host admission record.",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--show", metavar="RUNTIME_DIR")
    action.add_argument("--recover", nargs=2, metavar=("RUNTIME_DIR", "RECORD_SHA256"))
    parser.add_argument("--confirmed-by", default="")
    arguments = parser.parse_args(argv)
    try:
        if arguments.show:
            result = recovery_preview(arguments.show)
        else:
            runtime, digest = arguments.recover
            result = recover_after_independent_confirmation(
                runtime, record_sha256=digest, confirmed_by=arguments.confirmed_by
            )
    except HostAdmissionError as exc:
        print(f"refused: [{exc.reason_code}] {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
