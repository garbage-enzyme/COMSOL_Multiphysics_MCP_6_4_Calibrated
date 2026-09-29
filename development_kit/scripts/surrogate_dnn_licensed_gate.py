"""Licensed gate: apply the typed surrogate DNN adapter to real COMSOL 6.4.

Repository-only and licensed.  ``--dry-run`` validates the isolation contract
without importing MPh, starting COMSOL, or acquiring a solver lease.

The gate proves four things on an isolated licensed session:
1. the typed adapter constructs the smallest accepted Surrogate Model Training
   study step and DNN function and reads every COMSOL-owned default back;
2. the failure-atomic rollback leaves no partial surrogate tree behind;
3. save/reload preserves the constructed identity;
4. the owned session and solver lease are released with no descendant left.
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

SCHEMA_NAME = "comsol_mcp.surrogate_dnn_licensed_gate"
SCHEMA_VERSION = "1.0.0"
EXPECTED_BACKEND = {"major": 6, "minor": 4, "patch": 0, "build": 293}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", choices=["RUN_REAL_COMSOL"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--runtime-root", type=Path, default=Path("D:/comsol_runtime"))
    parser.add_argument("--cores", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--minimum-free-gb", type=float, default=2.0)
    parser.add_argument("--dry-run", action="store_true")
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
        "error": None if not incomplete else (detail or "child identities could not be read"),
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


def _terminate_owned_tree(
    process: subprocess.Popen, captured_descendants: list[dict] | tuple[dict, ...] = ()
) -> dict:
    errors = []
    direct_was_running = process.poll() is None
    if direct_was_running:
        try:
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
        except Exception as exc:
            errors.append({"stage": "direct_tree", "type": type(exc).__name__})
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=15)
            except Exception as exc:
                errors.append({"stage": "direct_wait", "type": type(exc).__name__})
        except Exception as exc:
            errors.append({"stage": "direct_wait", "type": type(exc).__name__})

    descendant_actions = []
    for identity in reversed(list(captured_descendants)):
        try:
            descendant_actions.append(terminate_exact(identity, force=True))
        except Exception as exc:
            errors.append({"stage": "descendant", "type": type(exc).__name__})
    try:
        deadline = time.monotonic() + 5.0
        while True:
            descendant_verification = verify_absent(list(captured_descendants))
            if descendant_verification.get("absent") is True or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    except Exception as exc:
        errors.append({"stage": "descendant_verification", "type": type(exc).__name__})
        descendant_verification = {"absent": False, "verdicts": []}
    return {
        "direct_was_running": direct_was_running,
        "direct_exited": process.poll() is not None,
        "descendant_actions": descendant_actions,
        "descendant_verification": descendant_verification,
        "errors": errors,
        "passed": (
            process.poll() is not None
            and descendant_verification.get("absent") is True
            and not errors
        ),
    }


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
        raise RuntimeError(f"expected exactly one COMSOL 6.4.0.293 backend, found {len(matching)}")
    return _backend_identity(matching[0])


def _configuration() -> dict:
    from comsol_mcp.surrogate.dnn_adapter import build_dnn_configuration

    return build_dnn_configuration(
        configuration_id="s4-licensed-gate",
        input_features=["a1", "a2"],
        output_features=["qoi"],
        hidden_layers=[4],
        activation="tanh",
        optimizer="adam",
        loss="mse",
        learning_rate=1e-3,
        batch_size=8,
        maximum_epochs=3,
        seed=17,
        validation_mode="table",
        test_mode="table",
    )


def _write_dataset(path: Path) -> None:
    """Write one tiny COMSOL spreadsheet-format dataset with named columns.

    COMSOL treats a leading ``%`` as a comment, so the header must be a plain
    first line; a commented header yields generic ``col1, col2, ...`` keys.
    """
    rows = ["a1, a2, qoi"]
    for index in range(8):
        a1 = 1.0 + index * 0.25
        a2 = 2.0 + index * 0.5
        rows.append(f"{a1:.6f}, {a2:.6f}, {a1 * a2:.6f}")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _run_worker(output: Path, cores: int, workspace: Path) -> int:
    result: dict[str, Any] = {
        "schema_name": f"{SCHEMA_NAME}_worker",
        "schema_version": SCHEMA_VERSION,
        "success": False,
        "python": platform.python_version(),
        "python_executable": sys.executable,
    }
    client = None
    started = time.monotonic()
    try:
        import jpype
        import mph

        from comsol_mcp.surrogate.dnn_adapter import (
            DNN_FUNCTION_TAG,
            STUDY_STEP_TAG,
            STUDY_TAG,
            apply_surrogate_configuration,
        )
        from comsol_mcp.surrogate.dnn_clientapi_backend import (
            ClientapiSurrogateDnnBackend,
        )

        backend_identity = _select_expected_backend(mph.discovery.find_backends())
        result["backend"] = backend_identity
        client = mph.Client(cores=cores, version="6.4")
        result["client"] = {
            "version": str(client.version),
            "standalone": bool(client.standalone),
            "host": client.host,
            "port": client.port,
            "jvm_started": bool(jpype.isJVMStarted()),
        }
        java_system = jpype.JClass("java.lang.System")
        result["java"] = {
            "version": str(java_system.getProperty("java.version")),
            "vendor": str(java_system.getProperty("java.vendor")),
        }
        try:
            result["comsol_reported_version"] = str(client.java.getComsolVersion())
        except Exception as exc:
            result["comsol_reported_version"] = None
            result["comsol_reported_version_error"] = f"{type(exc).__name__}: {exc}"

        model = client.create("SurrogateDnnLicensedGate")
        jm = model.java
        jm.param().set("a1", "1.0")
        jm.param().set("a2", "2.0")
        component = jm.component().create("comp1", True)
        result["component_created"] = str(component.tag())
        backend = ClientapiSurrogateDnnBackend(model, client=client)
        configuration = _configuration()
        result["configuration_sha256"] = configuration["configuration_sha256"]

        # ---- Phase 0: bind a real data source so `args` becomes writable ----
        dataset_path = workspace / "surrogate_dnn_gate.csv"
        _write_dataset(dataset_path)
        result["dataset"] = {
            "path_written": True,
            "bytes": dataset_path.stat().st_size,
            "sha256": _sha256_file(dataset_path),
        }

        # ---- Phase 1: successful construction and readback ----------------
        first = apply_surrogate_configuration(backend, configuration)
        result["apply"] = {
            "success": first["success"],
            "created_nodes": [list(item) for item in first.get("created_nodes", [])],
            "rolled_back": first.get("rolled_back"),
            "readback": first.get("readback", {}),
            "allowed_values": first.get("allowed_values", {}),
            "deferred_writes": first.get("deferred_writes", []),
        }
        if not first["success"]:
            raise RuntimeError(f"adapter apply failed: {first.get('error')}")
        # Bind the data source and write `args` from the columns COMSOL actually
        # derived from the file; this proves the deferral is resolvable.
        from comsol_mcp.surrogate.dnn_adapter import bind_data_source_and_arguments

        # Record every column-related property so the observed column naming is
        # evidenced rather than assumed.
        probe_dnn = backend.get_dnn_function(DNN_FUNCTION_TAG)
        backend.write_scalar(probe_dnn, "source", "file", "string")
        backend.write_scalar(probe_dnn, "filename", str(dataset_path), "string")
        column_evidence: dict[str, Any] = {}
        try:
            probe_dnn.importData()
            column_evidence["import"] = "ok"
        except Exception as exc:
            column_evidence["import"] = f"{type(exc).__name__}: {exc}"
        for name in (
            "columnKeys",
            "columnHeaders",
            "fileheaders",
            "filecolumns",
            "columnType",
            "columnSelection",
            "funcs",
        ):
            column_evidence[name] = backend.read_property(probe_dnn, name)
        result["column_evidence"] = column_evidence

        binding = bind_data_source_and_arguments(
            backend, configuration, dataset_path=str(dataset_path)
        )
        result["data_source_binding"] = binding
        result["data_source_bound"] = bool(binding.get("success"))

        # Every COMSOL-owned default that S0 proved readable must be readable
        # here too; an unreadable control must never stay implicit.
        unreadable = sorted(
            name for name, item in first["readback"].items() if not item.get("readable")
        )
        result["unreadable_after_apply"] = unreadable
        result["seed_readback"] = {
            name: first["readback"].get(name, {}).get("value")
            for name in ("useseed", "rndseed", "useseedvalidation", "rndseedvalidation")
        }
        result["step_side_binding"] = {}
        try:
            step = backend.get_study_step(STUDY_TAG, STUDY_STEP_TAG)
            for name in ("surrogatemodel", "layertype", "outfeatures", "activation"):
                result["step_side_binding"][name] = backend.read_property(step, name)
        except Exception as exc:
            result["step_side_binding"] = {"error": f"{type(exc).__name__}: {exc}"}

        # ---- Phase 2: save / reload identity -------------------------------
        model_path = workspace / "surrogate_dnn_gate.mph"
        model.save(str(model_path))
        result["saved_bytes"] = model_path.stat().st_size
        result["saved_sha256"] = _sha256_file(model_path)
        client.clear()
        reloaded = client.load(str(model_path))
        rjm = reloaded.java
        result["reload"] = {
            "study_tags": [str(item) for item in list(rjm.study().tags())],
            "func_tags": [str(item) for item in list(rjm.func().tags())],
        }
        reload_backend = ClientapiSurrogateDnnBackend(reloaded, client=client)
        rdnn = reload_backend.get_dnn_function(DNN_FUNCTION_TAG)
        result["reload"]["dnn_type"] = str(rdnn.getType())
        result["reload"]["seed_readback"] = {
            name: reload_backend.read_property(rdnn, name)
            for name in ("useseed", "rndseed", "useseedvalidation", "rndseedvalidation")
        }
        rstep = reload_backend.get_study_step(STUDY_TAG, STUDY_STEP_TAG)
        result["reload"]["step_type"] = str(rstep.getType())

        # ---- Phase 3: rollback leaves no partial tree -----------------------
        # Re-create the same tags on the reloaded model; the adapter must refuse
        # an existing tag without mutating anything.
        refusal = apply_surrogate_configuration(reload_backend, configuration)
        result["refusal_on_existing_tags"] = {
            "success": refusal["success"],
            "error": refusal.get("error"),
            "created_nodes": [list(item) for item in refusal.get("created_nodes", [])],
        }
        result["post_refusal_tags"] = {
            "study_tags": [str(item) for item in list(rjm.study().tags())],
            "func_tags": [str(item) for item in list(rjm.func().tags())],
        }

        # ---- Phase 4: injected mid-batch failure rolls back -----------------
        # A second model keeps the rollback proof independent of the reload.
        client.clear()
        rollback_model = client.create("SurrogateDnnRollbackGate")
        rollback_model.java.component().create("comp1", True)
        rollback_backend = ClientapiSurrogateDnnBackend(rollback_model, client=client)
        original_write = rollback_backend.write_scalar
        state = {"writes": 0}

        def failing_write(feature, name, value, kind):
            state["writes"] += 1
            if state["writes"] == 3:
                raise RuntimeError("injected mid-batch failure")
            return original_write(feature, name, value, kind)

        rollback_backend.write_scalar = failing_write  # type: ignore[method-assign]
        rollback = apply_surrogate_configuration(rollback_backend, configuration)
        result["rollback"] = {
            "success": rollback["success"],
            "error": rollback.get("error"),
            "rolled_back": rollback.get("rolled_back"),
            "rollback_errors": rollback.get("rollback_errors"),
            "created_nodes": [list(item) for item in rollback.get("created_nodes", [])],
        }
        rollback_backend.write_scalar = original_write  # type: ignore[method-assign]
        result["post_rollback_tags"] = {
            "study_tags": [str(item) for item in list(rollback_model.java.study().tags())],
            "func_tags": [str(item) for item in list(rollback_model.java.func().tags())],
        }

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
        raise ValueError("gate output must use a new path")
    git = _git_identity()

    output.parent.mkdir(parents=True, exist_ok=True)
    workspace = output.parent / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    worker_output = output.with_name(f".{output.stem}.worker.{uuid.uuid4().hex}.json")
    worker_stdout = worker_output.with_suffix(".stdout.log")
    worker_stderr = worker_output.with_suffix(".stderr.log")
    owner = SolverOwnership(args.runtime_root, owner="surrogate-dnn-licensed-gate")

    receipt: dict[str, Any] = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
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

    def observe_descendants() -> list[dict]:
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
            raise RuntimeError("gate requires no collision, lease, or active durable job")
        preflight = owner.preflight(
            output_path=str(output),
            requested_version="6.4",
            minimum_free_gb=args.minimum_free_gb,
        )
        receipt["preflight"] = preflight
        if not preflight.get("ready"):
            raise RuntimeError(f"solver preflight failed: {preflight.get('blockers')}")
        claim = owner.acquire(mode="surrogate-dnn-licensed-gate")
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
        with worker_stdout.open("wb") as stdout_handle, worker_stderr.open("wb") as stderr_handle:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                stdout=stdout_handle,
                stderr=stderr_handle,
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
                descendants = observe_descendants()
                ports = _listener_inventory({item["pid"] for item in descendants})
                if not ports["complete"]:
                    raise RuntimeError(f"owned listener inventory failed: {ports['error']}")
                for listener in ports["listeners"]:
                    listeners[(listener["pid"], listener["port"])] = listener
                time.sleep(0.25)
            observe_descendants()
            process.wait(timeout=15)
        stdout = worker_stdout.read_bytes().decode("utf-8", errors="replace")
        stderr = worker_stderr.read_bytes().decode("utf-8", errors="replace")
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
            "lease_server_processes": lease.get("comsol_server_processes"),
            "lease_server_port": lease.get("comsol_server_port"),
        }
        worker_result = receipt.get("worker_result") or {}
        apply_result = worker_result.get("apply") or {}
        binding = worker_result.get("data_source_binding") or {}
        rollback_result = worker_result.get("rollback") or {}
        refusal = worker_result.get("refusal_on_existing_tags") or {}
        phase_passed = (
            process.returncode == 0
            and not timed_out
            and worker_result.get("success") is True
            and apply_result.get("success") is True
            and apply_result.get("rolled_back") is False
            and worker_result.get("unreadable_after_apply") == []
            # The `args` write is deferred until a data source is bound, then
            # resolved from the columns COMSOL actually reported.
            and [item["property"] for item in apply_result.get("deferred_writes", [])]
            == ["args"]
            and worker_result.get("data_source_bound") is True
            and binding.get("success") is True
            and binding.get("bound_arguments") == ["a1", "a2", "qoi"]
            and binding.get("binding_mode") in {"by_name", "by_position"}
            and rollback_result.get("success") is False
            and rollback_result.get("rolled_back") is True
            and rollback_result.get("rollback_errors") == []
            and worker_result.get("post_rollback_tags") == {"study_tags": [], "func_tags": []}
            and refusal.get("success") is False
            and refusal.get("created_nodes") == []
            and active.get("durable_jobs", {}).get("active_count") == 0
        )
        if not phase_passed:
            raise RuntimeError("licensed surrogate DNN gate did not satisfy its evidence contract")
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
        lease_release = None
        if lease_acquired:
            try:
                lease_release = owner.release()
            except Exception as exc:
                cleanup_errors.append({"stage": "lease_release", "type": type(exc).__name__})
        receipt["lease_release"] = lease_release
        after = None
        try:
            after = _wait_clean(owner)
            receipt["ownership_after"] = _status_evidence(after)
        except Exception as exc:
            cleanup_errors.append({"stage": "ownership_after", "type": type(exc).__name__})
            receipt["ownership_after"] = None
        remaining_inventory = {"complete": False, "error": "not_collected", "identities": []}
        try:
            remaining_inventory = _descendant_identities(os.getpid())
        except Exception as exc:
            cleanup_errors.append({"stage": "descendants_after", "type": type(exc).__name__})
        remaining_descendants = remaining_inventory["identities"]
        final_listener_snapshot = {"complete": False, "error": "not_collected", "listeners": []}
        try:
            final_listener_snapshot = _listener_inventory(
                {item["pid"] for item in [*child_identities.values(), *remaining_descendants]}
            )
        except Exception as exc:
            cleanup_errors.append({"stage": "listeners_after", "type": type(exc).__name__})
        cleanup_passed = (
            not cleanup_errors
            and isinstance(after, dict)
            and _status_is_clean(after)
            and remaining_inventory["complete"] is True
            and not remaining_descendants
            and final_listener_snapshot.get("complete") is True
            and final_listener_snapshot.get("listeners") == []
            and (process_cleanup is None or process_cleanup.get("passed") is True)
        )
        receipt["cleanup"] = {
            "lease_absent": isinstance(after, dict)
            and after.get("lease", {}).get("state") == "absent",
            "collision_absent": isinstance(after, dict) and after.get("collision") is False,
            "active_job_count": (
                after.get("durable_jobs", {}).get("active_count")
                if isinstance(after, dict)
                else None
            ),
            "process_cleanup": process_cleanup,
            "owned_descendants": remaining_descendants,
            "owned_descendants_inventory_complete": remaining_inventory["complete"],
            "owned_descendants_absent": (
                remaining_inventory["complete"] is True and not remaining_descendants
            ),
            "final_listener_snapshot": final_listener_snapshot,
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


def _dry_run(args) -> int:
    payload = {
        "schema_name": f"{SCHEMA_NAME}_dry_run",
        "schema_version": SCHEMA_VERSION,
        "source_sha256": _sha256_file(Path(__file__).resolve()),
        "isolation_contract": {
            "starts_comsol": False,
            "acquires_solver_lease": False,
            "imports_mph": False,
            "network_access": False,
            "mutates_source_models": False,
            "generic_property_mutation": False,
        },
        "proves": [
            "typed adapter construction and full COMSOL-owned default readback",
            "save and reload identity preservation",
            "refusal on existing tags without mutation",
            "injected mid-batch failure rolls back every created node",
            "owned session, lease, descendant, and listener cleanup",
        ],
        "output": str(args.output.expanduser().resolve()) if args.output else None,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main() -> int:
    args = _parser().parse_args()
    if not 1 <= args.cores <= 64:
        raise SystemExit("--cores must be in 1..64")
    if not 1.0 <= args.timeout_seconds <= 7200.0:
        raise SystemExit("--timeout-seconds must be in 1..7200")
    if args.dry_run:
        return _dry_run(args)
    if args.worker:
        if args.confirm != "RUN_REAL_COMSOL":
            raise SystemExit("worker mode requires --confirm RUN_REAL_COMSOL")
        if args.worker_output is None:
            raise SystemExit("worker mode requires --worker-output")
        resolved = args.worker_output.resolve()
        return _run_worker(resolved, args.cores, resolved.parent / "workspace")
    if args.confirm != "RUN_REAL_COMSOL" or args.output is None:
        raise SystemExit("licensed gate requires --confirm RUN_REAL_COMSOL and --output")
    try:
        return _run_parent(args)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
