"""Licensed gate: export a trained COMSOL DNN surrogate and prove consistency.

Repository-only and licensed.  ``--dry-run`` validates the isolation contract
without importing MPh, starting COMSOL, or acquiring a solver lease.

The gate proves on an isolated licensed session:
1. a trained surrogate can be exported to COMSOL's internal format and the
   produced file is hashed by content (not by the unreadable filename property);
2. ONNX export availability is determined by attempting it, and an unavailable
   format is recorded as unavailable rather than passed;
3. a re-imported artifact's predictions agree with the in-model predictions
   within a declared tolerance;
4. an inconsistent artifact is refused registration;
5. the owned session and solver lease are released with no descendant left.
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

SCHEMA_NAME = "comsol_mcp.surrogate_export_licensed_gate"
SCHEMA_VERSION = "1.1.0"
EXPECTED_BACKEND = {"major": 6, "minor": 4, "patch": 0, "build": 293}

CONSISTENCY_ABSOLUTE_TOLERANCE = 1e-6


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", choices=["RUN_REAL_COMSOL"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--runtime-root", type=Path, default=Path("D:/comsol_runtime"))
    parser.add_argument("--cores", type=int, required=True)
    parser.add_argument("--mph-lane", choices=["1.3.1", "1.4.0"], default="1.3.1")
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
        except psutil.AccessDenied, psutil.ZombieProcess:
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
        "lease": lease.get("lease"),
        "durable_jobs_available": durable.get("available"),
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
        "passed": process.poll() is not None and verification.get("absent") is True and not errors,
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


def _write_dataset(path: Path) -> list[dict[str, float]]:
    """Write the headerless CRLF dataset the S6 gate proved COMSOL accepts."""
    rows: list[dict[str, float]] = []
    for index in range(48):
        a1 = 1.0 + (index % 8) * 0.25
        a2 = 2.0 + (index // 8) * 0.5
        qoi = math.sin(a1) * math.cos(a2) + 0.1 * a1 * a2
        rows.append({"a1": a1, "a2": a2, "qoi": qoi})
    lines = [f"{row['a1']:.9f}, {row['a2']:.9f}, {row['qoi']:.12f}" for row in rows]
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")
    return rows


def _run_worker(output: Path, cores: int, workspace: Path, mph_lane: str = "1.3.1") -> int:
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

        from comsol_mcp.adapter import make_backend

        if str(mph.__version__) != mph_lane:
            raise RuntimeError(f"expected MPh {mph_lane}, loaded {mph.__version__}")
        result["mph_identity"] = {
            "version": str(mph.__version__),
            "module": str(mph.__file__),
            "module_sha256": _sha256_file(Path(mph.__file__)),
            "adapter_lane": mph_lane,
        }

        from surrogate_gate_oracles import (
            bind_split_tables,
            comsol_predictions,
            frozen_split,
            mutated_onnx_predictions,
            onnx_predictions,
            write_rows,
        )

        from comsol_mcp.surrogate.dnn_adapter import (
            DNN_FUNCTION_TAG,
            apply_surrogate_configuration,
            bind_data_source_and_arguments,
            build_dnn_configuration,
            train_surrogate,
        )
        from comsol_mcp.surrogate.dnn_clientapi_backend import ClientapiSurrogateDnnBackend
        from comsol_mcp.surrogate.export import (
            build_export_manifest,
            check_prediction_consistency,
            hash_export_artifact,
            integrate_export_into_registry,
        )

        result["backend"] = _select_expected_backend(mph.discovery.find_backends())
        client = mph.Client(cores=cores, version="6.4")
        result["client"] = {
            "version": str(client.version),
            "standalone": bool(client.standalone),
            "port": client.port,
            "jvm_started": bool(jpype.isJVMStarted()),
        }

        dataset_path = workspace / "surrogate_export_gate.csv"
        rows = _write_dataset(dataset_path)
        split = frozen_split(rows)
        write_rows(dataset_path, split["rows"]["train"])
        result["frozen_split"] = split
        result["dataset"] = {
            "row_count": len(split["rows"]["train"]),
            "sha256": _sha256_file(dataset_path),
        }

        model = client.create("SurrogateExportGate")
        jm = model.java
        jm.param().set("a1", "1.0")
        jm.param().set("a2", "2.0")
        jm.component().create("comp1", True)

        backend = ClientapiSurrogateDnnBackend(model, client=client, adapter=make_backend(mph_lane))
        configuration = build_dnn_configuration(
            configuration_id="s7-export-gate",
            input_features=["a1", "a2"],
            output_features=["qoi"],
            hidden_layers=[16, 8],
            activation="tanh",
            optimizer="adam",
            loss="mse",
            learning_rate=1e-2,
            batch_size=8,
            maximum_epochs=60,
            seed=17,
            validation_mode="table",
            test_mode="table",
        )
        applied = apply_surrogate_configuration(backend, configuration)
        if not applied["success"]:
            raise RuntimeError(f"apply failed: {applied.get('error')}")
        bound = bind_data_source_and_arguments(
            backend, configuration, dataset_path=str(dataset_path)
        )
        if not bound["success"]:
            raise RuntimeError(f"data binding failed: {bound.get('error')}")
        dnn = backend.get_dnn_function(DNN_FUNCTION_TAG)
        result["table_bindings"] = bind_split_tables(model, backend, dnn, split)
        trained = train_surrogate(backend, run_test=True)
        if not trained["success"]:
            raise RuntimeError(f"training failed: {trained.get('error')}")
        trained_chksum = str((trained.get("trained_chksum") or {}).get("value"))
        result["training"] = {
            "trained_chksum": trained_chksum,
            "testloss": trained.get("testloss_value"),
            "layerconfig": (trained.get("layerconfig") or {}).get("value"),
        }

        dnn = backend.get_dnn_function(DNN_FUNCTION_TAG)

        # ---- Phase 1: export the trained network twice --------------------
        # COMSOL's `export(path)` writes ONNX protobuf regardless of extension.
        # Exporting twice proves the serialization is deterministic, which is
        # what makes a content hash a stable identity for the trained artifact.
        internal_paths = [
            workspace / "surrogate_export_a.onnx",
            workspace / "surrogate_export_b.onnx",
        ]
        internal_errors: list[str | None] = []
        for target in internal_paths:
            try:
                backend.export_onnx(dnn, str(target))
                internal_errors.append(None)
            except Exception as exc:
                internal_errors.append(f"{type(exc).__name__}: {exc}")
        result["export_attempts"] = {
            "errors": internal_errors,
            "exists": [path.is_file() for path in internal_paths],
            "bytes": [path.stat().st_size if path.is_file() else 0 for path in internal_paths],
        }

        # A differently-named export proves the extension is not the format.
        extension_probe = workspace / "surrogate_export_extension_probe.txt"
        extension_error = None
        try:
            backend.export_onnx(dnn, str(extension_probe))
        except Exception as exc:
            extension_error = f"{type(exc).__name__}: {exc}"
        result["extension_probe"] = {
            "path_suffix": ".txt",
            "error": extension_error,
            "exists": extension_probe.is_file(),
            "bytes": extension_probe.stat().st_size if extension_probe.is_file() else 0,
            "identical_to_onnx": (
                extension_probe.is_file()
                and internal_paths[0].is_file()
                and _sha256_file(extension_probe) == _sha256_file(internal_paths[0])
            ),
        }

        # ---- Phase 2: bind the exported artifact --------------------------
        artifacts: list[dict[str, Any]] = []
        if internal_paths[0].is_file() and internal_paths[0].stat().st_size > 0:
            artifacts.append(hash_export_artifact(path=internal_paths[0], export_format="onnx"))
        result["artifacts"] = artifacts

        # ---- Phase 3: prove the exported identity is stable and faithful ---
        # A second export must hash identically; otherwise the artifact cannot
        # serve as a stable identity and registration must be refused.
        export_deterministic = False
        if internal_paths[0].is_file() and internal_paths[1].is_file():
            first_hash = _sha256_file(internal_paths[0])
            second_hash = _sha256_file(internal_paths[1])
            export_deterministic = first_hash == second_hash
            result["export_determinism"] = {
                "first_sha256": first_hash,
                "second_sha256": second_hash,
                "deterministic": export_deterministic,
            }
        else:
            result["export_determinism"] = {
                "deterministic": False,
                "reason": "an export did not produce a file",
            }
        result["hash_stable"] = export_deterministic

        exported_predictions, payload_evidence = onnx_predictions(
            internal_paths[0], split["rows"]["test"]
        )
        result["export_payload"] = payload_evidence

        if artifacts:
            manifest = build_export_manifest(
                export_id="s7-export-gate",
                trained_chksum=trained_chksum,
                architecture_sha256=configuration["configuration_sha256"],
                artifacts=artifacts,
            )
            result["manifest"] = manifest
        else:
            result["manifest"] = None
            result["manifest_error"] = "no export artifact was produced"

        # ---- Phase 4: independent COMSOL-versus-ONNX predictions ----------
        if artifacts and payload_evidence.get("carries_trained_state"):
            names = [str(n) for n in model.java.func(DNN_FUNCTION_TAG).functionNames()]
            if len(names) != 1:
                raise RuntimeError("scalar fixture requires exactly one COMSOL function")
            reference = comsol_predictions(model, split["rows"]["test"], names[0])
            result["consistency_reference"] = {
                "source": "COMSOL_function_evaluation",
                "count": len(reference),
                "row_ids": split["row_ids"]["test"],
                "input_rows": split["rows"]["test"],
                "in_model_predictions": reference,
                "exported_predictions": exported_predictions,
                "split_sha256": split["split_sha256"],
            }
            agreeing = check_prediction_consistency(
                in_model_predictions=reference,
                reimported_predictions=exported_predictions,
                absolute_tolerance=CONSISTENCY_ABSOLUTE_TOLERANCE,
            )
            perturbed = [list(row) for row in reference]
            perturbed[0][0] = perturbed[0][0] + 1.0
            disagreeing = check_prediction_consistency(
                in_model_predictions=reference,
                reimported_predictions=perturbed,
                absolute_tolerance=CONSISTENCY_ABSOLUTE_TOLERANCE,
            )
            result["consistency"] = {
                "reference_source": "independent_COMSOL_and_ONNX_evaluators",
                "reference_count": len(reference),
                "agreeing": agreeing,
                "disagreeing": disagreeing,
            }
            mutations = mutated_onnx_predictions(internal_paths[0], split["rows"]["test"])
            result["mutation_consistency"] = {
                name: check_prediction_consistency(
                    in_model_predictions=reference,
                    reimported_predictions=predictions,
                    absolute_tolerance=CONSISTENCY_ABSOLUTE_TOLERANCE,
                )
                for name, predictions in mutations.items()
            }
            result["registry"] = {
                "consistent": integrate_export_into_registry(
                    export_manifest=result["manifest"],
                    consistency=agreeing,
                    ood_state="in_domain",
                    escalation_required=False,
                ),
                "inconsistent": integrate_export_into_registry(
                    export_manifest=result["manifest"],
                    consistency=disagreeing,
                    ood_state="in_domain",
                    escalation_required=False,
                ),
                "out_of_domain": integrate_export_into_registry(
                    export_manifest=result["manifest"],
                    consistency=agreeing,
                    ood_state="out_of_domain",
                    escalation_required=False,
                ),
            }
        else:
            result["consistency"] = None
            result["registry"] = None

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
    owner = SolverOwnership(args.runtime_root, owner="surrogate-export-licensed-gate")

    receipt: dict[str, Any] = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": False,
        "source": {
            "git": git,
            "script": str(Path(__file__).resolve().relative_to(ROOT)),
            "script_sha256": _sha256_file(Path(__file__).resolve()),
            "oracle_sha256": _sha256_file(Path(__file__).with_name("surrogate_gate_oracles.py")),
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
            raise RuntimeError("gate requires no collision, lease, or active durable job")
        preflight = owner.preflight(
            output_path=str(output),
            requested_version="6.4",
            minimum_free_gb=args.minimum_free_gb,
        )
        receipt["preflight"] = preflight
        if not preflight.get("ready"):
            raise RuntimeError(f"solver preflight failed: {preflight.get('blockers')}")
        claim = owner.acquire(mode="surrogate-export-licensed-gate")
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
            "--mph-lane",
            args.mph_lane,
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
            receipt["worker_result_sha256"] = _sha256_file(worker_output)
            receipt["worker_result"] = json.loads(worker_output.read_text(encoding="utf-8"))
        else:
            receipt["worker_result"] = None
        active = owner.status(require_fresh_inventory=True)
        receipt["ownership_during"] = _status_evidence(active)
        worker_result = receipt.get("worker_result") or {}
        manifest = worker_result.get("manifest") or {}
        registry = worker_result.get("registry") or {}
        consistency = worker_result.get("consistency") or {}
        agreeing = consistency.get("agreeing") or {}
        disagreeing = consistency.get("disagreeing") or {}
        payload = worker_result.get("export_payload") or {}
        phase_passed = (
            process.returncode == 0
            and not timed_out
            and worker_result.get("success") is True
            # The exported ONNX must be byte-stable across two exports and must
            # not depend on the filename extension.
            and worker_result.get("hash_stable") is True
            and (worker_result.get("extension_probe") or {}).get("identical_to_onnx") is True
            # It must carry real trained weights, not a degenerate record.
            and payload.get("carries_trained_state") is True
            and payload.get("is_onnx_protobuf") is True
            # The manifest must bind the trained identity and a content hash.
            and manifest.get("trained_chksum")
            == (worker_result.get("training") or {}).get("trained_chksum")
            and bool(manifest.get("artifacts"))
            and all(len(item["sha256"]) == 64 for item in manifest.get("artifacts", []))
            # ONNX availability must be recorded from the real export attempt.
            and (worker_result.get("artifacts") or [{}])[0].get("export_format") == "onnx"
            # Consistency must pass for agreement and fail for disagreement.
            and agreeing.get("state") == "consistent"
            and consistency.get("reference_source") == "independent_COMSOL_and_ONNX_evaluators"
            and len(worker_result.get("mutation_consistency", {})) == 3
            and all(
                item.get("state") == "inconsistent"
                for item in worker_result.get("mutation_consistency", {}).values()
            )
            and disagreeing.get("state") == "inconsistent"
            and (registry.get("consistent") or {}).get("registered") is True
            and (registry.get("inconsistent") or {}).get("registered") is False
            and (registry.get("inconsistent") or {}).get("escalation_required") is True
            # Out-of-domain must always demand fresh FEM escalation.
            and (registry.get("out_of_domain") or {}).get("escalation_required") is True
            and active.get("durable_jobs", {}).get("active_count") == 0
        )
        if not phase_passed:
            raise RuntimeError("licensed surrogate export gate did not satisfy its contract")
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
            "collision_absent": isinstance(after, dict) and after.get("collision") is False,
            "owned_descendants_absent": remaining["complete"] is True
            and not remaining["identities"],
            "active_job_count": (
                after.get("durable_jobs", {}).get("active_count")
                if isinstance(after, dict)
                else None
            ),
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
        },
        "proves": [
            "internal-format export hashed by content, not by the unreadable filename property",
            "ONNX availability determined by attempting the export",
            "re-imported prediction agreement within a declared tolerance",
            "inconsistent artifact refused registration",
            "out-of-domain always requires fresh FEM escalation",
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
        if args.confirm != "RUN_REAL_COMSOL" or args.worker_output is None:
            raise SystemExit("worker mode requires --confirm RUN_REAL_COMSOL and --worker-output")
        resolved = args.worker_output.resolve()
        return _run_worker(resolved, args.cores, resolved.parent / "workspace", args.mph_lane)
    if args.confirm != "RUN_REAL_COMSOL" or args.output is None:
        raise SystemExit("licensed gate requires --confirm RUN_REAL_COMSOL and --output")
    try:
        return _run_parent(args)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
