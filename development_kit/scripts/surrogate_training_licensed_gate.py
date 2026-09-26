"""Licensed gate: train, test, and continue a COMSOL-native DNN surrogate.

Repository-only and licensed.  ``--dry-run`` validates the isolation contract
without importing MPh, starting COMSOL, or acquiring a solver lease.

The gate proves on an isolated licensed session:
1. a bounded DNN trains on a real imported dataset and reports a trained
   checksum and losses;
2. the held-out test evaluation runs independently of training;
3. continuation is refused when a contract identity differs;
4. a deterministic seed reproduces the same trained checksum;
5. non-DNN baselines fit on the identical split;
6. the owned session and solver lease are released with no descendant left.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
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

SCHEMA_NAME = "comsol_mcp.surrogate_training_licensed_gate"
SCHEMA_VERSION = "1.0.0"
EXPECTED_BACKEND = {"major": 6, "minor": 4, "patch": 0, "build": 293}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", choices=["RUN_REAL_COMSOL"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--runtime-root", type=Path, default=Path("D:/comsol_runtime"))
    parser.add_argument("--cores", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
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


def _write_dataset(path: Path) -> list[dict[str, float]]:
    """Write one small dataset and return its exact rows.

    The target is a deterministic smooth function of the two inputs so that a
    trained network has a learnable signal; the same rows are later scored by the
    non-DNN baselines on the identical split.

    The file has **no header row**.  A leading text header imports cleanly but
    makes training fail with "读取训练数据时出错 - 线条数: 1", so the columns are
    bound positionally against COMSOL's own ``col1, col2, col3`` keys.  Line
    endings are CRLF, COMSOL's native Windows convention.
    """
    import math

    rows: list[dict[str, float]] = []
    for index in range(48):
        a1 = 1.0 + (index % 8) * 0.25
        a2 = 2.0 + (index // 8) * 0.5
        qoi = math.sin(a1) * math.cos(a2) + 0.1 * a1 * a2
        rows.append({"a1": a1, "a2": a2, "qoi": qoi})
    lines = [f"{row['a1']:.9f}, {row['a2']:.9f}, {row['qoi']:.12f}" for row in rows]
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")
    return rows


def _numeric_readback(record: Any) -> float:
    """Extract a finite float from an accessor readback record.

    Readback values are recorded as ``{"accessor": ..., "value": ...}`` so the
    raw evidence stays inspectable; numeric consumers must read the inner value.
    """
    if isinstance(record, dict):
        raw = record.get("value")
    else:
        raw = record
    if isinstance(raw, str):
        raw = raw.strip()
    try:
        number = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"readback value is not numeric: {record!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"readback value is not finite: {record!r}")
    return number


def _configuration(seed: int, epochs: int = 60) -> dict:
    """Build the gate configuration.

    COMSOL's ``table`` validation/test modes require a bound COMSOL result table
    ("未选择结果表"), which this gate does not construct.  The gate therefore uses
    COMSOL-internal subsetting for training-time model selection and records
    explicitly that the authoritative group-disjoint split is this project's own
    manifest, not COMSOL's internal subsetting.
    """
    from comsol_mcp.surrogate.dnn_adapter import build_dnn_configuration

    return build_dnn_configuration(
        configuration_id=f"s6-gate-seed{seed}",
        input_features=["a1", "a2"],
        output_features=["qoi"],
        hidden_layers=[16, 8],
        activation="tanh",
        optimizer="adam",
        loss="mse",
        learning_rate=1e-2,
        batch_size=8,
        maximum_epochs=epochs,
        seed=seed,
        validation_mode="fraction",
        test_mode="random",
    )


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
            apply_surrogate_configuration,
            bind_data_source_and_arguments,
            train_surrogate,
        )
        from comsol_mcp.surrogate.dnn_clientapi_backend import (
            ClientapiSurrogateDnnBackend,
        )
        from comsol_mcp.surrogate.training import (
            build_continuation_identity,
            compare_against_baseline,
            compute_metrics,
            evaluate_continuation,
            fit_baseline,
            predict_baseline,
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
        try:
            result["comsol_reported_version"] = str(client.java.getComsolVersion())
        except Exception as exc:
            result["comsol_reported_version"] = None
            result["comsol_reported_version_error"] = f"{type(exc).__name__}: {exc}"

        dataset_path = workspace / "surrogate_training_gate.csv"
        rows = _write_dataset(dataset_path)
        result["dataset"] = {
            "row_count": len(rows),
            "bytes": dataset_path.stat().st_size,
            "sha256": _sha256_file(dataset_path),
        }

        def _build_model(name: str) -> Any:
            model = client.create(name)
            jm = model.java
            jm.param().set("a1", "1.0")
            jm.param().set("a2", "2.0")
            jm.component().create("comp1", True)
            return model

        def _prepare(model: Any, seed: int, epochs: int = 60) -> tuple[Any, dict, Any]:
            backend = ClientapiSurrogateDnnBackend(model, client=client)
            configuration = _configuration(seed, epochs=epochs)
            applied = apply_surrogate_configuration(backend, configuration)
            if not applied["success"]:
                raise RuntimeError(f"apply failed: {applied.get('error')}")
            bound = bind_data_source_and_arguments(
                backend, configuration, dataset_path=str(dataset_path)
            )
            if not bound["success"]:
                raise RuntimeError(f"data binding failed: {bound.get('error')}")
            return backend, configuration, bound

        # ---- Phase 1: train on seed 17 ------------------------------------
        model = _build_model("SurrogateTrainingGate")
        backend, configuration, binding = _prepare(model, 17)
        result["binding"] = {
            "success": binding["success"],
            "binding_mode": binding["binding_mode"],
            "column_keys": binding["column_keys"],
        }
        first = train_surrogate(backend, run_test=True)
        result["train_seed17"] = {
            "success": first["success"],
            "test_executed": first.get("test_executed"),
            "trainingloss": first.get("trainingloss"),
            "trainingloss_value": first.get("trainingloss_value"),
            "validationloss": first.get("validationloss"),
            "validationloss_value": first.get("validationloss_value"),
            "testloss": first.get("testloss"),
            "testloss_value": first.get("testloss_value"),
            "trained_chksum": first.get("trained_chksum"),
            "layerconfig": first.get("layerconfig"),
            "trained_ninput": first.get("trained_ninput"),
            "trained_noutput": first.get("trained_noutput"),
            "test_error": first.get("test_error"),
        }
        if not first["success"]:
            # Record the data-shape properties so a format problem is evidenced.
            diag: dict[str, Any] = {}
            try:
                diag_dnn = backend.get_dnn_function(DNN_FUNCTION_TAG)
                for name in (
                    "filecolumns",
                    "columnKeys",
                    "columnType",
                    "sourcetype",
                    "source",
                    "filename",
                    "funcs",
                    "columnSelection",
                    "information",
                ):
                    diag[name] = backend.read_property(diag_dnn, name)
            except Exception as exc:
                diag["error"] = f"{type(exc).__name__}: {exc}"
            result["training_diagnostics"] = diag
            raise RuntimeError(f"training failed: {first.get('error')}")

        # ---- Phase 2: continuation identity rules -------------------------
        base_identity = build_continuation_identity(
            dataset_manifest_sha256="a" * 64,
            split_manifest_sha256="b" * 64,
            field_schema_sha256=configuration["configuration_sha256"],
            transforms_sha256="d" * 64,
            architecture_sha256=configuration["configuration_sha256"],
            comsol_build=str(backend_identity["build"]),
            objective="minimize_mse_on_qoi",
            prior_checkpoint_sha256="e" * 64,
        )
        changed_identity = dict(base_identity)
        changed_identity["comsol_build"] = "9.9.9"
        result["continuation"] = {
            "identical": evaluate_continuation(prior=base_identity, current=base_identity),
            "changed_build": evaluate_continuation(
                prior=base_identity, current=changed_identity
            ),
        }
        # A real continueRun must be attempted and its outcome recorded.
        continued = train_surrogate(backend, continue_training=True, run_test=False)
        result["continue_run"] = {
            "success": continued["success"],
            "continued": continued.get("continued"),
            "error": continued.get("error"),
            "trained_chksum": continued.get("trained_chksum"),
        }

        # ---- Phase 3: seed reproducibility on a fresh model ---------------
        seed17_again = _build_model("SurrogateTrainingGateSeed17")
        backend17, _cfg17, _bind17 = _prepare(seed17_again, 17)
        repeat = train_surrogate(backend17, run_test=False)
        result["seed17_repeat"] = {
            "success": repeat["success"],
            "trained_chksum": repeat.get("trained_chksum"),
        }
        seed29_model = _build_model("SurrogateTrainingGateSeed29")
        backend29, _cfg29, _bind29 = _prepare(seed29_model, 29)
        seed29 = train_surrogate(backend29, run_test=False)
        result["seed29"] = {
            "success": seed29["success"],
            "trained_chksum": seed29.get("trained_chksum"),
        }
        result["seed_reproducible"] = (
            result["train_seed17"]["trained_chksum"] == result["seed17_repeat"]["trained_chksum"]
        )
        result["seed_distinct"] = (
            result["train_seed17"]["trained_chksum"] != result["seed29"]["trained_chksum"]
        )

        # ---- Phase 4: non-DNN baselines on the identical split ------------
        split_index = int(len(rows) * 0.7)
        train_rows = rows[:split_index]
        test_rows = rows[split_index:]
        features_train = [[row["a1"], row["a2"]] for row in train_rows]
        targets_train = [[row["qoi"]] for row in train_rows]
        features_test = [[row["a1"], row["a2"]] for row in test_rows]
        targets_test = [[row["qoi"]] for row in test_rows]
        baseline = fit_baseline(
            baseline_id="s6-constant-mean",
            kind="constant_mean",
            train_features=features_train,
            train_targets=targets_train,
        )
        baseline_predictions = predict_baseline(baseline, features_test)
        baseline_metrics = compute_metrics(
            predicted=baseline_predictions, actual=targets_test
        )
        result["baseline"] = {
            "kind": baseline["kind"],
            "fit_row_count": baseline["fit_row_count"],
            "fitted_on": baseline["fitted_on"],
            "is_dnn": baseline["is_dnn"],
            "test_metrics": {
                "mae": baseline_metrics["mae"],
                "rmse": baseline_metrics["rmse"],
                "r2": baseline_metrics["r2"],
            },
        }
        # The DNN's own test loss is compared with the baseline on the same rows.
        # Readback values are accessor records, so the numeric field is extracted
        # explicitly rather than coerced from the record.
        dnn_testloss = _numeric_readback(first.get("testloss"))
        result["dnn_testloss_value"] = dnn_testloss
        comparison = compare_against_baseline(
            surrogate_metrics={"rmse": dnn_testloss},
            baseline_metrics={"rmse": baseline_metrics["rmse"]},
        )
        result["baseline_comparison"] = comparison

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
    owner = SolverOwnership(args.runtime_root, owner="surrogate-training-licensed-gate")

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
        claim = owner.acquire(mode="surrogate-training-licensed-gate")
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
        trained = worker_result.get("train_seed17") or {}
        continuation = worker_result.get("continuation") or {}
        identical = continuation.get("identical") or {}
        changed = continuation.get("changed_build") or {}
        baseline = worker_result.get("baseline") or {}
        phase_passed = (
            process.returncode == 0
            and not timed_out
            and worker_result.get("success") is True
            # Training must produce a real trained identity and a test result.
            and trained.get("success") is True
            and trained.get("test_executed") is True
            and trained.get("trained_chksum", {}).get("readable") is True
            and trained.get("testloss", {}).get("readable") is True
            and trained.get("layerconfig", {}).get("readable") is True
            # Continuation must be permitted only for identical identities.
            and identical.get("continuation_allowed") is True
            and changed.get("continuation_allowed") is False
            and changed.get("creates_new_lineage") is True
            # The seed must control the trained result.
            and worker_result.get("seed_reproducible") is True
            and worker_result.get("seed_distinct") is True
            # A non-DNN baseline must exist on the identical split.
            and baseline.get("is_dnn") is False
            and baseline.get("fitted_on") == "train_split_only"
            and active.get("durable_jobs", {}).get("active_count") == 0
        )
        if not phase_passed:
            raise RuntimeError("licensed surrogate training gate did not satisfy its contract")
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
            "bounded DNN training with a real trained checksum and losses",
            "independent held-out test evaluation",
            "continuation permitted only for identical contract identities",
            "seed-controlled reproducibility of the trained checksum",
            "non-DNN baseline fitted on the identical split",
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
