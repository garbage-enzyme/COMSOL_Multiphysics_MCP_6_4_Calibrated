"""Host admission contract tests (0.8.2 §10 startup gate, schema 1.1.0).

Each test drives the real lock file and real processes in an isolated
ASCII runtime directory. Injected interleavings replace a function at the
exact window that a concurrent writer could use.
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
from unittest.mock import patch

import psutil
import pytest

from comsol_mcp import host_admission as ha

SETTINGS = {"path": "D:/fixture/settings.json", "sha256": "b" * 64}
# Windows keeps a dead parent's PID in its children; POSIX reparents orphans
# to init, so parent links cannot attribute them there (see the module doc).
windows_parent_links = pytest.mark.skipif(
    os.name != "nt", reason="POSIX reparents orphans; parent links cannot attribute them"
)
SLEEPER = [sys.executable, "-c", "import time; time.sleep(120)"]


def _spawn(argv: list[str]) -> subprocess.Popen:
    return subprocess.Popen(  # noqa: S603 - fixed interpreter argv
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _identity(pid: int) -> dict:
    process = psutil.Process(pid)
    return {
        "pid": pid,
        "process_create_time": process.create_time(),
        "command_signature": ha.command_signature(process.cmdline()),
    }


def _dead_identity() -> dict:
    process = _spawn([sys.executable, "-c", "pass"])
    identity = _identity(process.pid)
    process.wait(timeout=30)
    deadline = time.monotonic() + 10
    while psutil.pid_exists(identity["pid"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    return identity


def _record(owner: dict, **extra) -> dict:
    return {
        "schema_name": ha.HOST_ADMISSION_SCHEMA,
        "schema_version": ha.HOST_ADMISSION_VERSION,
        "state": "active",
        "transport_mode": "shim",
        **owner,
        "executable": None,
        "client": None,
        "settings": SETTINGS,
        "domain": None,
        "shim": None,
        "owned_processes": [],
        "recovered_from": None,
        "acquired_at_epoch": time.time(),
        **extra,
    }


def _write(path: Path, document: dict) -> bytes:
    payload = ha.document_bytes(document)
    path.write_bytes(payload)
    return payload


@pytest.fixture
def runtime(ascii_tmp_path: Path) -> Path:
    directory = ascii_tmp_path / "runtime"
    directory.mkdir()
    return directory


@pytest.fixture
def cleanup_processes():
    started: list[subprocess.Popen | int] = []
    yield started
    for item in started:
        try:
            if isinstance(item, int):
                psutil.Process(item).kill()
            else:
                item.kill()
                item.wait(timeout=10)
        except psutil.Error, OSError, subprocess.TimeoutExpired:
            pass


def test_acquire_publishes_the_full_record_and_release_removes_it(runtime):
    claim = ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    document = json.loads(claim.path.read_bytes())
    assert claim.path.read_bytes() == claim.payload
    assert document["schema_version"] == "1.1.0"
    assert document["pid"] == os.getpid()
    assert document["process_create_time"] == pytest.approx(psutil.Process().create_time())
    assert len(document["command_signature"]) == 64
    assert document["settings"] == SETTINGS
    assert document["owned_processes"] == []
    assert document["state"] == "active"
    assert ha.release(claim) is True
    assert not claim.path.exists()


def test_release_preserves_a_replacement_record(runtime):
    claim = ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    replacement = _record(_identity(os.getpid()), transport_mode="shim")
    replacement_bytes = _write(claim.path, replacement)
    assert ha.release(claim) is False
    assert claim.path.read_bytes() == replacement_bytes


@pytest.mark.skipif(
    os.name != "nt",
    reason="only the Windows opened-identity delete can distinguish a same-content "
    "replacement; the POSIX quarantine path restores it without loss",
)
def test_release_preserves_identical_bytes_with_a_new_file_identity(runtime):
    claim = ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    claim.path.unlink()
    claim.path.write_bytes(claim.payload)
    assert ha.release(claim) is False
    assert claim.path.read_bytes() == claim.payload


def test_release_preserves_a_replacement_injected_after_the_owner_read(runtime):
    claim = ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    replacement = ha.document_bytes(_record(_identity(os.getpid())))
    original = ha.unlink_if_content

    def replace_then_unlink(path, expected, **kwargs):
        Path(path).write_bytes(replacement)
        return original(path, expected, **kwargs)

    with patch.object(ha, "unlink_if_content", side_effect=replace_then_unlink):
        assert ha.release(claim) is False
    assert claim.path.read_bytes() == replacement


def test_live_owner_refuses_with_its_identity(runtime, cleanup_processes):
    owner = _spawn(SLEEPER)
    cleanup_processes.append(owner)
    _write(ha.lock_path_for(runtime), _record(_identity(owner.pid)))
    with pytest.raises(ha.HostAdmissionError) as error:
        ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    assert error.value.reason_code == "admission_domain_busy"
    assert error.value.details["owner_pid"] == owner.pid
    assert error.value.details["owner_transport"] == "shim"


def test_matching_birth_with_a_different_signature_is_uncertain(runtime, cleanup_processes):
    owner = _spawn(SLEEPER)
    cleanup_processes.append(owner)
    identity = {**_identity(owner.pid), "command_signature": "0" * 64}
    _write(ha.lock_path_for(runtime), _record(identity))
    with pytest.raises(ha.HostAdmissionError) as error:
        ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    assert error.value.reason_code == "admission_state_uncertain"


def test_stale_owner_with_verified_cleanup_is_recovered_with_evidence(runtime):
    if os.name != "nt":
        previous = _contained_dead_host()
        _write(ha.lock_path_for(runtime), _contained_record(previous))
        stale = previous
    else:
        stale = _dead_identity()
        _write(ha.lock_path_for(runtime), _record(stale))
    claim = ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    recovered = claim.document["recovered_from"]
    assert recovered["pid"] == stale["pid"]
    assert recovered["cleanup"]["descendants"]["live"] == []
    assert recovered["cleanup"]["solver_lease"] == {"state": "absent"}
    assert recovered["cleanup"]["listeners"]["method"] == "owned_process_absence"
    assert ha.release(claim) is True


def test_reused_pid_is_stale_and_its_new_children_are_not_counted(runtime, cleanup_processes):
    holder = _spawn(SLEEPER)
    cleanup_processes.append(holder)
    reused = {**_identity(holder.pid), "process_create_time": 1_000_000.0}
    if os.name != "nt":
        record = _contained_record(_contained_dead_host())
        record["pid"] = reused["pid"]
        record["process_create_time"] = reused["process_create_time"]
        _write(ha.lock_path_for(runtime), record)
    else:
        _write(ha.lock_path_for(runtime), _record(reused))
    claim = ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    assert (
        claim.document["recovered_from"]["reason"] == "the PID now belongs to a different process"
    )
    ha.release(claim)


def test_live_recorded_worker_blocks_recovery(runtime, cleanup_processes):
    worker = _spawn(SLEEPER)
    cleanup_processes.append(worker)
    worker_identity = _identity(worker.pid)
    owned = [
        {
            "role": "worker",
            "pid": worker.pid,
            "process_create_time": worker_identity["process_create_time"],
        }
    ]
    _write(ha.lock_path_for(runtime), _record(_dead_identity(), owned_processes=owned))
    with pytest.raises(ha.HostAdmissionError) as error:
        ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    assert error.value.reason_code == "admission_cleanup_unverified"
    assert error.value.details["pid"] == worker.pid
    assert ha.lock_path_for(runtime).exists()


def _orphan_with_live_child() -> tuple[dict, int]:
    """Start a host-like process that leaves one live child, then exits.

    The process reports its own identity: behind a Windows venv launcher the
    interpreter that spawns children is not the launcher PID that Popen sees,
    exactly as a real host records ``os.getpid()`` of the interpreter.
    """
    script = (
        "import json, subprocess, sys, psutil;"
        "me = psutil.Process();"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],"
        " creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0));"
        "print(json.dumps({'pid': me.pid, 'process_create_time': me.create_time(),"
        " 'cmdline': me.cmdline(), 'child': child.pid}), flush=True)"
    )
    parent = subprocess.Popen(  # noqa: S603 - fixed interpreter argv
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    report = json.loads(parent.stdout.readline())
    parent.wait(timeout=30)
    deadline = time.monotonic() + 10
    while psutil.pid_exists(report["pid"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    identity = {
        "pid": report["pid"],
        "process_create_time": report["process_create_time"],
        "command_signature": ha.command_signature(report["cmdline"]),
    }
    return identity, report["child"]


@windows_parent_links
def test_live_direct_child_of_a_stale_owner_blocks_recovery(runtime, cleanup_processes):
    owner, child_pid = _orphan_with_live_child()
    cleanup_processes.append(child_pid)
    _write(ha.lock_path_for(runtime), _record(owner))
    with pytest.raises(ha.HostAdmissionError) as error:
        ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    assert error.value.reason_code == "admission_cleanup_unverified"
    assert [item["pid"] for item in error.value.details["descendants"]] == [child_pid]


@windows_parent_links
def test_verified_durable_job_worker_does_not_block_recovery(runtime, cleanup_processes):
    owner, child_pid = _orphan_with_live_child()
    cleanup_processes.append(child_pid)
    job = runtime / "jobs" / "job-1"
    job.mkdir(parents=True)
    (job / "state.json").write_text(
        json.dumps(
            {
                "status": "running",
                "worker_pid": child_pid,
                "worker_process_create_time": psutil.Process(child_pid).create_time(),
            }
        ),
        encoding="utf-8",
    )
    _write(ha.lock_path_for(runtime), _record(owner))
    claim = ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    independent = claim.document["recovered_from"]["cleanup"]["descendants"]["independent_owners"]
    owned = [item for item in independent if item["role"] != "console_host"]
    assert len(owned) == 1
    assert owned[0]["role"] in {"durable_job_worker", "durable_job_worker_launcher"}
    ha.release(claim)


@windows_parent_links
def test_terminal_job_state_does_not_excuse_a_live_child(runtime, cleanup_processes):
    owner, child_pid = _orphan_with_live_child()
    cleanup_processes.append(child_pid)
    job = runtime / "jobs" / "job-1"
    job.mkdir(parents=True)
    (job / "state.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "worker_pid": child_pid,
                "worker_process_create_time": psutil.Process(child_pid).create_time(),
            }
        ),
        encoding="utf-8",
    )
    _write(ha.lock_path_for(runtime), _record(owner))
    with pytest.raises(ha.HostAdmissionError) as error:
        ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    assert error.value.reason_code == "admission_cleanup_unverified"


def test_stale_reclaim_preserves_a_replacement_and_refuses(runtime):
    lock = ha.lock_path_for(runtime)
    if os.name != "nt":
        _write(lock, _contained_record(_contained_dead_host()))
    else:
        _write(lock, _record(_dead_identity()))
    replacement = ha.document_bytes(_record(_identity(os.getpid())))
    original = ha.verify_stale_cleanup

    def verify_then_replace(record, runtime_dir):
        evidence = original(record, runtime_dir)
        lock.write_bytes(replacement)
        return evidence

    with patch.object(ha, "verify_stale_cleanup", side_effect=verify_then_replace):
        with pytest.raises(ha.HostAdmissionError) as error:
            ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    assert error.value.reason_code == "admission_domain_busy"
    assert lock.read_bytes() == replacement


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"{", "admission_lock_corrupt"),
        (b"[]", "admission_lock_corrupt"),
        (
            json.dumps(
                {"schema_name": ha.HOST_ADMISSION_SCHEMA, "schema_version": "9.0.0"}
            ).encode(),
            "admission_lock_schema_unsupported",
        ),
        (
            json.dumps(
                {"schema_name": ha.HOST_ADMISSION_SCHEMA, "schema_version": "1.1.0", "pid": "x"}
            ).encode(),
            "admission_state_uncertain",
        ),
    ],
)
def test_unverifiable_records_refuse_without_recovery(runtime, content, reason):
    lock = ha.lock_path_for(runtime)
    lock.write_bytes(content)
    with pytest.raises(ha.HostAdmissionError) as error:
        ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    assert error.value.reason_code == reason
    assert lock.read_bytes() == content


def _lease(identity: dict, **extra) -> dict:
    return {
        "schema_version": "3",
        "owner": "comsol-mcp",
        "mode": "local-client",
        **identity,
        "acquisition_id": "a" * 32,
        "comsol_server_processes": [],
        **extra,
    }


def test_live_non_job_solver_owner_blocks_fresh_admission(runtime, cleanup_processes):
    owner = _spawn(SLEEPER)
    cleanup_processes.append(owner)
    (runtime / "solver_owner.json").write_text(
        json.dumps(_lease(_identity(owner.pid))), encoding="utf-8"
    )
    with pytest.raises(ha.HostAdmissionError) as error:
        ha.acquire(runtime, transport_mode="shim", settings=SETTINGS)
    assert error.value.reason_code == "admission_domain_busy"
    assert not ha.lock_path_for(runtime).exists()


def test_live_durable_job_solver_owner_is_reported_not_refused(runtime, cleanup_processes):
    owner = _spawn(SLEEPER)
    cleanup_processes.append(owner)
    (runtime / "solver_owner.json").write_text(
        json.dumps(_lease(_identity(owner.pid), owner="job:job-1", mode="durable-job")),
        encoding="utf-8",
    )
    claim = ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    ha.release(claim)


def test_stale_lease_with_a_live_recorded_server_blocks_recovery(runtime, cleanup_processes):
    server = _spawn(SLEEPER)
    cleanup_processes.append(server)
    (runtime / "solver_owner.json").write_text(
        json.dumps(_lease(_dead_identity(), comsol_server_processes=[_identity(server.pid)])),
        encoding="utf-8",
    )
    if os.name != "nt":
        # The lease evidence is positive: reach it through a record that can
        # be judged (a containment-less record is UNKNOWN first on POSIX).
        previous = _contained_dead_host()
        _write(ha.lock_path_for(runtime), _contained_record(previous))
    else:
        _write(ha.lock_path_for(runtime), _record(_dead_identity()))
    with pytest.raises(ha.HostAdmissionError) as error:
        ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    assert error.value.reason_code == "admission_cleanup_unverified"
    assert error.value.details["server_pid"] == server.pid


def test_stale_lease_with_absent_servers_permits_admission(runtime):
    (runtime / "solver_owner.json").write_text(
        json.dumps(_lease(_dead_identity(), comsol_server_processes=[_dead_identity()])),
        encoding="utf-8",
    )
    claim = ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    ha.release(claim)
    assert (runtime / "solver_owner.json").exists(), "admission never recovers a lease"


def test_unreadable_solver_lease_is_uncertain(runtime):
    (runtime / "solver_owner.json").write_text("{", encoding="utf-8")
    with pytest.raises(ha.HostAdmissionError) as error:
        ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    assert error.value.reason_code == "admission_state_uncertain"


@windows_parent_links
def test_status_reports_owner_and_cleanup_assessment(runtime, cleanup_processes):
    assert ha.status(runtime)["state"] == "absent"
    owner, child_pid = _orphan_with_live_child()
    cleanup_processes.append(child_pid)
    _write(ha.lock_path_for(runtime), _record(owner))
    report = ha.status(runtime, assess_cleanup=True)
    assert report["state"] == "stale"
    assert report["transport_mode"] == "shim"
    assert report["cleanup"]["state"] == "unverified"
    assert report["cleanup"]["reason_code"] == "admission_cleanup_unverified"


def test_worker_flag_without_binding_evidence_is_refused(runtime):
    settings = {
        "transport": "shim",
        "shim_binary": {
            "path": "D:/x.exe",
            "sha256": "a" * 64,
            "version": "0.8.2-dev",
            "platform": "x86_64-pc-windows-msvc",
        },
    }
    with pytest.raises(ha.HostAdmissionError) as error:
        ha.verify_worker_binding(
            settings=settings,
            settings_identity=SETTINGS,
            runtime_dir=runtime,
            environ={ha.WORKER_FLAG_ENV: "1"},
        )
    assert error.value.reason_code == "worker_binding_missing"


def test_worker_binding_refuses_without_an_admitted_shim(runtime):
    settings = {
        "transport": "shim",
        "shim_binary": {
            "path": "D:/x.exe",
            "sha256": "a" * 64,
            "version": "0.8.2-dev",
            "platform": "x86_64-pc-windows-msvc",
        },
    }
    environ = {
        ha.WORKER_FLAG_ENV: "1",
        ha.WORKER_HOST_PID_ENV: str(os.getppid()),
        ha.WORKER_TOKEN_ENV: "c" * 64,
    }
    with pytest.raises(ha.HostAdmissionError) as error:
        ha.verify_worker_binding(
            settings=settings, settings_identity=SETTINGS, runtime_dir=runtime, environ=environ
        )
    assert error.value.reason_code == "worker_binding_invalid"


def test_worker_binding_refuses_a_parent_that_is_not_the_recorded_host(runtime):
    binary = Path(sys.executable)
    settings = {
        "transport": "shim",
        "shim_binary": {
            "path": str(binary),
            "sha256": ha._sha256_file(binary),
            "version": "0.8.2-dev",
            "platform": "x86_64-pc-windows-msvc",
        },
    }
    token = "d" * 64
    host = _identity(os.getpid())  # Alive, but not this process's parent.
    _write(
        ha.lock_path_for(runtime),
        _record(
            host,
            executable=str(binary),
            shim={
                "version": "0.8.2-dev",
                "platform": "x86_64-pc-windows-msvc",
                "executable_sha256": settings["shim_binary"]["sha256"],
                "worker_token_sha256": __import__("hashlib").sha256(token.encode()).hexdigest(),
            },
        ),
    )
    environ = {
        ha.WORKER_FLAG_ENV: "1",
        ha.WORKER_HOST_PID_ENV: str(os.getpid()),
        ha.WORKER_TOKEN_ENV: token,
    }
    with pytest.raises(ha.HostAdmissionError) as error:
        ha.verify_worker_binding(
            settings=settings, settings_identity=SETTINGS, runtime_dir=runtime, environ=environ
        )
    assert error.value.reason_code == "worker_binding_invalid"


def test_expected_shim_version_tracks_the_cargo_manifest():
    manifest = Path(__file__).parents[2] / "shim" / "Cargo.toml"
    lines = manifest.read_text(encoding="utf-8").splitlines()
    version = next(line.split('"')[1] for line in lines if line.startswith("version = "))
    assert version == ha.EXPECTED_SHIM_VERSION


def _executing_identity(pid: int) -> dict:
    """The interpreter behind a virtual-environment launcher, or the process."""
    process = psutil.Process(pid)
    kids = [kid for kid in process.children() if kid.name().lower() != "conhost.exe"]
    if len(kids) == 1 and kids[0].cmdline() == process.cmdline():
        return _identity(kids[0].pid)
    return _identity(pid)


@windows_parent_links
def test_launcher_of_a_bound_worker_is_independent(runtime, cleanup_processes):
    owner, child_pid = _orphan_with_live_child()
    cleanup_processes.append(child_pid)
    executing = _executing_identity(child_pid)
    job = runtime / "jobs" / "job-2"
    job.mkdir(parents=True)
    (job / "state.json").write_text(
        json.dumps(
            {
                "status": "running",
                "worker_pid": executing["pid"],
                "worker_process_create_time": executing["process_create_time"],
            }
        ),
        encoding="utf-8",
    )
    _write(ha.lock_path_for(runtime), _record(owner))
    claim = ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    roles = {
        item["role"]
        for item in claim.document["recovered_from"]["cleanup"]["descendants"]["independent_owners"]
    }
    assert roles & {"durable_job_worker", "durable_job_worker_launcher"}
    ha.release(claim)


@windows_parent_links
@pytest.mark.parametrize("helper", ["wall_watchdog", "cancel_coordinator"])
def test_recorded_job_helpers_are_independent(runtime, cleanup_processes, helper):
    owner, child_pid = _orphan_with_live_child()
    cleanup_processes.append(child_pid)
    identity = _identity(child_pid)
    job = runtime / "jobs" / "job-3"
    job.mkdir(parents=True)
    if helper == "wall_watchdog":
        (job / "state.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        (job / "wall-watchdog.json").write_text(
            json.dumps({"status": "armed", "watchdog_identity": identity}), encoding="utf-8"
        )
    else:
        (job / "state.json").write_text(
            json.dumps({"status": "cancelling", "cancel": {"coordinator": identity}}),
            encoding="utf-8",
        )
    _write(ha.lock_path_for(runtime), _record(owner))
    claim = ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    roles = {
        item["role"]
        for item in claim.document["recovered_from"]["cleanup"]["descendants"]["independent_owners"]
    }
    assert helper in roles
    ha.release(claim)


@windows_parent_links
def test_observer_and_its_ancestors_are_not_cleanup_debt(runtime, cleanup_processes):
    owner, child_pid = _orphan_with_live_child()
    cleanup_processes.append(child_pid)
    _write(ha.lock_path_for(runtime), _record(owner))
    lineage = frozenset({child_pid, *(kid.pid for kid in psutil.Process(child_pid).children())})
    evidence = ha.verify_stale_cleanup(
        json.loads(ha.lock_path_for(runtime).read_bytes()), runtime, ignore_pids=lineage
    )
    assert evidence["descendants"]["live"] == []
    with pytest.raises(ha.HostAdmissionError):
        ha.verify_stale_cleanup(json.loads(ha.lock_path_for(runtime).read_bytes()), runtime)


def _orphan_chain_with_listener(scripts: Path, mark: Path) -> tuple[dict, int]:
    """A dead host, a dead middle, and a live listening grandchild.

    The three stages run from script files (no nested quoting); the
    grandchild picks its own port and records it in ``mark`` so nothing in
    this process holds the listener. The chain runs inside THIS process's
    containment, so a record carrying that identity must find the grandchild
    through the kernel even though its recorded ancestors are gone.
    """
    scripts.mkdir(parents=True, exist_ok=True)
    grand = scripts / "grand.py"
    middle = scripts / "middle.py"
    host = scripts / "host.py"
    grand.write_text(
        "import socket, time\n"
        "listener = socket.socket()\n"
        "listener.bind(('127.0.0.1', 0))\n"
        "listener.listen(1)\n"
        f"open(r'{mark}', 'w').write(str(listener.getsockname()[1]))\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    middle.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, r'%s'], creationflags=0x08000000)\n"
        "time.sleep(0.8)\n" % str(grand),
        encoding="utf-8",
    )
    host.write_text(
        "import json, psutil, subprocess, sys\n"
        "me = psutil.Process()\n"
        "middle = subprocess.Popen([sys.executable, r'%s'], creationflags=0x08000000)\n"
        "middle.wait()\n"
        "print(json.dumps({'host': me.pid, 'create': me.create_time()}), flush=True)\n"
        % str(middle),
        encoding="utf-8",
    )
    parent = subprocess.Popen(
        [sys.executable, str(host)],
        stdout=subprocess.PIPE,
        text=True,
        creationflags=0x08000000,
    )
    report = json.loads(parent.stdout.readline())
    parent.wait(timeout=30)
    deadline = time.time() + 10
    while time.time() < deadline and not mark.exists():
        time.sleep(0.05)
    if not mark.exists():
        return report, 0
    port = int(mark.read_text(encoding="ascii"))
    import psutil as _ps

    grand_pid = 0
    deadline = time.time() + 10
    while time.time() < deadline:
        for connection in _ps.net_connections(kind="inet"):
            if (
                connection.laddr
                and connection.laddr.port == port
                and connection.status == "LISTEN"
                and connection.pid != os.getpid()
            ):
                grand_pid = connection.pid
        if grand_pid:
            break
        time.sleep(0.1)
    return report, grand_pid


@windows_parent_links
def test_kernel_containment_finds_a_listening_grandchild_behind_dead_ancestors(
    runtime, cleanup_processes
):
    from comsol_mcp import host_containment

    identity = host_containment.host_containment_id(pid=os.getpid())
    handle = host_containment.adopt_host_containment(identity)
    mark = runtime / "grandchild-port.txt"
    report, grand = _orphan_chain_with_listener(runtime / "chain", mark)
    assert grand is not None, "the listening grandchild must exist"
    cleanup_processes.append(grand)
    try:
        record = _record(
            {
                "pid": report["host"],
                "process_create_time": report["create"],
                "command_signature": "0" * 64,
            },
            containment=identity,
        )
        with pytest.raises(ha.HostAdmissionError) as error:
            ha.verify_stale_cleanup(record, runtime, ignore_pids=ha.observer_lineage())
        assert error.value.reason_code == "admission_cleanup_unverified"
        assert grand in [item["pid"] for item in error.value.details["descendants"]]
    finally:
        import ctypes

        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)


@windows_parent_links
def test_kernel_containment_passes_when_no_member_remains(runtime):
    from comsol_mcp import host_containment

    identity = host_containment.host_containment_id(pid=os.getpid())
    handle = host_containment.adopt_host_containment(identity)
    try:
        dead = _dead_identity()
        record = _record(dead, containment=identity)
        # The only members are this process (the observer) and its console.
        evidence = ha.verify_stale_cleanup(record, runtime, ignore_pids=ha.observer_lineage())
        assert evidence["descendants"]["live"] == []
        assert evidence["descendants"]["scope"].startswith("kernel membership")
    finally:
        import ctypes

        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)


@windows_parent_links
def test_legacy_record_with_an_unreadable_parent_is_unknown(runtime, monkeypatch):
    dead = _dead_identity()
    _write(ha.lock_path_for(runtime), _record(dead))

    def broken_inventory(*_args, **_kwargs):
        return [(424242, None, 1000.0, "python.exe")]

    monkeypatch.setattr(ha, "_process_inventory", broken_inventory)
    with pytest.raises(ha.HostAdmissionError) as error:
        ha.verify_stale_cleanup(_record(dead), runtime)
    assert error.value.reason_code == "admission_state_uncertain"
    assert "unreadable parent" in str(error.value)


@windows_parent_links
def test_acquire_records_and_the_record_carries_containment(runtime):
    claim = ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    try:
        recorded = claim.document["containment"]
        assert recorded["kind"] in {"windows_job", "posix_session"}
        if recorded["kind"] == "windows_job":
            assert recorded["name"].startswith(r"Local\comsol_mcp_host_")
    finally:
        assert ha.release(claim) is True


# --- POSIX session containment (runs on the POSIX lane only) ----------------

posix_sessions = pytest.mark.skipif(os.name == "nt", reason="POSIX session containment")
REPO_ROOT = Path(__file__).resolve().parents[2]

_ADOPT_AND_REPORT = """
import json, os, subprocess, sys
if sys.argv[1] == "group_leader":
    os.setpgid(0, 0)
from comsol_mcp import host_containment
identity = host_containment.host_containment_id(pid=os.getpid())
try:
    host_containment.adopt_host_containment(identity)
    adopted, error = True, None
except OSError as exc:
    adopted, error = False, str(exc)
orphan = None
if adopted and sys.argv[2] == "orphan":
    # The orphan must not hold this child's pipes open, or the caller's
    # read would wait for it.
    orphan = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).pid
import psutil
print(json.dumps({
    "pid": os.getpid(),
    "process_create_time": psutil.Process().create_time(),
    "session": os.getsid(0),
    "identity": identity,
    "adopted": adopted,
    "error": error,
    "orphan": orphan,
}), flush=True)
"""


def _adopt_in_child(mode: str, orphan: str) -> dict:
    environment = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    completed = subprocess.run(  # noqa: S603 - fixed interpreter argv
        [sys.executable, "-c", _ADOPT_AND_REPORT, mode, orphan],
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@posix_sessions
def test_a_normal_child_leads_the_session_it_records():
    report = _adopt_in_child("normal", "none")

    assert report["adopted"] is True, report
    assert report["identity"] == {"kind": "posix_session", "id": report["pid"]}
    assert report["session"] == report["pid"]


@posix_sessions
def test_a_process_group_leader_cannot_record_a_session_it_does_not_lead():
    # setsid() is refused to a process-group leader, which keeps the session
    # it inherited; adoption must refuse instead of recording its own PID.
    report = _adopt_in_child("group_leader", "none")

    assert report["adopted"] is False, report
    assert "leads no session it can record" in report["error"]
    assert report["session"] != report["pid"]


@posix_sessions
def test_session_containment_finds_a_reparented_orphan(runtime):
    report = _adopt_in_child("normal", "orphan")
    orphan = psutil.Process(report["orphan"])
    try:
        deadline = time.time() + 10
        while psutil.pid_exists(report["pid"]) and time.time() < deadline:
            time.sleep(0.05)
        assert orphan.ppid() != report["pid"]  # reparented: parent links lost
        record = {
            "pid": report["pid"],
            "process_create_time": report["process_create_time"],
            "containment": report["identity"],
        }
        with pytest.raises(ha.HostAdmissionError) as refusal:
            ha.verify_stale_cleanup(record, runtime)
        assert refusal.value.reason_code == "admission_cleanup_unverified"
        assert str(report["orphan"]) in str(refusal.value)
    finally:
        orphan.kill()


# --- POSIX: a containment-less stale record is UNKNOWN, never clean ----------

_HOST_THAT_RECORDS_ITSELF = f"""
import json, sys
sys.path.insert(0, {str(REPO_ROOT)!r})
from comsol_mcp import host_containment
identity = host_containment.host_containment_id(pid=__import__("os").getpid())
host_containment.adopt_host_containment(identity)
host_containment.containment_members(identity)
import psutil
print(json.dumps({{
    "pid": __import__("os").getpid(),
    "process_create_time": psutil.Process().create_time(),
    "cmdline": psutil.Process().cmdline(),
    "containment": identity,
}}), flush=True)
"""


def _contained_dead_host() -> dict:
    """A previous host that adopted containment, recorded it, and exited."""
    completed = subprocess.run(  # noqa: S603 - fixed interpreter argv
        [sys.executable, "-c", _HOST_THAT_RECORDS_ITSELF],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO_ROOT),
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    deadline = time.monotonic() + 10
    while psutil.pid_exists(report["pid"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    return report


def _contained_record(report: dict) -> dict:
    return _record(
        {
            "pid": report["pid"],
            "process_create_time": report["process_create_time"],
            "command_signature": ha.command_signature(report["cmdline"]),
        },
        containment=report["containment"],
    )


_SESSION_ORPHAN_SCRIPT = f"""
import json, os, subprocess, sys
sys.path.insert(0, {str(REPO_ROOT)!r})
from comsol_mcp import host_containment
identity = host_containment.host_containment_id(pid=os.getpid())
host_containment.adopt_host_containment(identity)
orphan = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(120)"],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
import psutil
print(json.dumps({{
    "pid": os.getpid(),
    "process_create_time": psutil.Process().create_time(),
    "cmdline": psutil.Process().cmdline(),
    "containment": identity,
    "orphan": orphan.pid,
}}), flush=True)
"""


def _host_with_session_orphan() -> dict:
    """A previous host whose orphaned child stays in its containment."""
    parent = subprocess.Popen(  # noqa: S603 - fixed interpreter argv
        [sys.executable, "-c", _SESSION_ORPHAN_SCRIPT],
        stdout=subprocess.PIPE,
        text=True,
        cwd=str(REPO_ROOT),
    )
    report = json.loads(parent.stdout.readline())
    parent.wait(timeout=30)
    deadline = time.monotonic() + 10
    while psutil.pid_exists(report["pid"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    return report


@posix_sessions
def test_a_containment_less_stale_record_is_unknown_and_is_kept(runtime):
    dead = _dead_identity()
    payload = _write(ha.lock_path_for(runtime), _record(dead))
    with pytest.raises(ha.HostAdmissionError) as error:
        ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    assert error.value.reason_code == "admission_state_uncertain"
    assert "no containment identity" in str(error.value)
    assert "the record was kept" in str(error.value)
    details = error.value.details
    assert details["path"] == str(ha.lock_path_for(runtime))
    assert details["recovery"].startswith("independent confirmation")
    assert ha.lock_path_for(runtime).read_bytes() == payload


@posix_sessions
def test_a_contained_dead_host_is_recovered_with_evidence(runtime):
    previous = _contained_dead_host()
    _write(ha.lock_path_for(runtime), _contained_record(previous))
    claim = ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    recovered = claim.document["recovered_from"]
    assert recovered["pid"] == previous["pid"]
    assert recovered["cleanup"]["descendants"]["live"] == []
    assert recovered["cleanup"]["solver_lease"] == {"state": "absent"}
    assert ha.release(claim) is True


@posix_sessions
def test_a_contained_host_with_a_live_session_orphan_blocks_recovery(runtime):
    report = _host_with_session_orphan()
    try:
        payload = _write(ha.lock_path_for(runtime), _contained_record(report))
        with pytest.raises(ha.HostAdmissionError) as error:
            ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
        assert error.value.reason_code == "admission_cleanup_unverified"
        assert str(report["orphan"]) in str(error.value)
        assert ha.lock_path_for(runtime).read_bytes() == payload
    finally:
        try:
            psutil.Process(report["orphan"]).kill()
        except psutil.Error:
            pass


# --- the explicit operator recovery flow -------------------------------------


def test_recovery_needs_a_named_confirmation(runtime):
    dead = _dead_identity()
    _write(ha.lock_path_for(runtime), _record(dead))
    with pytest.raises(ha.HostAdmissionError) as error:
        ha.recover_after_independent_confirmation(
            runtime, record_sha256="0" * 64, confirmed_by="  "
        )
    assert error.value.reason_code == "admission_recovery_unconfirmed"


def test_recovery_refuses_when_the_record_changed(runtime):
    dead = _dead_identity()
    payload = _write(ha.lock_path_for(runtime), _record(dead))
    with pytest.raises(ha.HostAdmissionError) as error:
        ha.recover_after_independent_confirmation(
            runtime, record_sha256="0" * 64, confirmed_by="operator"
        )
    assert error.value.reason_code == "admission_recovery_record_mismatch"
    assert ha.lock_path_for(runtime).read_bytes() == payload


def test_recovery_refuses_a_live_recorded_host(runtime, cleanup_processes):
    live = _spawn(SLEEPER)
    cleanup_processes.append(live)
    payload = _write(ha.lock_path_for(runtime), _record(_identity(live.pid)))
    with pytest.raises(ha.HostAdmissionError) as error:
        ha.recover_after_independent_confirmation(
            runtime,
            record_sha256=hashlib.sha256(payload).hexdigest(),
            confirmed_by="operator",
        )
    assert error.value.reason_code == "admission_domain_busy"
    assert ha.lock_path_for(runtime).read_bytes() == payload


def test_recovery_is_not_lifted_over_positive_evidence(runtime, cleanup_processes):
    worker = _spawn(SLEEPER)
    cleanup_processes.append(worker)
    payload = _write(
        ha.lock_path_for(runtime),
        _record(_dead_identity(), owned_processes=[_identity(worker.pid)]),
    )
    with pytest.raises(ha.HostAdmissionError) as error:
        ha.recover_after_independent_confirmation(
            runtime,
            record_sha256=hashlib.sha256(payload).hexdigest(),
            confirmed_by="operator",
        )
    assert error.value.reason_code == "admission_cleanup_unverified"
    assert ha.lock_path_for(runtime).read_bytes() == payload


def test_recovery_of_an_absent_record_reports_absence(runtime):
    result = ha.recover_after_independent_confirmation(
        runtime, record_sha256="0" * 64, confirmed_by="operator"
    )
    assert result == {
        "schema_name": ha.HOST_ADMISSION_RECOVERY_SCHEMA,
        "schema_version": ha.HOST_ADMISSION_RECOVERY_VERSION,
        "outcome": "absent",
        "path": str(ha.lock_path_for(runtime)),
        "terminated_processes": [],
    }


def test_recovery_archives_and_removes_a_confirmed_stale_record(runtime):
    dead = _dead_identity()
    payload = _write(ha.lock_path_for(runtime), _record(dead))
    digest = hashlib.sha256(payload).hexdigest()

    preview = ha.recovery_preview(runtime)
    assert preview["record_sha256"] == digest
    assert "--recover" in preview["recovery_command"]
    assert preview["explicit_recovery"]["state"] == "ready"

    receipt = ha.recover_after_independent_confirmation(
        runtime, record_sha256=digest, confirmed_by="operator"
    )
    assert receipt["outcome"] == "recovered"
    assert receipt["record"]["sha256"] == digest
    assert receipt["terminated_processes"] == []
    assert not ha.lock_path_for(runtime).exists()
    archive_dir = runtime / "admission_recovered"
    archived = archive_dir / receipt["record"]["archived_as"]
    assert archived.read_bytes() == payload
    receipt_path = archive_dir / receipt["record"]["archived_as"].replace(".lock", ".recovery.json")
    body = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert body["schema_name"] == ha.HOST_ADMISSION_RECOVERY_SCHEMA
    assert body["schema_version"] == ha.HOST_ADMISSION_RECOVERY_VERSION == "1.1.0"
    assert body["operator_confirmation"]["confirmed_by"] == "operator"
    machine = receipt["machine_verification"]
    if os.name == "nt":
        # Parent links verify a Windows legacy record by machine: the
        # confirmation stands in for nothing.
        assert receipt["automatic_verification"]["state"] == "verified"
        assert receipt["operator_confirmation"]["covers"] == []
        assert machine["listeners"]["method"] == "owned_process_absence"
    else:
        assert receipt["automatic_verification"]["state"] == "unknown"
        covered = [item["evidence"] for item in receipt["operator_confirmation"]["covers"]]
        assert covered == ["descendants", "listeners"]
        assert machine["descendants"]["unavailable"] == "the record has no containment identity"
        assert "own session" in machine["descendants"]["scope"]
        assert machine["listeners"]["method"] == "not_machine_verified"

    # A host may start again once the record was explicitly recovered.
    claim = ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    assert ha.release(claim) is True


@posix_sessions
def test_recovery_of_a_contained_record_verifies_automatically(runtime):
    previous = _contained_dead_host()
    payload = _write(ha.lock_path_for(runtime), _contained_record(previous))
    receipt = ha.recover_after_independent_confirmation(
        runtime,
        record_sha256=hashlib.sha256(payload).hexdigest(),
        confirmed_by="operator",
    )
    assert receipt["outcome"] == "recovered"
    assert receipt["automatic_verification"]["state"] == "verified"
    assert not ha.lock_path_for(runtime).exists()


# POSIX only: on Windows a dead host's job name no longer opens, so its
# members cannot be enumerated after the host exits (open finding, see the
# 2026-11-10 handback); the session form of this control is sound.
@posix_sessions
def test_recovery_is_not_lifted_over_a_live_session_member(runtime):
    report = _host_with_session_orphan()
    try:
        payload = _write(ha.lock_path_for(runtime), _contained_record(report))
        with pytest.raises(ha.HostAdmissionError) as error:
            ha.recover_after_independent_confirmation(
                runtime,
                record_sha256=hashlib.sha256(payload).hexdigest(),
                confirmed_by="operator",
            )
        assert error.value.reason_code == "admission_cleanup_unverified"
        assert str(report["orphan"]) in str(error.value)
        assert ha.lock_path_for(runtime).read_bytes() == payload
    finally:
        try:
            psutil.Process(report["orphan"]).kill()
        except psutil.Error:
            pass


# --- WIN-CONTAIN-01: a lost job name is UNKNOWN, never clean ------------------

_JOB_ORPHAN_SCRIPT = f"""
import json, os, psutil, subprocess, sys
sys.path.insert(0, {str(REPO_ROOT)!r})
from comsol_mcp import host_containment
identity = host_containment.host_containment_id(pid=os.getpid())
handle = host_containment.adopt_host_containment(identity)
assert host_containment.containment_members(identity)  # the handle keeps it readable
# The venv launcher does not outlive its host here; the base interpreter does.
orphan = subprocess.Popen(
    [getattr(sys, "_base_executable", sys.executable), "-c", "import time; time.sleep(120)"],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
del handle  # the last handle: the job object dies with it, not with the orphan
me = psutil.Process()
print(json.dumps({{
    "pid": me.pid,
    "process_create_time": me.create_time(),
    "cmdline": me.cmdline(),
    "containment": identity,
    "orphan": orphan.pid,
    "orphan_in_a_job": True,
}}), flush=True)
"""


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _console_hosts_of(pid: int) -> list[psutil.Process]:
    """Live console hosts serving the console that process ``pid`` created."""
    return [
        process
        for process in psutil.process_iter(["ppid", "name"])
        if process.info["ppid"] == pid and (process.info["name"] or "").lower() == "conhost.exe"
    ]


def _wait_until_gone(processes: list[psutil.Process]) -> None:
    for process in processes:
        try:
            process.wait(timeout=10)
        except psutil.NoSuchProcess:
            pass


def _wait_for_exit(pid: int) -> None:
    deadline = time.monotonic() + 10
    while psutil.pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.05)


def _dead_host_behind_a_lost_job() -> dict:
    """A Windows host that exits holding the only job handle, orphan alive.

    The host starts with its own hidden console, as an MCP client starts
    it, so the fixture does not depend on whether pytest has a console. The
    orphan inherits that console and so keeps it in use.
    """
    # The venv launcher terminates its own descendants when it exits, which
    # would take the orphan with it; the base interpreter leaves it alone.
    parent = subprocess.Popen(  # noqa: S603 - fixed interpreter argv
        [getattr(sys, "_base_executable", sys.executable), "-c", _JOB_ORPHAN_SCRIPT],
        stdout=subprocess.PIPE,
        text=True,
        cwd=str(REPO_ROOT),
        creationflags=_NO_WINDOW,
    )
    report = json.loads(parent.stdout.readline())
    parent.wait(timeout=30)
    _wait_for_exit(report["pid"])
    return report


@windows_parent_links
def test_a_job_name_that_no_longer_opens_is_unknown(runtime):
    import ctypes

    from comsol_mcp import host_containment
    from comsol_mcp.host_containment import UnknownContainment

    identity = host_containment.host_containment_id(pid=os.getpid())
    handle = host_containment.adopt_host_containment(identity)
    assert os.getpid() in host_containment.containment_members(identity)
    ctypes.windll.kernel32.CloseHandle(handle)  # the only handle
    with pytest.raises(UnknownContainment) as unknown:
        host_containment.containment_members(identity)
    assert "no longer opens" in str(unknown.value)
    assert "cannot be ruled out" in str(unknown.value)


@windows_parent_links
def test_a_dead_windows_host_with_an_orphan_is_unknown_and_kept(runtime):
    report = _dead_host_behind_a_lost_job()
    orphan = psutil.Process(report["orphan"])
    try:
        payload = _write(ha.lock_path_for(runtime), _contained_record(report))
        with pytest.raises(ha.HostAdmissionError) as error:
            ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
        assert error.value.reason_code == "admission_state_uncertain"
        assert "containment cannot be read" in str(error.value)
        assert str(error.value).endswith(ha.OPERATOR_RECOVERY_HINT)
        assert orphan.is_running()
        assert ha.lock_path_for(runtime).read_bytes() == payload
    finally:
        orphan.kill()


@windows_parent_links
def test_explicit_recovery_refuses_a_live_direct_child_after_job_loss(runtime):
    # WIN-RECOVER-02: a confirmation may cover what the machine cannot see,
    # never a child the process inventory still attributes to the host.
    report = _dead_host_behind_a_lost_job()
    orphan = psutil.Process(report["orphan"])
    born = orphan.create_time()
    try:
        assert orphan.ppid() == report["pid"]
        payload = _write(ha.lock_path_for(runtime), _contained_record(report))
        digest = hashlib.sha256(payload).hexdigest()
        preview = ha.recovery_preview(runtime)
        assert preview["explicit_recovery"]["state"] == "refused"
        assert preview["explicit_recovery"]["reason_code"] == "admission_cleanup_unverified"
        with pytest.raises(ha.HostAdmissionError) as error:
            ha.recover_after_independent_confirmation(
                runtime, record_sha256=digest, confirmed_by="operator"
            )
        assert error.value.reason_code == "admission_cleanup_unverified"
        assert str(report["orphan"]) in str(error.value)
        by_pid = {item["pid"]: item for item in error.value.details["descendants"]}
        assert by_pid.pop(report["orphan"])["parent_pid"] == report["pid"]
        # The orphan also keeps the host's own console, and its host, alive.
        assert [item.get("role") for item in by_pid.values()] == ["console_host_in_use"]
        assert ha.lock_path_for(runtime).read_bytes() == payload
        assert not (runtime / "admission_recovered").exists()
        assert orphan.is_running() and orphan.create_time() == born  # left alone
    finally:
        orphan.kill()


@windows_parent_links
def test_explicit_recovery_of_a_lost_job_record_needs_the_operator(runtime):
    report = _dead_host_behind_a_lost_job()
    orphan = psutil.Process(report["orphan"])
    try:
        payload = _write(ha.lock_path_for(runtime), _contained_record(report))
        digest = hashlib.sha256(payload).hexdigest()
        # The operator's own cleanup, before any confirmation.
        consoles = _console_hosts_of(report["pid"])
        orphan.kill()
        _wait_until_gone([orphan, *consoles])
        receipt = ha.recover_after_independent_confirmation(
            runtime, record_sha256=digest, confirmed_by="operator"
        )
        assert receipt["outcome"] == "recovered"
        assert receipt["terminated_processes"] == []
        assert receipt["automatic_verification"]["state"] == "unknown"
        machine = receipt["machine_verification"]
        assert "no longer opens" in machine["descendants"]["unavailable"]
        assert "parent exited" in machine["descendants"]["not_machine_verified"]
        assert machine["descendants"]["scope"].startswith("direct children of the host")
        assert machine["listeners"]["method"] == "not_machine_verified"
        confirmation = receipt["operator_confirmation"]
        assert confirmation["confirmed_by"] == "operator"
        assert [item["evidence"] for item in confirmation["covers"]] == [
            "descendants",
            "listeners",
        ]
        assert not ha.lock_path_for(runtime).exists()
        archived = runtime / "admission_recovered" / receipt["record"]["archived_as"]
        assert archived.read_bytes() == payload
        claim = ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
        assert ha.release(claim) is True
    finally:
        try:
            orphan.kill()
        except psutil.Error:
            pass


_JOB_GRANDCHILD_SCRIPT = f"""
import json, os, psutil, subprocess, sys
sys.path.insert(0, {str(REPO_ROOT)!r})
from comsol_mcp import host_containment
base = getattr(sys, "_base_executable", sys.executable)
grandchild_flags = sys.argv[1]  # 0 inherits the host's console
identity = host_containment.host_containment_id(pid=os.getpid())
handle = host_containment.adopt_host_containment(identity)
intermediate = subprocess.Popen(
    [base, "-c",
     "import subprocess, sys; "
     "child = subprocess.Popen([sys.argv[1], '-c', 'import time; time.sleep(120)'], "
     "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
     "creationflags=int(sys.argv[2])); "
     "print(child.pid, flush=True)",
     base, grandchild_flags],
    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
)
grandchild = int(intermediate.stdout.readline())
intermediate.wait(timeout=30)
me = psutil.Process()
print(json.dumps({{
    "pid": me.pid,
    "process_create_time": me.create_time(),
    "cmdline": me.cmdline(),
    "containment": identity,
    "intermediate": intermediate.pid,
    "grandchild": grandchild,
}}), flush=True)
"""


def _dead_host_with_a_grandchild(grandchild_flags: int) -> dict:
    """A host with its own hidden console that exits behind a lost job name.

    Its intermediate child exits first, so the grandchild's parent link names
    a dead process. ``grandchild_flags`` 0 keeps the grandchild on the host's
    console; CREATE_NO_WINDOW gives it a console of its own.
    """
    parent = subprocess.Popen(  # noqa: S603 - fixed interpreter argv
        [
            getattr(sys, "_base_executable", sys.executable),
            "-c",
            _JOB_GRANDCHILD_SCRIPT,
            str(grandchild_flags),
        ],
        stdout=subprocess.PIPE,
        text=True,
        cwd=str(REPO_ROOT),
        creationflags=_NO_WINDOW,
    )
    report = json.loads(parent.stdout.readline())
    parent.wait(timeout=30)
    _wait_for_exit(report["pid"])
    return report


@windows_parent_links
def test_a_confirmation_never_claims_an_unattributable_descendant_is_gone(runtime):
    # The boundary the confirmation may cover: a grandchild whose own parent
    # exited first names a dead PID and holds a console of its own, so nothing
    # attributes it to the host. The receipt must say that this was not
    # machine-verified.
    report = _dead_host_with_a_grandchild(_NO_WINDOW)
    grandchild = psutil.Process(report["grandchild"])
    try:
        assert grandchild.ppid() == report["intermediate"]
        assert grandchild.ppid() != report["pid"]
        _wait_until_gone(_console_hosts_of(report["pid"]))  # nothing keeps it in use
        payload = _write(ha.lock_path_for(runtime), _contained_record(report))
        receipt = ha.recover_after_independent_confirmation(
            runtime,
            record_sha256=hashlib.sha256(payload).hexdigest(),
            confirmed_by="operator",
        )
        assert receipt["outcome"] == "recovered"
        machine = receipt["machine_verification"]
        assert machine["descendants"]["live"] == []
        assert "unavailable" in machine["descendants"]
        assert machine["listeners"]["method"] == "not_machine_verified"
        assert receipt["operator_confirmation"]["covers"]
        assert receipt["terminated_processes"] == []
        assert grandchild.is_running()  # nothing was terminated
    finally:
        grandchild.kill()


@windows_parent_links
def test_explicit_recovery_refuses_while_the_dead_hosts_console_is_in_use(runtime):
    # The host had its own hidden console. Its grandchild cannot be attributed
    # (its parent exited first), but it keeps that console in use, and a
    # console host lives only while a process is attached: visible evidence.
    report = _dead_host_with_a_grandchild(0)
    grandchild = psutil.Process(report["grandchild"])
    try:
        consoles = _console_hosts_of(report["pid"])
        assert len(consoles) == 1, "the host had its own console"
        payload = _write(ha.lock_path_for(runtime), _contained_record(report))
        digest = hashlib.sha256(payload).hexdigest()
        with pytest.raises(ha.HostAdmissionError) as error:
            ha.recover_after_independent_confirmation(
                runtime, record_sha256=digest, confirmed_by="operator"
            )
        assert error.value.reason_code == "admission_cleanup_unverified"
        assert "console is still in use" in str(error.value)
        roles = {item["pid"]: item.get("role") for item in error.value.details["descendants"]}
        assert roles == {consoles[0].pid: "console_host_in_use"}
        assert ha.lock_path_for(runtime).read_bytes() == payload
        assert not (runtime / "admission_recovered").exists()
        assert grandchild.is_running()

        # The operator stops what keeps the console in use; then it proceeds.
        grandchild.kill()
        _wait_until_gone([grandchild, *consoles])
        receipt = ha.recover_after_independent_confirmation(
            runtime, record_sha256=digest, confirmed_by="operator"
        )
        assert receipt["outcome"] == "recovered"
        assert "console" in receipt["machine_verification"]["descendants"]["scope"]
        assert receipt["terminated_processes"] == []
    finally:
        try:
            grandchild.kill()
        except psutil.Error:
            pass


def test_a_lost_job_name_names_the_operator_flow_and_only_it_is_confirmable(runtime, monkeypatch):
    from comsol_mcp import host_containment

    def lost(_identity):
        raise host_containment.ContainmentNameLost(host_containment.JOB_NAME_LOST_MESSAGE)

    monkeypatch.setattr(host_containment, "containment_members", lost)
    record = _record(
        _dead_identity(),
        containment={"kind": "windows_job", "name": "Local\\comsol_mcp_host_0_lost"},
    )
    payload = _write(ha.lock_path_for(runtime), record)
    with pytest.raises(ha.HostAdmissionError) as automatic:
        ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    assert automatic.value.reason_code == "admission_state_uncertain"
    assert str(automatic.value).endswith(ha.OPERATOR_RECOVERY_HINT)
    assert automatic.value.details["recovery"].startswith("independent confirmation")
    assert ha.lock_path_for(runtime).read_bytes() == payload

    preview = ha.recovery_preview(runtime)
    assert preview["explicit_recovery"]["state"] == "ready"
    covers = preview["explicit_recovery"]["operator_confirmation_covers"]
    assert [item["evidence"] for item in covers] == ["descendants", "listeners"]
    receipt = ha.recover_after_independent_confirmation(
        runtime, record_sha256=preview["record_sha256"], confirmed_by="operator"
    )
    assert receipt["outcome"] == "recovered"
    assert receipt["automatic_verification"]["state"] == "unknown"
    machine = receipt["machine_verification"]
    assert machine["descendants"]["unavailable"].endswith(host_containment.JOB_NAME_LOST_MESSAGE)
    assert machine["descendants"]["not_machine_verified"]
    assert machine["listeners"]["method"] == "not_machine_verified"
    assert receipt["operator_confirmation"] == {"confirmed_by": "operator", "covers": covers}


@pytest.mark.parametrize(
    "problem",
    [
        "the host job members cannot be listed: winerror 5",
        "the host job cannot be opened: winerror 5",
        "/proc cannot be listed: [Errno 13] denied",
    ],
)
def test_explicit_recovery_keeps_other_unreadable_containment_unknown(
    runtime, monkeypatch, problem
):
    from comsol_mcp import host_containment

    def unreadable(_identity):
        raise host_containment.UnknownContainment(problem)

    monkeypatch.setattr(host_containment, "containment_members", unreadable)
    record = _record(
        _dead_identity(),
        containment={"kind": "windows_job", "name": "Local\\comsol_mcp_host_0_denied"},
    )
    payload = _write(ha.lock_path_for(runtime), record)
    with pytest.raises(ha.HostAdmissionError) as automatic:
        ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
    assert automatic.value.reason_code == "admission_state_uncertain"
    assert "--show" not in str(automatic.value)
    with pytest.raises(ha.HostAdmissionError) as explicit:
        ha.recover_after_independent_confirmation(
            runtime,
            record_sha256=hashlib.sha256(payload).hexdigest(),
            confirmed_by="operator",
        )
    assert explicit.value.reason_code == "admission_state_uncertain"
    assert problem in str(explicit.value)
    assert ha.lock_path_for(runtime).read_bytes() == payload
    assert not (runtime / "admission_recovered").exists()
    assert ha.recovery_preview(runtime)["explicit_recovery"]["state"] == "refused"


_SESSION_LEADER_SCRIPT = """
import json, os, subprocess, sys
os.setsid()  # a leader that records no containment (a legacy record)
member = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(120)"],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
import psutil
me = psutil.Process()
print(json.dumps({
    "pid": me.pid,
    "process_create_time": me.create_time(),
    "cmdline": me.cmdline(),
    "member": member.pid,
}), flush=True)
"""


@posix_sessions
def test_explicit_recovery_refuses_a_live_member_of_the_dead_hosts_own_session(runtime):
    parent = subprocess.Popen(  # noqa: S603 - fixed interpreter argv
        [sys.executable, "-c", _SESSION_LEADER_SCRIPT],
        stdout=subprocess.PIPE,
        text=True,
        cwd=str(REPO_ROOT),
    )
    report = json.loads(parent.stdout.readline())
    parent.wait(timeout=30)
    deadline = time.monotonic() + 10
    while psutil.pid_exists(report["pid"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    member = psutil.Process(report["member"])
    born = member.create_time()
    try:
        assert os.getsid(member.pid) == report["pid"]
        assert member.ppid() != report["pid"]  # reparented: no parent link left
        record = _record(
            {
                "pid": report["pid"],
                "process_create_time": report["process_create_time"],
                "command_signature": ha.command_signature(report["cmdline"]),
            }
        )
        payload = _write(ha.lock_path_for(runtime), record)
        with pytest.raises(ha.HostAdmissionError) as automatic:
            ha.acquire(runtime, transport_mode="python", settings=SETTINGS)
        assert automatic.value.reason_code == "admission_state_uncertain"
        with pytest.raises(ha.HostAdmissionError) as error:
            ha.recover_after_independent_confirmation(
                runtime,
                record_sha256=hashlib.sha256(payload).hexdigest(),
                confirmed_by="operator",
            )
        assert error.value.reason_code == "admission_cleanup_unverified"
        assert str(member.pid) in str(error.value)
        [found] = error.value.details["descendants"]
        assert (found["pid"], found["session"]) == (member.pid, report["pid"])
        assert ha.lock_path_for(runtime).read_bytes() == payload
        assert not (runtime / "admission_recovered").exists()
        assert member.is_running() and member.create_time() == born
        assert ha.recovery_preview(runtime)["explicit_recovery"]["state"] == "refused"
    finally:
        member.kill()


@posix_sessions
def test_the_host_session_check_handles_reuse_age_and_unreadable_sessions(monkeypatch):
    host = {"pid": 990_001, "process_create_time": 1000.0, "role": "host"}
    rows = [(990_002, 1, 1500.0, "member")]
    monkeypatch.setattr(ha, "_process_inventory", lambda: list(rows))
    sessions = {990_002: host["pid"]}

    def getsid(pid):
        if pid in sessions:
            return sessions[pid]
        raise ProcessLookupError(pid)

    monkeypatch.setattr(os, "getsid", getsid)

    def check():
        return ha._attributable_descendants(
            [host], ignore_pids=frozenset(), owners={}, context="for a test record"
        )

    scope, found, _ = check()
    assert [item["pid"] for item in found] == [990_002]
    assert "own session" in scope
    # A live process now holding the host PID owns any session with that id.
    rows.append((host["pid"], 1, 2000.0, "reuser"))
    scope, found, _ = check()
    assert found == [] and "belongs to another process" in scope
    rows.pop()
    # Older than the host: not started by it.
    rows[0] = (990_002, 1, 900.0, "member")
    assert check()[1] == []
    # A process that exited since the inventory is gone, not unknown.
    rows[0] = (990_003, 1, 1500.0, "gone")
    assert check()[1] == []

    def denied(_pid):
        raise PermissionError(1, "denied")

    monkeypatch.setattr(os, "getsid", denied)
    with pytest.raises(ha.HostAdmissionError) as error:
        check()
    assert error.value.reason_code == "admission_state_uncertain"
    assert "session of process 990003" in str(error.value)


def _rust_str_const(source: str, name: str) -> str:
    match = re.search(rf'const {name}: &str = "((?:[^"\\]|\\.)*)";', source, re.DOTALL)
    assert match, name
    return re.sub(r"\\\r?\n\s*", "", match.group(1))


def test_shared_operator_texts_match_the_shim_sources():
    from comsol_mcp import host_containment

    shim = REPO_ROOT / "shim" / "src"
    admission = (shim / "admission.rs").read_text(encoding="utf-8")
    containment = (shim / "containment.rs").read_text(encoding="utf-8")
    assert _rust_str_const(admission, "OPERATOR_RECOVERY_HINT") == ha.OPERATOR_RECOVERY_HINT
    assert _rust_str_const(containment, "JOB_NAME_LOST") == (host_containment.JOB_NAME_LOST_MESSAGE)


@windows_parent_links
def test_explicit_recovery_still_refuses_positive_evidence_after_job_loss(runtime):
    worker = _spawn(SLEEPER)
    cleanup_processes_fixture = [worker]
    try:
        report = _dead_host_behind_a_lost_job()
        orphan = psutil.Process(report["orphan"])
        cleanup_processes_fixture.append(orphan)
        record = _contained_record(report)
        record["owned_processes"] = [_identity(worker.pid)]
        payload = _write(ha.lock_path_for(runtime), record)
        with pytest.raises(ha.HostAdmissionError) as error:
            ha.recover_after_independent_confirmation(
                runtime,
                record_sha256=hashlib.sha256(payload).hexdigest(),
                confirmed_by="operator",
            )
        assert error.value.reason_code == "admission_cleanup_unverified"
        assert str(worker.pid) in str(error.value)
        assert ha.lock_path_for(runtime).read_bytes() == payload
    finally:
        for process in cleanup_processes_fixture:
            try:
                process.kill()
            except psutil.Error:
                pass
