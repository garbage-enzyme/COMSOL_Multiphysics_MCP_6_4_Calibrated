"""Licensed probe: resolve how to read a numerical value from a solved model.

The S8 campaign gate needs a fresh FEM evaluation that returns a real number.
`result().numerical('ev1').getReal()` returned an empty list, so this probe
tries the bounded set of evaluation-node configurations on a real solved model
and records which one actually yields a value.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import uuid
from importlib import import_module
from pathlib import Path
from typing import Any

import psutil

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(ROOT))

atomic_write_json_exclusive = import_module("comsol_mcp.durable.io").atomic_write_json_exclusive
ownership_module = import_module("comsol_mcp.tools.ownership")
SolverOwnership = ownership_module.SolverOwnership
_command_signature = ownership_module._command_signature
process_control_module = import_module("comsol_mcp.jobs.process_control")
terminate_exact = process_control_module.terminate_exact
verify_absent = process_control_module.verify_absent

SCHEMA_NAME = "comsol_mcp.surrogate_fem_eval_probe"
SCHEMA_VERSION = "1.0.0"
EXPECTED_BACKEND = {"major": 6, "minor": 4, "patch": 0, "build": 293}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", choices=["RUN_REAL_COMSOL"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--runtime-root", type=Path, default=Path("D:/comsol_runtime"))
    parser.add_argument("--cores", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=1200.0)
    parser.add_argument("--minimum-free-gb", type=float, default=2.0)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    return parser


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_receipt(path: Path, value: dict[str, Any]) -> None:
    atomic_write_json_exclusive(path, value)


def _git_identity() -> dict:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, check=True, capture_output=True, text=True
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError) as exc:
        return {"commit": None, "dirty_entry_count": None, "error_type": type(exc).__name__}
    return {"commit": commit, "dirty_entry_count": len(dirty)}


def _process_identity(pid: int) -> dict:
    process = psutil.Process(pid)
    with process.oneshot():
        return {
            "pid": process.pid,
            "parent_pid": process.ppid(),
            "process_create_time": process.create_time(),
            "name": process.name(),
            "command_line": list(process.cmdline()),
            "command_signature": _command_signature(list(process.cmdline())),
        }


def _descendant_identities(pid: int) -> dict:
    try:
        children = psutil.Process(pid).children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        return {"complete": False, "error": f"{type(exc).__name__}: {exc}", "identities": []}
    identities = []
    incomplete = False
    detail = None
    for child in children:
        try:
            identities.append(_process_identity(child.pid))
        except psutil.NoSuchProcess:
            continue
        except (psutil.AccessDenied, psutil.ZombieProcess) as exc:
            incomplete = True
            detail = f"{type(exc).__name__}: {exc}"
    return {
        "complete": not incomplete,
        "error": None if not incomplete else (detail or "unreadable"),
        "identities": sorted(
            identities, key=lambda item: (item["process_create_time"], item["pid"])
        ),
    }


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
        "lease_state": lease.get("state"),
        "active_job_count": durable.get("active_count"),
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


def _terminate_owned_tree(
    process: subprocess.Popen, captured: list[dict] | tuple[dict, ...] = ()
) -> dict:
    errors = []
    if process.poll() is None:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception as exc:
            errors.append({"stage": "direct_tree", "type": type(exc).__name__})
        try:
            process.wait(timeout=15)
        except Exception as exc:
            errors.append({"stage": "direct_wait", "type": type(exc).__name__})
    for identity in reversed(list(captured)):
        try:
            terminate_exact(identity, force=True)
        except Exception as exc:
            errors.append({"stage": "descendant", "type": type(exc).__name__})
    try:
        deadline = time.monotonic() + 5.0
        while True:
            verification = verify_absent(list(captured))
            if verification.get("absent") is True or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    except Exception as exc:
        errors.append({"stage": "verification", "type": type(exc).__name__})
        verification = {"absent": False, "verdicts": []}
    return {
        "descendant_verification": verification,
        "errors": errors,
        "passed": process.poll() is not None
        and verification.get("absent") is True
        and not errors,
    }


def _select_expected_backend(backends: list[dict]) -> dict:
    matching = [
        backend
        for backend in backends
        if all(int(backend.get(key, -1)) == value for key, value in EXPECTED_BACKEND.items())
    ]
    if len(matching) != 1:
        raise RuntimeError(f"expected exactly one COMSOL 6.4.0.293 backend, found {len(matching)}")
    return {"name": str(matching[0].get("name")), "build": int(matching[0].get("build"))}


def _run_worker(output: Path, cores: int, workspace: Path) -> int:
    result: dict[str, Any] = {
        "schema_name": f"{SCHEMA_NAME}_worker",
        "schema_version": SCHEMA_VERSION,
        "success": False,
        "python": platform.python_version(),
    }
    client = None
    started = time.monotonic()
    try:
        import mph

        result["backend"] = _select_expected_backend(mph.discovery.find_backends())
        client = mph.Client(cores=cores, version="6.4")
        result["client"] = {"version": str(client.version), "standalone": bool(client.standalone)}

        model = client.create("FemEvalProbe")
        jm = model.java
        jm.param().set("a1", "1.5")
        jm.param().set("a2", "2.5")
        jm.component().create("comp1", True)
        geom = jm.component("comp1").geom().create("geom1", 1)
        del geom
        jm.component("comp1").geom("geom1").create("i1", "Interval")
        jm.component("comp1").geom("geom1").feature("i1").set("coord", ["0", "1"])
        jm.component("comp1").geom("geom1").run()
        jm.component("comp1").physics().create("c1", "CoefficientFormPDE", "geom1")
        jm.study().create("std1")
        jm.study("std1").create("stat", "Stationary")
        jm.study("std1").run()

        result["model_state"] = {
            "datasets": [str(x) for x in list(jm.result().dataset().tags())],
            "solutions": [str(x) for x in list(jm.sol().tags())],
        }

        attempts: list[dict[str, Any]] = []

        def attempt(name: str, call) -> Any:
            entry: dict[str, Any] = {"path": name}
            try:
                value = call()
                entry["ok"] = True
                entry["value"] = None if value is None else str(value)[:400]
                attempts.append(entry)
                return value
            except Exception as exc:
                entry["ok"] = False
                entry["error"] = f"{type(exc).__name__}: {exc}"[:400]
                attempts.append(entry)
                return None

        expr = "sin(a1)*cos(a2)+0.1*a1*a2"

        # Variant 1: Eval node without an explicit dataset.
        attempt("numerical.create('evA','Eval')", lambda: jm.result().numerical().create("evA", "Eval"))
        attempt("evA.set('expr', ...)", lambda: jm.result().numerical("evA").set("expr", expr))
        attempt("evA.getReal() [no data]", lambda: [list(x) for x in jm.result().numerical("evA").getReal()])
        attempt("evA.set('data','dset1')", lambda: jm.result().numerical("evA").set("data", "dset1"))
        attempt("evA.getReal() [dset1]", lambda: [list(x) for x in jm.result().numerical("evA").getReal()])
        attempt("evA.set('unit','')", lambda: jm.result().numerical("evA").set("unit", ""))
        attempt("evA.getReal() [unit]", lambda: [list(x) for x in jm.result().numerical("evA").getReal()])
        attempt("evA.set('inner','first')", lambda: jm.result().numerical("evA").set("inner", "first"))
        attempt("evA.getReal() [inner first]", lambda: [list(x) for x in jm.result().numerical("evA").getReal()])
        attempt(
            "evA.set('selection', 1)",
            lambda: jm.result().numerical("evA").set("selection", 1),
        )
        attempt("evA.getReal() [selection 1]", lambda: [list(x) for x in jm.result().numerical("evA").getReal()])
        attempt("evA.getData()", lambda: str(jm.result().numerical("evA").getData())[:300])
        attempt(
            "evA.properties",
            lambda: [str(x) for x in list(jm.result().numerical("evA").properties())],
        )

        # Variant 2: Global evaluation node.
        attempt("numerical.create('evB','Global')", lambda: jm.result().numerical().create("evB", "Global"))
        attempt("evB.set('expr', ...)", lambda: jm.result().numerical("evB").set("expr", expr))
        attempt("evB.set('data','dset1')", lambda: jm.result().numerical("evB").set("data", "dset1"))
        attempt("evB.getReal()", lambda: [list(x) for x in jm.result().numerical("evB").getReal()])

        # Variant 3: interpolate the solved solution then evaluate.
        attempt("numerical.create('evC','Interp')", lambda: jm.result().numerical().create("evC", "Interp"))
        attempt("evC.set('expr','u')", lambda: jm.result().numerical("evC").set("expr", "u"))
        attempt("evC.set('data','dset1')", lambda: jm.result().numerical("evC").set("data", "dset1"))
        attempt("evC.set('coord','0.5')", lambda: jm.result().numerical("evC").set("coord", "0.5"))
        attempt("evC.getReal()", lambda: [list(x) for x in jm.result().numerical("evC").getReal()])

        result["attempts"] = attempts
        result["success"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if client is not None:
            try:
                client.clear()
            except Exception as exc:
                result["client_clear_error"] = f"{type(exc).__name__}: {exc}"
                result["success"] = False
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        _write_receipt(output, result)
    return 0 if result["success"] else 1


def _run_parent(args) -> int:
    output = args.output.expanduser().resolve()
    if output.exists():
        raise ValueError("probe output must use a new path")
    git = _git_identity()
    output.parent.mkdir(parents=True, exist_ok=True)
    workspace = output.parent / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    worker_output = output.with_name(f".{output.stem}.worker.{uuid.uuid4().hex}.json")
    worker_stdout = worker_output.with_suffix(".stdout.log")
    worker_stderr = worker_output.with_suffix(".stderr.log")
    owner = SolverOwnership(args.runtime_root, owner="surrogate-fem-eval-probe")

    receipt: dict[str, Any] = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": False,
        "source": {
            "git": git,
            "script": str(Path(__file__).resolve().relative_to(ROOT)),
            "script_sha256": _sha256_file(Path(__file__).resolve()),
        },
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
    }
    process = None
    lease_acquired = False
    child_identities: dict[tuple[int, float], dict] = {}
    listeners: dict[tuple[int, int], dict] = {}
    started = time.monotonic()
    returncode = 1

    def observe() -> list[dict]:
        inventory = _descendant_identities(os.getpid())
        if not inventory["complete"]:
            raise RuntimeError(f"owned descendant inventory failed: {inventory['error']}")
        for identity in inventory["identities"]:
            child_identities[(identity["pid"], identity["process_create_time"])] = identity
        return inventory["identities"]

    try:
        before = _wait_clean(owner, timeout_seconds=1.0)
        receipt["ownership_before"] = _status_evidence(before)
        if not _status_is_clean(before):
            raise RuntimeError("probe requires no collision, lease, or active durable job")
        preflight = owner.preflight(
            output_path=str(output),
            requested_version="6.4",
            minimum_free_gb=args.minimum_free_gb,
        )
        receipt["preflight"] = preflight
        if not preflight.get("ready"):
            raise RuntimeError(f"solver preflight failed: {preflight.get('blockers')}")
        claim = owner.acquire(mode="surrogate-fem-eval-probe")
        receipt["lease_claim"] = claim
        if not claim.get("success"):
            raise RuntimeError(claim.get("error", "solver lease acquisition failed"))
        lease_acquired = True

        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            "--confirm",
            "RUN_REAL_COMSOL",
            "--worker-output",
            str(worker_output),
            "--cores",
            str(args.cores),
        ]
        with worker_stdout.open("wb") as out_handle, worker_stderr.open("wb") as err_handle:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                stdout=out_handle,
                stderr=err_handle,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            deadline = time.monotonic() + args.timeout_seconds
            timed_out = False
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    timed_out = True
                    _terminate_owned_tree(process)
                    break
                if not owner.heartbeat(refresh_server_processes=True):
                    raise RuntimeError("solver lease heartbeat failed")
                descendants = observe()
                ports = _listener_inventory({item["pid"] for item in descendants})
                for listener in ports.get("listeners", []):
                    listeners[(listener["pid"], listener["port"])] = listener
                time.sleep(0.25)
            observe()
            process.wait(timeout=15)
        receipt["worker_execution"] = {
            "returncode": process.returncode,
            "timed_out": timed_out,
            "stderr_tail": worker_stderr.read_bytes().decode("utf-8", errors="replace")[-2000:],
        }
        if worker_output.is_file():
            receipt["worker_result"] = json.loads(worker_output.read_text(encoding="utf-8"))
        else:
            receipt["worker_result"] = None
        active = owner.status(require_fresh_inventory=True)
        receipt["ownership_during"] = _status_evidence(active)
        worker_result = receipt.get("worker_result") or {}
        if process.returncode != 0 or timed_out or worker_result.get("success") is not True:
            raise RuntimeError("probe did not satisfy its evidence contract")
        returncode = 0
    except Exception as exc:
        receipt["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        cleanup_errors = []
        process_cleanup = None
        if process is not None:
            try:
                process_cleanup = _terminate_owned_tree(process, tuple(child_identities.values()))
            except Exception as exc:
                cleanup_errors.append({"stage": "process", "type": type(exc).__name__})
        if lease_acquired:
            try:
                owner.release()
            except Exception as exc:
                cleanup_errors.append({"stage": "lease_release", "type": type(exc).__name__})
        after = None
        try:
            after = _wait_clean(owner)
            receipt["ownership_after"] = _status_evidence(after)
        except Exception as exc:
            cleanup_errors.append({"stage": "ownership_after", "type": type(exc).__name__})
        remaining = {"complete": False, "identities": []}
        try:
            remaining = _descendant_identities(os.getpid())
        except Exception as exc:
            cleanup_errors.append({"stage": "descendants_after", "type": type(exc).__name__})
        final_listeners = {"complete": False, "listeners": []}
        try:
            final_listeners = _listener_inventory(
                {item["pid"] for item in [*child_identities.values(), *remaining["identities"]]}
            )
        except Exception as exc:
            cleanup_errors.append({"stage": "listeners_after", "type": type(exc).__name__})
        cleanup_passed = (
            not cleanup_errors
            and isinstance(after, dict)
            and _status_is_clean(after)
            and remaining["complete"] is True
            and not remaining["identities"]
            and final_listeners.get("complete") is True
            and final_listeners.get("listeners") == []
            and (process_cleanup is None or process_cleanup.get("passed") is True)
        )
        receipt["cleanup"] = {
            "lease_absent": isinstance(after, dict)
            and after.get("lease", {}).get("state") == "absent",
            "owned_descendants_absent": remaining["complete"] is True
            and not remaining["identities"],
            "errors": cleanup_errors,
            "passed": cleanup_passed,
        }
        receipt["duration_seconds"] = round(time.monotonic() - started, 3)
        receipt["success"] = returncode == 0 and cleanup_passed
        if not receipt["success"]:
            returncode = 1
        try:
            _write_receipt(output, receipt)
        finally:
            worker_output.unlink(missing_ok=True)
            worker_stdout.unlink(missing_ok=True)
            worker_stderr.unlink(missing_ok=True)
    print(output)
    return returncode


def main() -> int:
    args = _parser().parse_args()
    if not 1 <= args.cores <= 64:
        raise SystemExit("--cores must be in 1..64")
    if args.worker:
        if args.confirm != "RUN_REAL_COMSOL" or args.worker_output is None:
            raise SystemExit("worker mode requires --confirm RUN_REAL_COMSOL and --worker-output")
        resolved = args.worker_output.resolve()
        return _run_worker(resolved, args.cores, resolved.parent / "workspace")
    if args.confirm != "RUN_REAL_COMSOL" or args.output is None:
        raise SystemExit("licensed probe requires --confirm RUN_REAL_COMSOL and --output")
    try:
        return _run_parent(args)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
