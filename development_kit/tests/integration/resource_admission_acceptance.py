"""Controlled COMSOL 6.4 gate for detached resource admission."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.jobs.manager import JobManager
from src.jobs.process_control import OwnedJobObject
from src.jobs.resource_admission import (
    collect_resource_telemetry,
    evaluate_resource_admission,
)
from src.jobs.store import TERMINAL_STATES
from src.tools.ownership import SolverOwnership


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _terminate_builder_tree(
    process: subprocess.Popen,
    containment: OwnedJobObject | None,
) -> dict[str, object]:
    errors = []
    if containment is not None:
        try:
            containment.close()
        except Exception as exc:
            errors.append({"stage": "job_object_close", "type": type(exc).__name__})
    if process.poll() is None:
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        try:
            subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append({"stage": "taskkill", "type": type(exc).__name__})
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append({"stage": "direct_kill", "type": type(exc).__name__})
    except (OSError, subprocess.SubprocessError) as exc:
        errors.append({"stage": "wait", "type": type(exc).__name__})
    root_absent = process.poll() is not None
    return {
        "passed": root_absent and containment is not None and not errors,
        "root_absent": root_absent,
        "job_object_contained": containment is not None,
        "errors": errors,
    }


def _run_builder_process(command: list[str]) -> dict[str, object]:
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creation_flags,
    )
    containment = OwnedJobObject.assign(process.pid)
    timed_out = False
    communication_errors = []
    stdout = ""
    stderr = ""
    try:
        stdout, stderr = process.communicate(timeout=120)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = _timeout_text(exc.stdout)
        stderr = _timeout_text(exc.stderr)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        communication_errors.append(type(exc).__name__)
    finally:
        cleanup = _terminate_builder_tree(process, containment)
    if timed_out or communication_errors:
        try:
            tail_stdout, tail_stderr = process.communicate(timeout=30)
            stdout += tail_stdout or ""
            stderr += tail_stderr or ""
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            communication_errors.append(type(exc).__name__)
    return {
        "success": not timed_out
        and process.returncode == 0
        and cleanup["passed"] is True
        and not communication_errors,
        "returncode": process.returncode,
        "timed_out": timed_out,
        "communication_errors": communication_errors,
        "stdout_tail": stdout[-2000:],
        "stderr_tail": stderr[-2000:],
        "cleanup": cleanup,
    }


def _resource_journal_acceptance(journal: list[dict]) -> dict[str, object]:
    telemetry = [entry for entry in journal if entry.get("entry_type") == "telemetry"]
    admissions = [entry for entry in journal if entry.get("entry_type") == "admission"]
    expected_stages = ["pre_solve", "post_solve"]
    checks = {
        "telemetry_stages_complete": [entry.get("stage") for entry in telemetry]
        == expected_stages,
        "admission_stages_complete": [entry.get("stage") for entry in admissions]
        == expected_stages,
        "admissions_allow": bool(admissions)
        and all(entry.get("decision") == "allow" for entry in admissions),
    }
    return {"passed": all(checks.values()), "checks": checks}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resource_policy() -> dict[str, object]:
    return {
        "available_memory_refuse_fraction": 0.05,
        "remaining_commit_refuse_fraction": 0.05,
        "runtime_free_space_refuse_bytes": 1_000_000_000,
        "max_mesh_elements": 100_000,
        "wall_time_budget_seconds": 180.0,
        "minimum_next_point_seconds": 10.0,
    }


def _build_source(runtime: Path, source_path: Path, mesh_receipt: Path) -> None:
    import jpype
    import mph

    owner = SolverOwnership(runtime, owner="resource-admission-builder")
    client = None
    model = None
    result = {"success": False, "solve_ran": False}
    exit_code = 1
    started = time.monotonic()
    try:
        claim = owner.acquire(mode="resource_mesh_builder", model_path=str(source_path))
        if not claim.get("acquired"):
            raise RuntimeError(f"solver lease unavailable: {claim}")
        client = mph.Client(cores=1, version="6.4")
        model = client.create("ResourceAdmissionSource")
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

        electrostatics = component.physics().create(
            "es", "Electrostatics", str(geometry.getSDim())
        )
        conservation = electrostatics.feature().create(
            "ccn1", "ChargeConservation", int(geometry.getSDim())
        )
        conservation.selection().set([1])
        conservation.set("materialType", "from_mat")
        material = component.material().create("mat1", "Common")
        material.propertyGroup("def").set("relpermittivity", "epsr")
        material.selection().set([1])
        ground = electrostatics.feature().create("gnd1", "Ground", 2)
        ground.selection().set([3])
        potential = electrostatics.feature().create("ep1", "ElectricPotential", 2)
        potential.selection().set([4])
        potential.set("V0", "V0")

        mesh = component.mesh().create("mesh1")
        mesh.feature().create("ftr1", "FreeTet")
        pre_mesh = collect_resource_telemetry(
            stage="pre_mesh",
            runtime_path=runtime,
            process_id=os.getpid(),
            mesh_elements=0,
            elapsed_wall_seconds=time.monotonic() - started,
        )
        mesh.run()
        element_count = int(mesh.getNumElem())
        post_mesh = collect_resource_telemetry(
            stage="post_mesh",
            runtime_path=runtime,
            process_id=os.getpid(),
            mesh_elements=element_count,
            elapsed_wall_seconds=time.monotonic() - started,
        )
        post_mesh_admission = evaluate_resource_admission(
            _resource_policy(),
            post_mesh,
        )
        if post_mesh_admission["decision"] != "allow":
            raise AssertionError(f"post-mesh admission failed: {post_mesh_admission}")

        study = jm.study().create("std1")
        study.create("step1", "Stationary")
        jm.save(str(source_path))
        result.update(
            success=True,
            source_sha256=_sha256(source_path),
            mesh_elements=element_count,
            pre_mesh=pre_mesh,
            post_mesh=post_mesh,
            post_mesh_admission=post_mesh_admission,
        )
        exit_code = 0
    except Exception as exc:
        result["error"] = str(exc)
        result["traceback"] = traceback.format_exc(limit=10)
    finally:
        if client is not None:
            if model is not None:
                try:
                    client.remove(model)
                except Exception:
                    pass
            try:
                client.clear()
            except Exception:
                pass
        result["lease_release"] = owner.release()
        mesh_receipt.write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(result, ensure_ascii=False), flush=True)
        os._exit(exit_code)


def _poll_terminal(manager: JobManager, job_id: str, timeout_seconds: float) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = manager.status(job_id)
        if state["status"] in TERMINAL_STATES:
            return state
        time.sleep(0.25)
    raise TimeoutError(f"resource admission detached job did not become terminal: {job_id}")


def _poll_cleanup(runtime: Path, timeout_seconds: float) -> dict:
    deadline = time.monotonic() + timeout_seconds
    latest = None
    while time.monotonic() < deadline:
        latest = SolverOwnership(runtime, owner="resource-admission-cleanup").status()
        if not latest["collision"] and latest["lease"]["state"] == "absent":
            return latest
        time.sleep(0.25)
    raise TimeoutError(f"resource admission detached worker cleanup did not complete: {latest}")


def _recover_submitted_job(manager: JobManager, job_id: str, runtime: Path) -> dict:
    recovery: dict[str, object] = {"job_id": job_id, "passed": False}
    status = None
    try:
        status = manager.status(job_id)
        recovery["status_before"] = status
    except Exception as exc:
        recovery["status_error"] = type(exc).__name__
    if not isinstance(status, dict) or status.get("status") not in TERMINAL_STATES:
        try:
            recovery["cancel"] = manager.cancel(job_id)
        except Exception as exc:
            recovery["cancel_error"] = type(exc).__name__
    else:
        recovery["cancel"] = {"requested": False, "reason": "already_terminal"}
    try:
        terminal = _poll_terminal(manager, job_id, 30.0)
        recovery["terminal"] = terminal
    except Exception as exc:
        terminal = None
        recovery["terminal_error"] = type(exc).__name__
    try:
        cleanup = _poll_cleanup(runtime, 30.0)
        recovery["cleanup"] = cleanup
    except Exception as exc:
        cleanup = None
        recovery["cleanup_error"] = type(exc).__name__
    recovery["passed"] = (
        isinstance(terminal, dict)
        and terminal.get("status") in TERMINAL_STATES
        and isinstance(cleanup, dict)
        and cleanup.get("collision") is False
        and cleanup.get("lease", {}).get("state") == "absent"
    )
    return recovery


def _run_gate() -> None:
    runtime = Path(os.environ.get("COMSOL_MCP_RUNTIME_DIR", "D:/comsol_runtime"))
    artifact_dir = runtime / "resource_admission"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    source_path = artifact_dir / "resource_admission_source.mph"
    mesh_receipt_path = artifact_dir / "mesh_gate_receipt.json"
    result_path = artifact_dir / "resource_gate_result.json"
    result = {"success": False}
    exit_code = 1
    manager = None
    job_id = None
    try:
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--build-source",
            str(runtime),
            str(source_path),
            str(mesh_receipt_path),
        ]
        builder = _run_builder_process(command)
        result["builder_process"] = builder
        if builder["success"] is not True:
            result["builder_lease_recovery"] = SolverOwnership(
                runtime, owner="resource-admission-builder-recovery"
            ).recover_stale()
            raise RuntimeError(
                "resource admission source builder failed: "
                + str(builder)[-2000:]
            )
        mesh_receipt = json.loads(mesh_receipt_path.read_text(encoding="utf-8"))
        if not mesh_receipt.get("success") or not mesh_receipt.get("lease_release", {}).get("success"):
            raise AssertionError(f"resource admission mesh receipt failed: {mesh_receipt}")
        source_hash = _sha256(source_path)
        source_stat = source_path.stat()

        _poll_cleanup(runtime, 30.0)
        manager = JobManager(runtime / "jobs")
        submitted = manager.submit(
            {
                "job_type": "staged_sweep",
                "source_model_path": str(source_path),
                "parameter_name": "V0",
                "parameter_unit": "V",
                "parameter_values": [1.0],
                "expressions": ["2*es.intWe/V0^2"],
                "physical_bounds": {"2*es.intWe/V0^2": [0.0, 1.0e-9]},
                "study_name": "std1",
                "version": "6.4",
                "cores": 1,
                "smoke_points": 1,
                "resource_policy": _resource_policy(),
            }
        )
        job_id = submitted["job_id"]
        terminal = _poll_terminal(manager, job_id, 180.0)
        if terminal["status"] != "completed" or terminal["progress"] != {
            "completed": 1,
            "total": 1,
        }:
            raise AssertionError(
                f"resource admission detached job did not complete one point: {terminal}; "
                f"tail={manager.tail(job_id, 50)}"
            )
        job_dir = manager.store.job_dir(job_id)
        with (job_dir / "results.csv").open(
            newline="", encoding="utf-8-sig"
        ) as handle:
            rows = list(csv.DictReader(handle))
        if len(rows) != 1 or rows[0].get("status") != "ok":
            raise AssertionError(f"resource admission result row mismatch: {rows}")
        capacitance = float(rows[0]["2*es.intWe/V0^2"])
        if not 0.0 < capacitance < 1.0e-9:
            raise AssertionError(f"resource admission capacitance is outside bounds: {capacitance}")
        journal = manager.store.read_resource_journal(job_id)
        journal_acceptance = _resource_journal_acceptance(journal)
        if journal_acceptance["passed"] is not True:
            raise AssertionError(
                f"resource admission journal evidence is incomplete: {journal_acceptance}"
            )
        final_stat = source_path.stat()
        source_unchanged = (
            _sha256(source_path) == source_hash
            and final_stat.st_mtime_ns == source_stat.st_mtime_ns
            and final_stat.st_size == source_stat.st_size
        )
        if not source_unchanged:
            raise AssertionError("resource admission immutable source changed")
        final_status = _poll_cleanup(runtime, 30.0)
        result.update(
            success=True,
            source_sha256=source_hash,
            source_unchanged=True,
            mesh_receipt=mesh_receipt,
            job_id=job_id,
            job_state=terminal,
            result_row=rows[0],
            resource_journal=journal,
            resource_journal_acceptance=journal_acceptance,
            cleanup_status=final_status,
            recovery_stage="not_applicable_no_refusal_or_interruption",
        )
        exit_code = 0
    except Exception as exc:
        result["error"] = str(exc)
        result["traceback"] = traceback.format_exc(limit=10)
        if manager is not None and job_id is not None:
            result["post_submit_recovery"] = _recover_submitted_job(
                manager, job_id, runtime
            )
    finally:
        result_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(result, ensure_ascii=False), flush=True)
        raise SystemExit(exit_code)


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--build-source":
        _build_source(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
    _run_gate()
