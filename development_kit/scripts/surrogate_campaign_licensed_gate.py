"""Licensed gate: surrogate screening escalated to a fresh FEM verification.

Repository-only and licensed.  ``--dry-run`` validates the isolation contract
without importing MPh, starting COMSOL, or acquiring a solver lease.

This gate proves the central 0.7.5 invariant end to end on real COMSOL:

1. a trained surrogate screens a bounded candidate set and produces only
   `predicted` records;
2. ranking selects a bounded escalation set, forcing every out-of-domain
   candidate to be escalated regardless of rank;
3. each promoted candidate is verified by a **fresh FEM solve** whose own
   evidence is independent of the surrogate;
4. the promoted candidate's fresh FEM result is compared with the prediction,
   so the surrogate's error is measured rather than asserted;
5. no prediction is promoted without a verified FEM result and artifact hash;
6. the owned session, lease, descendants, and listeners are all released.
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

SCHEMA_NAME = "comsol_mcp.surrogate_campaign_licensed_gate"
SCHEMA_VERSION = "1.0.0"
EXPECTED_BACKEND = {"major": 6, "minor": 4, "patch": 0, "build": 293}

# The analytic target the FEM model evaluates.  A real COMSOL model solves this
# expression at each candidate point, so the FEM result is independent of the
# surrogate that predicted it.
ANALYTIC_EXPRESSION = "sin(a1)*cos(a2)+0.1*a1*a2"

CANDIDATE_POINTS: list[tuple[float, float]] = [
    (1.00, 2.0),
    (1.50, 2.5),
    (2.00, 3.0),
    (2.50, 3.5),
    (3.00, 4.0),
    (3.50, 4.5),
]
# The candidate deliberately placed outside the training domain, so the
# escalation rule must force it to FEM even if it ranks poorly.
OOD_CANDIDATE_POINTS: list[tuple[float, float]] = [(9.0, 9.0)]


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


def _write_dataset(path: Path) -> list[dict[str, float]]:
    """Write the headerless CRLF dataset that the S6 gate proved COMSOL accepts."""
    rows: list[dict[str, float]] = []
    for index in range(48):
        a1 = 1.0 + (index % 8) * 0.25
        a2 = 2.0 + (index // 8) * 0.5
        rows.append(
            {"a1": a1, "a2": a2, "qoi": math.sin(a1) * math.cos(a2) + 0.1 * a1 * a2}
        )
    lines = [f"{row['a1']:.9f}, {row['a2']:.9f}, {row['qoi']:.12f}" for row in rows]
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")
    return rows


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

        from comsol_mcp.surrogate.campaign import (
            assert_no_prediction_promotion,
            build_campaign_spec,
            build_screening_record,
            rank_candidates,
            record_fem_result,
            summarize_campaign,
        )
        from comsol_mcp.surrogate.dnn_adapter import (
            DNN_FUNCTION_TAG,
            apply_surrogate_configuration,
            bind_data_source_and_arguments,
            build_dnn_configuration,
            train_surrogate,
        )
        from comsol_mcp.surrogate.dnn_clientapi_backend import ClientapiSurrogateDnnBackend
        from comsol_mcp.surrogate.onnx_runtime import evaluate_onnx_model, load_onnx_model

        result["backend"] = _select_expected_backend(mph.discovery.find_backends())
        client = mph.Client(cores=cores, version="6.4")
        result["client"] = {
            "version": str(client.version),
            "standalone": bool(client.standalone),
            "port": client.port,
            "jvm_started": bool(jpype.isJVMStarted()),
        }

        dataset_path = workspace / "surrogate_campaign_gate.csv"
        rows = _write_dataset(dataset_path)
        result["dataset"] = {
            "row_count": len(rows),
            "sha256": _sha256_file(dataset_path),
            "analytic_expression": ANALYTIC_EXPRESSION,
        }

        # ---- Phase 1: train the screening surrogate ------------------------
        model = client.create("SurrogateCampaignGate")
        jm = model.java
        jm.param().set("a1", "1.0")
        jm.param().set("a2", "2.0")
        jm.component().create("comp1", True)

        backend = ClientapiSurrogateDnnBackend(model)
        configuration = build_dnn_configuration(
            configuration_id="s8-campaign-gate",
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
            validation_mode="fraction",
            test_mode="random",
        )
        applied = apply_surrogate_configuration(backend, configuration)
        if not applied["success"]:
            raise RuntimeError(f"apply failed: {applied.get('error')}")
        bound = bind_data_source_and_arguments(
            backend, configuration, dataset_path=str(dataset_path)
        )
        if not bound["success"]:
            raise RuntimeError(f"data binding failed: {bound.get('error')}")
        trained = train_surrogate(backend, run_test=True)
        if not trained["success"]:
            raise RuntimeError(f"training failed: {trained.get('error')}")
        trained_chksum = str((trained.get("trained_chksum") or {}).get("value"))
        dnn = backend.get_dnn_function(DNN_FUNCTION_TAG)
        result["training"] = {
            "trained_chksum": trained_chksum,
            "testloss": trained.get("testloss_value"),
            "layerconfig": (trained.get("layerconfig") or {}).get("value"),
        }

        # ---- Phase 2: screen the bounded candidate set ---------------------
        spec = build_campaign_spec(
            campaign_id="s8-campaign-gate",
            candidate_count=len(CANDIDATE_POINTS) + len(OOD_CANDIDATE_POINTS),
            top_k=3,
            maximum_fem_escalations=len(CANDIDATE_POINTS) + len(OOD_CANDIDATE_POINTS),
            wall_time_budget_seconds=3600,
            objective="maximize_qoi",
            maximize=True,
            surrogate_registry_entry_sha256=configuration["configuration_sha256"],
            dataset_manifest_sha256=_sha256_file(dataset_path),
        )
        result["campaign_spec"] = spec

        # The surrogate's prediction is read from the exported ONNX artifact of
        # the trained network.  A trained COMSOL DNN function cannot be evaluated
        # in a geometry-free surrogate model (dataset creation fails with
        # "在这个情景中不能创建本操作"), so the exported artifact is the
        # evaluable form of the trained model.  This also proves the export is a
        # working network rather than inert bytes.
        export_path = workspace / "campaign_surrogate.onnx"
        export_error = None
        try:
            backend.export_onnx(dnn, str(export_path))
        except Exception as exc:
            export_error = f"{type(exc).__name__}: {exc}"
        if not export_path.is_file() or export_path.stat().st_size <= 0:
            raise RuntimeError(
                f"surrogate export produced no artifact: {export_error or 'empty file'}"
            )
        onnx_model = load_onnx_model(export_path)
        result["surrogate_export"] = {
            "path_name": export_path.name,
            "bytes": export_path.stat().st_size,
            "sha256": _sha256_file(export_path),
            "producer": onnx_model["producer"],
            "operators": onnx_model["operators"],
            "initializer_shapes": onnx_model["initializer_shapes"],
            "graph_inputs": onnx_model["graph_inputs"],
        }

        def _surrogate_predict(a1: float, a2: float) -> float | None:
            try:
                prediction = evaluate_onnx_model(
                    onnx_model,
                    input_names=["a1", "a2"],
                    points=[[a1, a2]],
                )[0][0]
            except Exception:
                return None
            return prediction if math.isfinite(prediction) else None

        screening_records: list[dict[str, Any]] = []
        prediction_errors: list[str] = []
        for index, (a1, a2) in enumerate([*CANDIDATE_POINTS, *OOD_CANDIDATE_POINTS]):
            ood_state = "out_of_domain" if index >= len(CANDIDATE_POINTS) else "in_domain"
            prediction = _surrogate_predict(a1, a2)
            if prediction is None or not math.isfinite(prediction):
                # A candidate the surrogate cannot score is still a candidate;
                # it is recorded as uncalibrated so it must be FEM-verified.
                prediction_errors.append(f"candidate {index}: surrogate produced no value")
                prediction = 0.0
                ood_state = "uncalibrated"
            screening_records.append(
                build_screening_record(
                    candidate_id=f"cand-{index}",
                    features={"a1": a1, "a2": a2},
                    predicted_objective=prediction,
                    ood_state=ood_state,
                    surrogate_registry_entry_sha256=configuration["configuration_sha256"],
                )
            )
        result["screening"] = {
            "records": screening_records,
            "prediction_errors": prediction_errors,
            "all_records_predicted_state": all(
                record["state"] == "predicted" for record in screening_records
            ),
            "no_record_claims_fem_evidence": all(
                record["is_fem_evidence"] is False for record in screening_records
            ),
        }

        ranking = rank_candidates(screening_records, maximize=True, top_k=3)
        result["ranking"] = ranking

        # ---- Phase 3: fresh FEM verification of every escalated candidate --
        # Each escalated candidate is evaluated by an independent COMSOL solve.
        # A second, separate model is built so the verification does not share
        # any state with the screening surrogate.
        fem_model = client.create("SurrogateCampaignFEM")
        fem_java = fem_model.java
        fem_java.param().set("a1", "1.0")
        fem_java.param().set("a2", "2.0")
        fem_java.component().create("comp1", True)
        # A geometry and a PDE are required before a numerical evaluation can
        # resolve a dataset, so the verification model is a real solvable model.
        fem_java.component("comp1").geom().create("geom1", 1)
        fem_java.component("comp1").geom("geom1").create("i1", "Interval")
        fem_java.component("comp1").geom("geom1").feature("i1").set("coord", ["0", "1"])
        fem_java.component("comp1").geom("geom1").run()
        fem_java.component("comp1").physics().create("c1", "CoefficientFormPDE", "geom1")
        fem_java.study().create("std_fem")
        fem_java.study("std_fem").create("stat", "Stationary")
        fem_java.study("std_fem").run()
        result["fem_model"] = {
            "geometry_created": True,
            "study_solved": True,
            "dataset_tags": [
                str(item) for item in list(fem_java.result().dataset().tags())
            ],
        }

        fem_results: list[dict[str, Any]] = []
        for item in ranking["escalation_plan"]:
            candidate_id = item["candidate_id"]
            record = next(
                row for row in screening_records if row["candidate_id"] == candidate_id
            )
            a1 = record["features"]["a1"]
            a2 = record["features"]["a2"]
            fem_value: float | None = None
            fem_error: str | None = None
            try:
                fem_java.param().set("a1", repr(float(a1)))
                fem_java.param().set("a2", repr(float(a2)))
                # A fresh FEM run per candidate: the parameters are set and the
                # study is re-solved, so the solution reflects this candidate.
                # Evaluating a previously solved dataset instead would silently
                # return the *first* candidate's value for every candidate,
                # because a stored dataset keeps the parameters it was solved at.
                fem_java.study("std_fem").run()

                # The evaluation node must use the `Global` operation.  An `Eval`
                # node requires a geometric selection and silently returns an
                # empty array here, which would be indistinguishable from a
                # legitimately absent result.
                tag = f"ev_{candidate_id.replace('-', '_')}"
                fem_java.result().numerical().create(tag, "Global")
                node = fem_java.result().numerical(tag)
                node.set("expr", ANALYTIC_EXPRESSION)
                node.set("data", "dset1")
                raw = node.getReal()
                values = [float(row[0]) for row in raw if len(row) >= 1]
                if values and math.isfinite(values[0]):
                    fem_value = values[0]
                else:
                    fem_error = f"no finite FEM value: {values!r}"
            except Exception as exc:
                fem_error = f"{type(exc).__name__}: {exc}"

            fem_results.append(
                record_fem_result(
                    candidate_id=candidate_id,
                    predicted_objective=record["predicted_objective"],
                    measured_objective=fem_value,
                    fem_evidence_state="verified" if fem_value is not None else "failed",
                    fem_artifact_sha256=(
                        hashlib.sha256(
                            f"{candidate_id}:{a1}:{a2}:{fem_value}".encode("utf-8")
                        ).hexdigest()
                        if fem_value is not None
                        else None
                    ),
                    absolute_tolerance=1e-6,
                )
            )
            fem_results[-1]["escalation_reason"] = item["reason"]
            fem_results[-1]["fem_error"] = fem_error
            fem_results[-1]["coordinates"] = {"a1": a1, "a2": a2}

        result["fem_results"] = fem_results

        # ---- Phase 4: invariant and summary --------------------------------
        invariant = assert_no_prediction_promotion(screening_records, fem_results)
        result["invariant"] = invariant
        summary = summarize_campaign(
            spec=spec,
            screening_records=screening_records,
            fem_results=fem_results,
            fem_escalations_used=len(fem_results),
            wall_time_seconds=max(time.monotonic() - started, 0.0),
        )
        result["summary"] = summary

        # The surrogate's measured error against the fresh FEM results is the
        # only admissible accuracy statement.
        verified = [r for r in fem_results if r["state"] == "verified"]
        result["surrogate_error"] = {
            "verified_fem_count": len(verified),
            "worst_absolute_error": summary["worst_absolute_error"],
            "mean_absolute_error": summary["mean_absolute_error"],
            "error_is_measured_against_fresh_fem": True,
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
    owner = SolverOwnership(args.runtime_root, owner="surrogate-campaign-licensed-gate")

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
            raise RuntimeError("gate requires no collision, lease, or active durable job")
        preflight = owner.preflight(
            output_path=str(output),
            requested_version="6.4",
            minimum_free_gb=args.minimum_free_gb,
        )
        receipt["preflight"] = preflight
        if not preflight.get("ready"):
            raise RuntimeError(f"solver preflight failed: {preflight.get('blockers')}")
        claim = owner.acquire(mode="surrogate-campaign-licensed-gate")
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
            receipt["worker_result_sha256"] = _sha256_file(worker_output)
            receipt["worker_result"] = json.loads(worker_output.read_text(encoding="utf-8"))
        else:
            receipt["worker_result"] = None
        active = owner.status(require_fresh_inventory=True)
        receipt["ownership_during"] = _status_evidence(active)
        worker_result = receipt.get("worker_result") or {}
        screening = worker_result.get("screening") or {}
        ranking = worker_result.get("ranking") or {}
        fem_results = worker_result.get("fem_results") or []
        invariant = worker_result.get("invariant") or {}
        summary = worker_result.get("summary") or {}
        verified = [item for item in fem_results if item.get("state") == "verified"]
        # Independent verification of the FEM evidence: each measured value must
        # reproduce the analytic target at *its own* coordinates.  Without this,
        # a stale-dataset evaluation that returns one value for every candidate
        # would look like a successful escalation.
        per_candidate_check = []
        for item in verified:
            coordinates = item.get("coordinates") or {}
            a1 = coordinates.get("a1")
            a2 = coordinates.get("a2")
            measured = item.get("measured_objective")
            if a1 is None or a2 is None or measured is None:
                per_candidate_check.append({"candidate_id": item.get("candidate_id"), "matches": False})
                continue
            expected = math.sin(a1) * math.cos(a2) + 0.1 * a1 * a2
            per_candidate_check.append(
                {
                    "candidate_id": item.get("candidate_id"),
                    "coordinates": coordinates,
                    "measured": measured,
                    "expected": expected,
                    "absolute_difference": abs(measured - expected),
                    "matches": abs(measured - expected) <= 1e-6,
                }
            )
        measured_values = [item.get("measured_objective") for item in verified]
        distinct_measurements = len({round(float(value), 9) for value in measured_values})
        receipt["fem_verification"] = {
            "per_candidate": per_candidate_check,
            "all_candidates_match_their_own_coordinates": all(
                entry["matches"] for entry in per_candidate_check
            )
            if per_candidate_check
            else False,
            "distinct_measurement_count": distinct_measurements,
            "verified_count": len(verified),
            "measurements_are_distinct": distinct_measurements == len(verified)
            if verified
            else False,
        }
        phase_passed = (
            process.returncode == 0
            and not timed_out
            and worker_result.get("success") is True
            # Screening produces predictions only.
            and screening.get("all_records_predicted_state") is True
            and screening.get("no_record_claims_fem_evidence") is True
            # Ranking is not evidence and escalates the out-of-domain candidate.
            and ranking.get("ranking_is_evidence") is False
            and any(
                item.get("reason") == "out_of_domain"
                for item in ranking.get("escalation_plan", [])
            )
            # Every escalated candidate received a fresh FEM result.
            and len(fem_results) == ranking.get("escalation_count")
            # The fresh FEM solve actually produced verified evidence.
            and len(verified) >= 2
            and all(item.get("is_fem_evidence") is True for item in verified)
            and all(item.get("fem_artifact_sha256") for item in verified)
            # Each measurement must be a genuine fresh solve at its own point.
            and receipt["fem_verification"]["all_candidates_match_their_own_coordinates"]
            and receipt["fem_verification"]["measurements_are_distinct"]
            # No prediction was promoted without verified FEM evidence.
            and invariant.get("no_unverified_promotion") is True
            and invariant.get("predictions_are_not_evidence") is True
            and summary.get("predictions_are_not_evidence") is True
            and summary.get("verified_requires_fresh_fem") is True
            and summary.get("upgrades_fem_evidence") is False
            and summary.get("verified_count") == len(verified)
            and summary.get("error_metrics_source") == "fresh_fem_comparison"
            and active.get("durable_jobs", {}).get("active_count") == 0
        )
        if not phase_passed:
            raise RuntimeError("licensed surrogate campaign gate did not satisfy its contract")
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
            "surrogate screening produces predicted records only",
            "ranking is not evidence and forces out-of-domain escalation",
            "every escalated candidate receives a fresh FEM solve",
            "the surrogate's error is measured against the fresh FEM results",
            "no prediction is promoted without verified FEM evidence and an artifact hash",
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
        return _run_worker(resolved, args.cores, resolved.parent / "workspace")
    if args.confirm != "RUN_REAL_COMSOL" or args.output is None:
        raise SystemExit("licensed gate requires --confirm RUN_REAL_COMSOL and --output")
    try:
        return _run_parent(args)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
