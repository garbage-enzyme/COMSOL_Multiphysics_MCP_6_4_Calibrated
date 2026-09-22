"""Licensed diagnostic: find the exact DNN data-source contract by experiment.

The reference tables and error messages do not state the accepted training-data
format.  This probe tries a bounded set of declared configurations against real
COMSOL and records, for each, the exact outcome, so the adapter can be built on
observed behaviour instead of inference.
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

SCHEMA_NAME = "comsol_mcp.surrogate_dnn_data_diagnostic"
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
        "error": None if not incomplete else (detail or "child identities unreadable"),
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
    return {
        "name": str(matching[0].get("name")),
        "build": int(matching[0].get("build")),
        "root": str(matching[0].get("root")),
    }


def _dataset_variants(workspace: Path) -> dict[str, Path]:
    """Write the bounded set of candidate dataset encodings."""
    rows: list[tuple[float, float, float]] = []
    for index in range(48):
        a1 = 1.0 + (index % 8) * 0.25
        a2 = 2.0 + (index // 8) * 0.5
        rows.append((a1, a2, math.sin(a1) * math.cos(a2) + 0.1 * a1 * a2))

    variants: dict[str, Path] = {}
    # A: header + CRLF, space after comma
    path_a = workspace / "variant_a.csv"
    lines = ["a1, a2, qoi"] + [f"{a:.9f}, {b:.9f}, {c:.12f}" for a, b, c in rows]
    path_a.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
    variants["header_crlf_spaced"] = path_a

    # B: header + CRLF, no space after comma
    path_b = workspace / "variant_b.csv"
    lines = ["a1,a2,qoi"] + [f"{a:.9f},{b:.9f},{c:.12f}" for a, b, c in rows]
    path_b.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
    variants["header_crlf_compact"] = path_b

    # C: no header + CRLF (COMSOL then derives col1/col2/col3)
    path_c = workspace / "variant_c.csv"
    lines = [f"{a:.9f},{b:.9f},{c:.12f}" for a, b, c in rows]
    path_c.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
    variants["noheader_crlf"] = path_c

    # D: semicolon separated with dseparator expectation
    path_d = workspace / "variant_d.csv"
    lines = ["a1;a2;qoi"] + [f"{a:.9f};{b:.9f};{c:.12f}" for a, b, c in rows]
    path_d.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
    variants["header_semicolon"] = path_d
    return variants


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
        import jpype
        import mph

        from comsol_mcp.surrogate.dnn_adapter import (
            DNN_FUNCTION_TAG,
            apply_surrogate_configuration,
            bind_data_source_and_arguments,
            build_dnn_configuration,
            train_surrogate,
        )
        from comsol_mcp.surrogate.dnn_clientapi_backend import ClientapiSurrogateDnnBackend

        result["backend"] = _select_expected_backend(mph.discovery.find_backends())
        client = mph.Client(cores=cores, version="6.4")
        result["client"] = {
            "version": str(client.version),
            "standalone": bool(client.standalone),
            "port": client.port,
        }

        variants = _dataset_variants(workspace)
        result["dataset_variants"] = {
            name: {
                "path": str(path),
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
                "first_line": path.read_text(encoding="utf-8").splitlines()[0],
            }
            for name, path in variants.items()
        }

        trials: list[dict[str, Any]] = []
        for name, path in variants.items():
            for validation_mode, test_mode in (
                ("fraction", "none"),
                ("random", "none"),
            ):
                trial: dict[str, Any] = {
                    "dataset_variant": name,
                    "validation_mode": validation_mode,
                    "test_mode": test_mode,
                }
                try:
                    model = client.create(f"Diag{name[:12]}{validation_mode[:4]}")
                    jm = model.java
                    jm.param().set("a1", "1.0")
                    jm.param().set("a2", "2.0")
                    jm.component().create("comp1", True)
                    backend = ClientapiSurrogateDnnBackend(model)
                    configuration = build_dnn_configuration(
                        configuration_id=f"diag-{name}",
                        input_features=["a1", "a2"],
                        output_features=["qoi"],
                        hidden_layers=[8],
                        activation="tanh",
                        optimizer="adam",
                        loss="mse",
                        learning_rate=1e-2,
                        batch_size=8,
                        maximum_epochs=10,
                        seed=17,
                        validation_mode=validation_mode,
                        test_mode=test_mode,
                    )
                    applied = apply_surrogate_configuration(backend, configuration)
                    trial["apply_success"] = applied["success"]
                    if not applied["success"]:
                        trial["apply_error"] = applied.get("error")
                        trials.append(trial)
                        client.clear()
                        continue
                    bound = bind_data_source_and_arguments(
                        backend, configuration, dataset_path=str(path)
                    )
                    trial["binding"] = {
                        "success": bound.get("success"),
                        "binding_mode": bound.get("binding_mode"),
                        "column_keys": bound.get("column_keys"),
                        "error": bound.get("error"),
                    }
                    dnn = backend.get_dnn_function(DNN_FUNCTION_TAG)
                    trial["pre_train"] = {
                        name2: backend.read_property(dnn, name2)
                        for name2 in (
                            "source",
                            "sourcetype",
                            "columnKeys",
                            "columnType",
                            "funcs",
                            "filecolumns",
                            "validation",
                            "test",
                            "fraction",
                            "activation",
                            "layertype",
                            "outfeatures",
                        )
                    }
                    trained = train_surrogate(backend, run_test=False)
                    trial["train"] = {
                        "success": trained.get("success"),
                        "error": trained.get("error"),
                        "trainingloss": trained.get("trainingloss"),
                        "trained_chksum": trained.get("trained_chksum"),
                        "layerconfig": trained.get("layerconfig"),
                    }
                    client.clear()
                except Exception as exc:
                    trial["fatal"] = f"{type(exc).__name__}: {exc}"
                    try:
                        client.clear()
                    except Exception:
                        pass
                trials.append(trial)
                if trial.get("train", {}).get("success") is True:
                    break
            if trials and trials[-1].get("train", {}).get("success") is True:
                break

        result["trials"] = trials
        result["any_training_succeeded"] = any(
            item.get("train", {}).get("success") is True for item in trials
        )
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
        _write_receipt(output, result)
    return 0 if result["success"] else 1


def _run_parent(args) -> int:
    output = args.output.expanduser().resolve()
    if output.exists():
        raise ValueError("diagnostic output must use a new path")
    git = _git_identity()
    output.parent.mkdir(parents=True, exist_ok=True)
    workspace = output.parent / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    worker_output = output.with_name(f".{output.stem}.worker.{uuid.uuid4().hex}.json")
    worker_stdout = worker_output.with_suffix(".stdout.log")
    worker_stderr = worker_output.with_suffix(".stderr.log")
    owner = SolverOwnership(args.runtime_root, owner="surrogate-dnn-data-diagnostic")

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
            raise RuntimeError("diagnostic requires no collision, lease, or active durable job")
        preflight = owner.preflight(
            output_path=str(output),
            requested_version="6.4",
            minimum_free_gb=args.minimum_free_gb,
        )
        receipt["preflight"] = preflight
        if not preflight.get("ready"):
            raise RuntimeError(f"solver preflight failed: {preflight.get('blockers')}")
        claim = owner.acquire(mode="surrogate-dnn-data-diagnostic")
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
            "stderr_tail": worker_stderr.read_bytes().decode("utf-8", errors="replace")[-3000:],
        }
        if worker_output.is_file():
            receipt["worker_result"] = json.loads(worker_output.read_text(encoding="utf-8"))
        else:
            receipt["worker_result"] = None
        active = owner.status(require_fresh_inventory=True)
        receipt["ownership_during"] = _status_evidence(active)
        worker_result = receipt.get("worker_result") or {}
        if process.returncode != 0 or timed_out or worker_result.get("success") is not True:
            raise RuntimeError("diagnostic did not satisfy its evidence contract")
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
        raise SystemExit("licensed diagnostic requires --confirm RUN_REAL_COMSOL and --output")
    try:
        return _run_parent(args)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
