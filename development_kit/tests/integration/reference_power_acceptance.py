"""Fresh-process coordinator and worker for the licensed reference-power physical gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
from typing import Any

import psutil


ROOT = Path(__file__).parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.evidence.reference_power_acceptance import (
    MAX_INPUT_BYTES,
    build_reference_power_dry_run_receipt,
    load_bounded_json,
    validate_reference_power_acceptance_contract,
    validate_reference_power_execution_spec,
)
from src.evidence.reference_power_gate import (
    build_reference_power_policies,
    evaluate_reference_power_results,
    inventory_reference_power_artifacts,
)


DEFAULT_CONTRACT = (
    ROOT
    / "development_kit"
    / "release"
    / "integration_fixtures"
    / "reference_power_evidence.json"
)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _runtime_root() -> Path:
    return Path(os.environ.get("COMSOL_MCP_RUNTIME_DIR", "D:/comsol_runtime")).resolve()


def _ancestor_pids(pid: int) -> set[int]:
    result = {pid}
    current = pid
    while current:
        try:
            current = psutil.Process(current).ppid()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            break
        if not current or current in result:
            break
        result.add(current)
    return result


def _lightweight_solver_status() -> dict[str, Any]:
    """Fail-closed collision scan that does not import MPh or COMSOL modules."""
    excluded = _ancestor_pids(os.getpid())
    processes = []
    complete = True
    error = None
    try:
        iterator = psutil.process_iter(["pid", "ppid", "name", "cmdline", "create_time"])
        for process in iterator:
            try:
                item = process.info
                pid = int(item["pid"])
                if pid in excluded:
                    continue
                name = str(item.get("name") or "").casefold()
                command_line = [str(value) for value in (item.get("cmdline") or [])]
                command = " ".join(command_line).casefold()
                kind = None
                if name.split(".")[0] in {"comsol", "comsolmphserver", "comsolbatch"}:
                    kind = "comsol-process"
                elif "mphserver" in name:
                    kind = "comsol-server"
                elif name in {"java", "java.exe"} and "comsol" in command and "server" in command:
                    kind = "comsol-java-server"
                elif any(pattern in command for pattern in ("mph.client", "import mph", "from mph", "-m mph")):
                    kind = "python-mph-client"
                elif name in {"python", "python.exe", "pythonw", "pythonw.exe"}:
                    for argument in command_line[1:]:
                        script = Path(argument.strip('"'))
                        if script.suffix.casefold() != ".py" or not script.is_file():
                            continue
                        if script.stat().st_size <= 2 * 1024 * 1024:
                            source = script.read_text(encoding="utf-8", errors="ignore").casefold()
                            if "mph.client" in source:
                                kind = "python-mph-client-script"
                        break
                if kind:
                    processes.append(
                        {
                            "pid": pid,
                            "parent_pid": item.get("ppid"),
                            "create_time": item.get("create_time"),
                            "kind": kind,
                            "command_line": command_line[:32],
                        }
                    )
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
    except Exception as exc:
        complete = False
        error = f"{type(exc).__name__}: {str(exc)[:300]}"
    lease_path = _runtime_root() / "solver_owner.json"
    lease_state = "absent"
    lease_sha256 = None
    if lease_path.exists():
        try:
            lease_bytes = lease_path.read_bytes()
            json.loads(lease_bytes.decode("utf-8"))
            lease_state = "present"
            lease_sha256 = hashlib.sha256(lease_bytes).hexdigest()
        except Exception as exc:
            lease_state = "uncertain"
            error = f"lease unreadable: {type(exc).__name__}: {str(exc)[:300]}"
    return {
        "complete": complete,
        "error": error,
        "lease_path": str(lease_path),
        "lease_state": lease_state,
        "lease_sha256": lease_sha256,
        "external_solver_processes": processes,
        "collision": not complete or lease_state != "absent" or bool(processes),
    }


def _admit_lightweight_status(status: dict[str, Any]) -> tuple[bool, list[str]]:
    blockers = []
    if status.get("complete") is not True:
        blockers.append("process inventory incomplete")
    if status.get("lease_state") != "absent":
        blockers.append("solver lease is not absent")
    if status.get("external_solver_processes"):
        blockers.append("external COMSOL/MPh solver process detected")
    return not blockers, blockers


def _redacted_status(status: dict[str, Any]) -> dict[str, Any]:
    return {
        "complete": status.get("complete"),
        "error": status.get("error"),
        "lease_state": status.get("lease_state"),
        "lease_sha256": status.get("lease_sha256"),
        "collision": status.get("collision"),
        "external_solver_processes": [
            {
                "pid": item.get("pid"),
                "parent_pid": item.get("parent_pid"),
                "create_time": item.get("create_time"),
                "kind": item.get("kind"),
            }
            for item in status.get("external_solver_processes", [])
        ],
    }


def _worker_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": payload.get("success"),
        "error": payload.get("error"),
        "evaluation": payload.get("evaluation"),
        "client_clear": payload.get("client_clear"),
        "lease_release": payload.get("lease_release"),
    }


def _comsol_pids() -> set[int]:
    result = set()
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = str(process.info.get("name") or "").casefold()
            command = " ".join(str(value) for value in process.info.get("cmdline") or []).casefold()
            if name.split(".")[0] in {"comsol", "comsolmphserver", "comsolbatch"} or (
                name in {"java", "java.exe"} and "comsol" in command and "server" in command
            ):
                result.add(int(process.info["pid"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return result


def _wait_lightweight_clean(timeout_seconds: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        status = _lightweight_solver_status()
        admitted, _blockers = _admit_lightweight_status(status)
        if admitted:
            return status
        if time.monotonic() >= deadline:
            return status
        time.sleep(0.25)


def _load_inputs(contract_path: Path, spec_path: Path | None, *, verify_files: bool):
    contract = validate_reference_power_acceptance_contract(load_bounded_json(contract_path, MAX_INPUT_BYTES))
    spec = None
    if spec_path is not None:
        raw = load_bounded_json(spec_path, contract["limits"]["max_spec_bytes"])
        spec = validate_reference_power_execution_spec(raw, contract, verify_files=verify_files)
    return contract, spec


def _run_worker(args: argparse.Namespace) -> int:
    result: dict[str, Any] = {"success": False, "mode": "worker", "started_at_epoch": time.time()}
    exit_code = 1
    owner = None
    client = None
    source_model = None
    try:
        contract, spec = _load_inputs(args.contract, args.spec, verify_files=True)
        if spec is None:
            raise ValueError("worker requires an execution spec")
        admission = _lightweight_solver_status()
        admitted, blockers = _admit_lightweight_status(admission)
        result["pre_import_admission"] = {
            "admitted": admitted,
            "blockers": blockers,
            "status": _redacted_status(admission),
        }
        if not admitted:
            raise RuntimeError(f"pre-import solver admission refused: {blockers}")

        from src.tools.ownership import SolverOwnership
        import mph
        from src.tools.wave_optics_audit import (
            run_wave_optics_point_audit,
            run_wave_optics_reference_audit,
        )

        owner = SolverOwnership(owner="reference-power-evidence")
        claim = owner.acquire(mode="reference_power_evidence", model_path=spec["source_model_path"])
        result["lease_claim"] = claim
        if not claim.get("success"):
            raise RuntimeError("solver lease acquisition failed")
        client = mph.Client(cores=args.cores)
        source_model = client.load(spec["source_model_path"])
        policies = build_reference_power_policies(contract)
        artifact_root = Path(spec["artifact_dir"]).resolve()
        artifact_root.mkdir(parents=True, exist_ok=True)
        reference_spec = spec["reference_air"]
        model_spec = spec["model"]
        wavelength = spec["wavelength"]
        reference_result = run_wave_optics_reference_audit(
            source_model,
            client,
            model_name=source_model.name(),
            component_tag=model_spec["component_tag"],
            physics_tag=model_spec["physics_tag"],
            study_tag=model_spec["study_tag"],
            study_step_tag=model_spec["study_step_tag"],
            study_step_property=model_spec["study_step_property"],
            wavelength_value=wavelength["value"],
            wavelength_unit=wavelength["unit"],
            wavelength_parameter=wavelength["parameter"],
            expected_source_sha256=spec["expected_source_sha256"],
            config_id=f"{spec['config_id']}.reference",
            reference_method="all_air_clone",
            expected_material_tags=reference_spec["expected_material_tags"],
            all_domain_ids=reference_spec["all_domain_ids"],
            top_air_domain_ids=reference_spec["top_air_domain_ids"],
            top_air_coordinate_range=reference_spec["top_air_coordinate_range"],
            target_axis=reference_spec["target_axis"],
            aggregation=reference_spec["aggregation"],
            artifact_dir=str(artifact_root / "reference"),
            r_expression=reference_spec["r_expression"],
            t_expression=reference_spec["t_expression"],
            validation_policy=policies["reference_air"],
        )
        point_result = run_wave_optics_point_audit(
            source_model,
            model_name=source_model.name(),
            component_tag=model_spec["component_tag"],
            physics_tag=model_spec["physics_tag"],
            study_tag=model_spec["study_tag"],
            wavelength_value=wavelength["value"],
            wavelength_unit=wavelength["unit"],
            wavelength_parameter=wavelength["parameter"],
            study_step_tag=model_spec["study_step_tag"],
            study_step_property=model_spec["study_step_property"],
            expected_source_sha256=spec["expected_source_sha256"],
            config_id=f"{spec['config_id']}.physical",
            artifact_dir=str(artifact_root / "physical"),
            top_air_domain_ids=reference_spec["top_air_domain_ids"],
            top_air_coordinate_range=reference_spec["top_air_coordinate_range"],
            declared_plane_flux=spec["declared_plane_flux"],
            validation_policy=policies["declared_flux"],
            session_state={"connected": True, "models": [source_model.name()]},
            active_profile="wave_optics",
            ownership_preflight={"ready": True},
        )
        evaluation = evaluate_reference_power_results(contract, reference_result, point_result)
        result.update(
            {
                "success": evaluation["passed"],
                "reference_result": reference_result,
                "point_result": point_result,
                "evaluation": evaluation,
            }
        )
        exit_code = 0 if evaluation["passed"] else 1
    except Exception as exc:
        result["error"] = str(exc)[:2000]
        result["traceback"] = traceback.format_exc(limit=20)
    finally:
        if client is not None:
            try:
                client.clear()
                result["client_clear"] = True
            except Exception as exc:
                result["client_clear"] = False
                result["client_clear_error"] = str(exc)[:500]
                exit_code = 1
                result["success"] = False
        if owner is not None:
            result["lease_release"] = owner.release()
            if not result["lease_release"].get("success"):
                exit_code = 1
                result["success"] = False
        result["finished_at_epoch"] = time.time()
        _atomic_json(args.worker_result, result)
        print(json.dumps({"success": result.get("success"), "worker_result": str(args.worker_result)}), flush=True)
        os._exit(exit_code)


def _terminate_owned_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    subprocess.run(
        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _run_coordinator(args: argparse.Namespace) -> int:
    contract, spec = _load_inputs(args.contract, args.spec, verify_files=True)
    if spec is None:
        raise ValueError("licensed coordinator requires --spec")
    before_status = _lightweight_solver_status()
    admitted, blockers = _admit_lightweight_status(before_status)
    receipt: dict[str, Any] = {
        "schema_version": "1.0.0",
        "gate": "reference_power_fresh_process_licensed",
        "pre_import_admission": {
            "admitted": admitted,
            "blockers": blockers,
            "status": _redacted_status(before_status),
        },
        "source_sha256": spec["expected_source_sha256"],
        "config_id": spec["config_id"],
    }
    if not admitted:
        receipt.update({"success": False, "worker_started": False})
        _atomic_json(args.output, receipt)
        return 2
    before_pids = _comsol_pids()
    artifact_root = Path(spec["artifact_dir"]).resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    if any(artifact_root.iterdir()):
        receipt.update(
            {
                "success": False,
                "worker_started": False,
                "artifact_error": "artifact root must be empty before a licensed reference-power run",
            }
        )
        _atomic_json(args.output, receipt)
        return 2
    worker_result = artifact_root / "worker_result.json"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--confirm", "RUN_REAL_COMSOL",
        "--contract", str(args.contract),
        "--spec", str(args.spec),
        "--worker-result", str(worker_result),
        "--cores", str(args.cores),
    ]
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=creation_flags,
    )
    timed_out = False
    try:
        stdout, _ = process.communicate(timeout=args.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_owned_tree(process)
        stdout, _ = process.communicate(timeout=30)
    finally:
        _terminate_owned_tree(process)
    cleanup_status = _wait_lightweight_clean()
    after_pids = _comsol_pids()
    cleanup_passed = (
        cleanup_status["collision"] is False
        and before_pids == after_pids
        and cleanup_status["lease_state"] == "absent"
    )
    worker_payload = (
        json.loads(worker_result.read_text(encoding="utf-8"))
        if worker_result.is_file()
        else {"success": False, "error": "worker result artifact is missing"}
    )
    try:
        artifacts = inventory_reference_power_artifacts(artifact_root, contract["limits"])
        artifact_error = None
    except Exception as exc:
        artifacts = None
        artifact_error = str(exc)[:1000]
    success = (
        not timed_out
        and process.returncode == 0
        and worker_payload.get("success") is True
        and cleanup_passed
        and artifact_error is None
    )
    receipt.update(
        {
            "success": success,
            "worker_started": True,
            "worker_pid": process.pid,
            "worker_returncode": process.returncode,
            "worker_timed_out": timed_out,
            "worker_stdout_tail": stdout[-4000:],
            "worker_result": _worker_summary(worker_payload),
            "cleanup": {
                "status": _redacted_status(cleanup_status),
                "comsol_pid_set_unchanged": before_pids == after_pids,
                "passed": cleanup_passed,
            },
            "artifact_inventory": artifacts,
            "artifact_error": artifact_error,
        }
    )
    _atomic_json(args.output, receipt)
    return 0 if success else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm", choices=["RUN_REAL_COMSOL"])
    parser.add_argument("--cores", type=int)
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--worker-result", type=Path)
    args = parser.parse_args()
    if args.worker:
        if (
            args.confirm != "RUN_REAL_COMSOL"
            or args.spec is None
            or args.worker_result is None
            or not args.cores
            or not 1 <= args.cores <= 64
        ):
            raise SystemExit(
                "worker requires explicit confirmation, --spec, --worker-result, and cores in 1..64"
            )
        return _run_worker(args)
    if args.dry_run:
        contract, spec = _load_inputs(args.contract, args.spec, verify_files=args.spec is not None)
        receipt = build_reference_power_dry_run_receipt(contract, spec, verify_files=False)
        if args.output is not None:
            _atomic_json(args.output, receipt)
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0
    if (
        args.confirm != "RUN_REAL_COMSOL"
        or args.output is None
        or args.spec is None
        or not args.cores
        or not 1 <= args.cores <= 64
        or not args.timeout_seconds
        or not 1.0 <= args.timeout_seconds <= 7200.0
    ):
        raise SystemExit(
            "licensed run requires --confirm RUN_REAL_COMSOL, --spec, a new --output, cores in 1..64, and timeout in 1..7200 seconds"
        )
    if args.output.exists():
        raise SystemExit("licensed run output must not already exist")
    return _run_coordinator(args)


if __name__ == "__main__":
    raise SystemExit(main())
