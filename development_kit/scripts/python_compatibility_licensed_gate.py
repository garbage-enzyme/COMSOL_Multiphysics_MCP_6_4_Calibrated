"""Run a provenance-bound Python/COMSOL compatibility probe on a licensed host."""

from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import uuid

import psutil

from src.jobs.store import atomic_write_json
from src.tools.ownership import SolverOwnership, _command_signature


ROOT = Path(__file__).resolve().parents[2]
EXPECTED_BACKEND = {"major": 6, "minor": 4, "patch": 0, "build": 293}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_identity() -> dict:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return {"commit": commit, "dirty_entry_count": len(dirty)}


def _process_identity(pid: int) -> dict:
    process = psutil.Process(pid)
    with process.oneshot():
        command_line = list(process.cmdline())
        try:
            executable = process.exe()
        except (psutil.AccessDenied, psutil.ZombieProcess):
            executable = None
        return {
            "pid": process.pid,
            "parent_pid": process.ppid(),
            "process_create_time": process.create_time(),
            "name": process.name(),
            "executable": executable,
            "command_line": command_line,
            "command_signature": _command_signature(command_line),
        }


def _descendant_identities(pid: int) -> list[dict]:
    try:
        children = psutil.Process(pid).children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []
    identities = []
    for child in children:
        try:
            identities.append(_process_identity(child.pid))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return sorted(identities, key=lambda item: (item["process_create_time"], item["pid"]))


def _listener_inventory(pids: set[int]) -> dict:
    try:
        connections = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError) as exc:
        return {"complete": False, "error": f"{type(exc).__name__}: {exc}", "listeners": []}
    listeners = []
    for connection in connections:
        if connection.pid not in pids or connection.status != psutil.CONN_LISTEN:
            continue
        local = connection.laddr
        listeners.append(
            {
                "pid": connection.pid,
                "address": getattr(local, "ip", local[0] if local else None),
                "port": getattr(local, "port", local[1] if local else None),
            }
        )
    return {
        "complete": True,
        "error": None,
        "listeners": sorted(listeners, key=lambda item: (item["pid"], item["port"])),
    }


def _status_evidence(status: dict) -> dict:
    durable = status.get("durable_jobs") or {}
    lease = status.get("lease") or {}
    return {
        "collision": status.get("collision"),
        "process_inventory": status.get("process_inventory"),
        "external_solver_processes": status.get("external_solver_processes"),
        "lease_state": lease.get("state"),
        "lease": lease.get("lease"),
        "durable_jobs_available": durable.get("available"),
        "active_job_count": durable.get("active_count"),
        "active_jobs": durable.get("active"),
    }


def _status_is_clean(status: dict) -> bool:
    durable = status.get("durable_jobs") or {}
    inventory = status.get("process_inventory") or {}
    return (
        inventory.get("complete") is True
        and inventory.get("fresh") is True
        and status.get("collision") is False
        and status.get("lease", {}).get("state") == "absent"
        and durable.get("available") is True
        and durable.get("active_count") == 0
    )


def _wait_clean(owner: SolverOwnership, timeout_seconds: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while True:
        status = owner.status(require_fresh_inventory=True)
        if _status_is_clean(status) or time.monotonic() >= deadline:
            return status
        time.sleep(0.25)


def _terminate_owned_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    else:
        for child in reversed(psutil.Process(process.pid).children(recursive=True)):
            child.kill()
        psutil.Process(process.pid).kill()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=15)


def _backend_identity(backend: dict) -> dict:
    return {
        "name": str(backend.get("name")),
        "major": int(backend.get("major")),
        "minor": int(backend.get("minor")),
        "patch": int(backend.get("patch")),
        "build": int(backend.get("build")),
        "root": str(backend.get("root")),
        "jvm": str(backend.get("jvm")),
    }


def _select_expected_backend(backends: list[dict]) -> dict:
    matching = [
        backend
        for backend in backends
        if all(int(backend.get(key, -1)) == value for key, value in EXPECTED_BACKEND.items())
    ]
    if len(matching) != 1:
        raise RuntimeError(
            f"expected exactly one COMSOL 6.4.0.293 backend, found {len(matching)}"
        )
    return _backend_identity(matching[0])


def _run_worker(output: Path, cores: int) -> int:
    import jpype
    import mph

    result = {
        "schema_name": "comsol_mcp.python_compatibility_capacitor_probe",
        "schema_version": "1.0.0",
        "success": False,
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "packages": {
            name: version(name) for name in ("jpype1", "mcp", "mph", "psutil", "pydantic")
        },
    }
    client = None
    started = time.monotonic()
    try:
        backend = _select_expected_backend(mph.discovery.find_backends())
        result["backend"] = backend
        client = mph.Client(cores=cores, version="6.4")
        result["client"] = {
            "version": str(client.version),
            "standalone": bool(client.standalone),
            "host": client.host,
            "port": client.port,
            "jvm_started": bool(jpype.isJVMStarted()),
        }
        if not client.standalone or client.port is not None:
            raise RuntimeError("compatibility probe requires a standalone client without a server port")

        java_system = jpype.JClass("java.lang.System")
        result["java"] = {
            "version": str(java_system.getProperty("java.version")),
            "vendor": str(java_system.getProperty("java.vendor")),
            "home": str(java_system.getProperty("java.home")),
        }
        try:
            result["comsol_reported_version"] = str(client.java.getComsolVersion())
        except Exception as exc:
            result["comsol_reported_version"] = None
            result["comsol_reported_version_error"] = f"{type(exc).__name__}: {exc}"

        model = client.create("PythonCompatibilityCapacitor")
        jm = model.java
        for name, value in (
            ("L", "0.01[m]"),
            ("d", "0.001[m]"),
            ("epsr", "2.1"),
            ("V0", "1[V]"),
        ):
            jm.param().set(name, value)

        component = jm.component().create("comp1", True)
        geometry = component.geom().create("geom1", 3)
        block = geometry.feature().create("blk1", "Block")
        block.set("size", jpype.JArray(jpype.JDouble)([0.01, 0.01, 0.001]))
        block.set("pos", jpype.JArray(jpype.JDouble)([0.0, 0.0, 0.0]))
        geometry.run()

        dimension = str(geometry.getSDim())
        electrostatics = component.physics().create("es", "Electrostatics", dimension)
        conservation = electrostatics.feature().create(
            "ccn1", "ChargeConservation", int(dimension)
        )
        conservation.selection().set([1])
        conservation.set("materialType", "from_mat")
        material = component.material().create("mat1", "Common")
        material.propertyGroup("def").set("relpermittivity", "2.1")
        material.selection().set([1])
        ground = electrostatics.feature().create("gnd1", "Ground", 2)
        ground.selection().set([3])
        potential = electrostatics.feature().create("ep1", "ElectricPotential", 2)
        potential.selection().set([4])
        potential.set("V0", "V0")

        mesh = component.mesh().create("mesh1")
        mesh.feature().create("ftr1", "FreeTet")
        mesh.run()
        study = jm.study().create("std1")
        study.create("step1", "Stationary")
        study.run()

        measured = float(model.evaluate("2*es.intWe/(1[V])^2", "pF").reshape(-1)[0])
        theory = 8.8541878128e-12 * 2.1 * math.pow(0.01, 2) / 0.001 * 1e12
        relative_error = abs(measured - theory) / theory
        accepted = math.isclose(measured, theory, rel_tol=1e-8, abs_tol=1e-9)
        result["geometry"] = {
            "domains": int(geometry.getNDomains()),
            "boundaries": int(geometry.getNBoundaries()),
        }
        result["mesh"] = {"elements": int(mesh.getNumElem())}
        result["physics"] = {
            "measured_capacitance_pf": measured,
            "theory_capacitance_pf": theory,
            "relative_error": relative_error,
            "relative_tolerance": 1e-8,
            "absolute_tolerance_pf": 1e-9,
            "accepted": accepted,
        }
        if not accepted:
            raise AssertionError("parallel-plate capacitance is outside tolerance")
        result["success"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if client is not None:
            try:
                client.clear()
            except Exception as exc:
                result["client_clear_error"] = f"{type(exc).__name__}: {exc}"
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        atomic_write_json(output, result)
    return 0 if result["success"] else 1


def _run_parent(args) -> int:
    output = args.output.expanduser().resolve()
    if output.exists():
        raise ValueError("licensed gate output must use a new path")
    git = _git_identity()
    if git["dirty_entry_count"]:
        raise RuntimeError("licensed gate requires a clean git tree")

    output.parent.mkdir(parents=True, exist_ok=True)
    worker_output = output.with_name(f".{output.stem}.worker.{uuid.uuid4().hex}.json")
    owner = SolverOwnership(args.runtime_root, owner="python-compatibility-licensed-gate")
    receipt = {
        "schema_name": "comsol_mcp.python_compatibility_licensed_gate",
        "schema_version": "1.0.0",
        "success": False,
        "source": {
            "git": git,
            "script": str(Path(__file__).resolve().relative_to(ROOT)),
            "script_sha256": _sha256_file(Path(__file__).resolve()),
        },
        "environment": {
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
    }
    process = None
    lease_acquired = False
    child_identities: dict[tuple[int, float], dict] = {}
    listeners: dict[tuple[int, int], dict] = {}
    started = time.monotonic()
    returncode = 1
    try:
        before = _wait_clean(owner, timeout_seconds=1.0)
        receipt["ownership_before"] = _status_evidence(before)
        if not _status_is_clean(before):
            raise RuntimeError("licensed gate requires no collision, lease, or active durable job")
        preflight = owner.preflight(
            output_path=str(output),
            requested_version="6.4",
            minimum_free_gb=args.minimum_free_gb,
        )
        receipt["preflight"] = preflight
        if not preflight.get("ready"):
            raise RuntimeError(f"solver preflight failed: {preflight.get('blockers')}")
        claim = owner.acquire(mode="python-compatibility-gate")
        receipt["lease_claim"] = claim
        if not claim.get("success"):
            raise RuntimeError(claim.get("error", "solver lease acquisition failed"))
        lease_acquired = True

        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            "--worker-output",
            str(worker_output),
            "--cores",
            str(args.cores),
        ]
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        receipt["worker_process"] = _process_identity(process.pid)
        deadline = time.monotonic() + args.timeout_seconds
        timed_out = False
        while process.poll() is None:
            if time.monotonic() >= deadline:
                timed_out = True
                _terminate_owned_tree(process)
                break
            if not owner.heartbeat(refresh_server_processes=True):
                raise RuntimeError("solver lease heartbeat failed")
            descendants = _descendant_identities(os.getpid())
            for identity in descendants:
                child_identities[(identity["pid"], identity["process_create_time"])] = identity
            ports = _listener_inventory({item["pid"] for item in descendants})
            if not ports["complete"]:
                raise RuntimeError(f"owned listener inventory failed: {ports['error']}")
            for listener in ports["listeners"]:
                listeners[(listener["pid"], listener["port"])] = listener
            time.sleep(0.25)
        stdout, stderr = process.communicate(timeout=15)
        receipt["worker_execution"] = {
            "returncode": process.returncode,
            "timed_out": timed_out,
            "stdout_tail": stdout[-4000:],
            "stderr_tail": stderr[-4000:],
        }
        if worker_output.is_file():
            receipt["worker_result_sha256"] = _sha256_file(worker_output)
            receipt["worker_result"] = json.loads(worker_output.read_text(encoding="utf-8"))
        else:
            receipt["worker_result"] = None

        if not owner.heartbeat(refresh_server_processes=True):
            raise RuntimeError("final solver lease heartbeat failed")
        active = owner.status(require_fresh_inventory=True)
        receipt["ownership_during"] = _status_evidence(active)
        lease = active.get("lease", {}).get("lease") or {}
        receipt["runtime_process_evidence"] = {
            "descendants": sorted(
                child_identities.values(),
                key=lambda item: (item["process_create_time"], item["pid"]),
            ),
            "owned_listeners": sorted(
                listeners.values(), key=lambda item: (item["pid"], item["port"])
            ),
            "listener_inventory_complete": True,
            "lease_server_processes": lease.get("comsol_server_processes"),
            "lease_server_port": lease.get("comsol_server_port"),
        }
        worker_result = receipt.get("worker_result") or {}
        runtime_evidence = receipt["runtime_process_evidence"]
        phase_passed = (
            process.returncode == 0
            and not timed_out
            and worker_result.get("success") is True
            and worker_result.get("backend", {}).get("build") == EXPECTED_BACKEND["build"]
            and worker_result.get("client", {}).get("standalone") is True
            and worker_result.get("client", {}).get("port") is None
            and worker_result.get("physics", {}).get("accepted") is True
            and runtime_evidence["owned_listeners"] == []
            and runtime_evidence["lease_server_port"] is None
            and active.get("durable_jobs", {}).get("active_count") == 0
        )
        if not phase_passed:
            raise RuntimeError("licensed compatibility phase did not satisfy its evidence contract")
        returncode = 0
    except Exception as exc:
        receipt["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if process is not None:
            _terminate_owned_tree(process)
        if lease_acquired:
            receipt["lease_release"] = owner.release()
        after = _wait_clean(owner)
        receipt["ownership_after"] = _status_evidence(after)
        cleanup_passed = _status_is_clean(after) and not _descendant_identities(os.getpid())
        receipt["cleanup"] = {
            "lease_absent": after.get("lease", {}).get("state") == "absent",
            "collision_absent": after.get("collision") is False,
            "active_job_count": after.get("durable_jobs", {}).get("active_count"),
            "owned_descendants_absent": not _descendant_identities(os.getpid()),
            "passed": cleanup_passed,
        }
        receipt["duration_seconds"] = round(time.monotonic() - started, 3)
        receipt["success"] = returncode == 0 and cleanup_passed
        if not receipt["success"]:
            returncode = 1
        atomic_write_json(output, receipt)
        worker_output.unlink(missing_ok=True)
    print(output)
    return returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", choices=["RUN_REAL_COMSOL"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--runtime-root", type=Path, default=Path("D:/comsol_runtime"))
    parser.add_argument("--cores", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--minimum-free-gb", type=float, default=2.0)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        if args.worker_output is None:
            raise SystemExit("worker mode requires --worker-output")
        return _run_worker(args.worker_output.resolve(), args.cores)
    if args.confirm != "RUN_REAL_COMSOL" or args.output is None:
        raise SystemExit("licensed gate requires --confirm RUN_REAL_COMSOL and --output")
    if not 1 <= args.cores <= 64:
        raise SystemExit("--cores must be in 1..64")
    if not 1.0 <= args.timeout_seconds <= 7200.0:
        raise SystemExit("--timeout-seconds must be in 1..7200")
    try:
        return _run_parent(args)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
