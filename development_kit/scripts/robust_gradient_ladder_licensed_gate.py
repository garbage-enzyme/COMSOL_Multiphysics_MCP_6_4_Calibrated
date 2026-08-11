"""Run the alpha7.2 owned native-gradient and bounded-optimizer S4 ladder."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import psutil

from comsol_mcp.durable import atomic_write_json
from comsol_mcp.tools.ownership import SolverOwnership

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_NAME = "comsol_mcp.robust_gradient_ladder_licensed_gate"
SCHEMA_VERSION = "1.0.0"
_GRADIENT_STAGES = ("native", "finite_difference", "directional")
_STAGES = (*_GRADIENT_STAGES, "gcmma")
_SUFFIXES = {
    "native": "n",
    "finite_difference": "f",
    "directional": "d",
    "gcmma": "g",
    "mma": "m",
}
_RECEIPT_NAMES = {
    "native": "gradient-receipt.json",
    "finite_difference": "fd-receipt.json",
    "directional": "directional-receipt.json",
    "gcmma": "native-optimizer-receipt.json",
    "mma": "native-optimizer-receipt.json",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-root", type=Path, required=True)
    parser.add_argument("--source-model", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--tree-audit", type=Path, required=True)
    parser.add_argument("--cores", type=int, required=True)
    parser.add_argument("--validation-max-solves", type=int, required=True)
    parser.add_argument("--optimizer-max-solves", type=int, required=True)
    parser.add_argument("--max-iterations", type=int, required=True)
    parser.add_argument("--max-wall-time-seconds", type=int, required=True)
    parser.add_argument("--max-commit-fraction", type=float, required=True)
    parser.add_argument("--max-disk-bytes", type=int, required=True)
    parser.add_argument("--max-review-items", type=int, required=True)
    parser.add_argument("--max-elements-per-model", type=int, required=True)
    parser.add_argument("--minimum-element-quality", type=float, required=True)
    parser.add_argument("--minimum-available-memory-bytes", type=int, required=True)
    parser.add_argument("--minimum-runtime-free-bytes", type=int, required=True)
    parser.add_argument("--run-mma", action="store_true")
    parser.add_argument("--mma-max-solves", type=int)
    parser.add_argument("--mma-max-iterations", type=int)
    parser.add_argument("--mma-max-wall-time-seconds", type=int)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _positive_integer(value: object, name: str, *, maximum: int = 1 << 50) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be a caller-supplied positive integer")
    return value


def _git_identity() -> dict[str, Any]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable is unavailable")
    revision = subprocess.run(  # noqa: S603
        [git, "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    status = subprocess.run(  # noqa: S603
        [git, "status", "--porcelain"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    return {"revision": revision, "clean": not status.strip()}


def _spec(args: argparse.Namespace) -> dict[str, Any]:
    root = args.test_root.resolve(strict=True)
    approved = Path("D:/mcp_tests").resolve(strict=False)
    if os.name == "nt" and (root.parent != approved or len(root.name) > 11):
        raise ValueError("test root must be a direct short child of D:/mcp_tests")
    if not str(root).isascii():
        raise ValueError("test root must be ASCII")
    source = args.source_model.resolve(strict=True)
    manifest = args.manifest.resolve(strict=True)
    audit = args.tree_audit.resolve(strict=True)
    if source.suffix.casefold() != ".mph":
        raise ValueError("source model must be an MPH file")
    for path, name in ((manifest, "manifest"), (audit, "tree audit")):
        if not path.is_file() or not str(path).isascii():
            raise ValueError(f"{name} must be an ASCII regular file")
    cores = _positive_integer(args.cores, "cores", maximum=1024)
    available_cores = os.cpu_count()
    if not isinstance(available_cores, int) or cores > available_cores:
        raise ValueError("cores exceeds live host capacity or capacity is unavailable")
    integer_fields = {
        "validation_max_solves": (args.validation_max_solves, 100_000),
        "optimizer_max_solves": (args.optimizer_max_solves, 100_000),
        "max_iterations": (args.max_iterations, 10_000),
        "max_wall_time_seconds": (args.max_wall_time_seconds, 31_536_000),
        "max_disk_bytes": (args.max_disk_bytes, 1 << 50),
        "max_review_items": (args.max_review_items, 100_000),
        "max_elements_per_model": (args.max_elements_per_model, 1_000_000_000),
        "minimum_available_memory_bytes": (args.minimum_available_memory_bytes, 1 << 60),
        "minimum_runtime_free_bytes": (args.minimum_runtime_free_bytes, 1 << 60),
    }
    normalized = {
        name: _positive_integer(value, name, maximum=maximum)
        for name, (value, maximum) in integer_fields.items()
    }
    mma_budget_values = {
        "mma_max_solves": (args.mma_max_solves, 100_000),
        "mma_max_iterations": (args.mma_max_iterations, 10_000),
        "mma_max_wall_time_seconds": (args.mma_max_wall_time_seconds, 31_536_000),
    }
    if args.run_mma:
        normalized.update(
            {
                name: _positive_integer(value, name, maximum=maximum)
                for name, (value, maximum) in mma_budget_values.items()
            }
        )
    elif any(value is not None for value, _maximum in mma_budget_values.values()):
        raise ValueError("MMA budgets require explicit --run-mma")
    commit_fraction = float(args.max_commit_fraction)
    minimum_quality = float(args.minimum_element_quality)
    if not math.isfinite(commit_fraction) or not 0.0 < commit_fraction <= 1.0:
        raise ValueError("max_commit_fraction must be caller supplied in (0, 1]")
    if not math.isfinite(minimum_quality) or not 0.0 < minimum_quality <= 1.0:
        raise ValueError("minimum_element_quality must be caller supplied in (0, 1]")
    child_roots = {stage: root.with_name(root.name + suffix) for stage, suffix in _SUFFIXES.items()}
    if any(len(path.name) > 12 or path.parent != approved for path in child_roots.values()):
        raise ValueError("derived stage roots exceed the Windows path budget")
    return {
        "root": root,
        "source": source,
        "source_sha256": _sha(source),
        "manifest": manifest,
        "tree_audit": audit,
        "cores": cores,
        **normalized,
        "max_commit_fraction": commit_fraction,
        "minimum_element_quality": minimum_quality,
        "run_mma": bool(args.run_mma),
        "child_roots": child_roots,
        "receipt": root / "ladder-receipt.json",
        "private_receipt": root / "ladder-private.json",
    }


def _common_args(
    spec: dict[str, Any],
    root: Path,
    *,
    max_solves: int,
    max_iterations: int | None = None,
    max_wall_time_seconds: int | None = None,
) -> list[str]:
    return [
        "--test-root",
        str(root),
        "--source-model",
        str(spec["source"]),
        "--manifest",
        str(spec["manifest"]),
        "--tree-audit",
        str(spec["tree_audit"]),
        "--cores",
        str(spec["cores"]),
        "--optimizer-method",
        "gcmma",
        "--max-solves",
        str(max_solves),
        "--max-iterations",
        str(spec["max_iterations"] if max_iterations is None else max_iterations),
        "--max-wall-time-seconds",
        str(
            spec["max_wall_time_seconds"]
            if max_wall_time_seconds is None
            else max_wall_time_seconds
        ),
        "--max-commit-fraction",
        format(spec["max_commit_fraction"], ".17g"),
        "--max-disk-bytes",
        str(spec["max_disk_bytes"]),
        "--max-review-items",
        str(spec["max_review_items"]),
    ]


def _stage_command(spec: dict[str, Any], stage: str) -> list[str]:
    root = spec["child_roots"][stage]
    if stage == "native":
        return [
            sys.executable,
            "-m",
            "development_kit.scripts.native_gradient_licensed_gate",
            *_common_args(spec, root, max_solves=spec["validation_max_solves"]),
            "--mode",
            "full-vector",
        ]
    if stage in {"finite_difference", "directional"}:
        module = (
            "development_kit.scripts.native_gradient_fd_licensed_gate"
            if stage == "finite_difference"
            else "development_kit.scripts.native_gradient_directional_licensed_gate"
        )
        return [
            sys.executable,
            "-m",
            module,
            *_common_args(spec, root, max_solves=spec["validation_max_solves"]),
            "--mode",
            "full-vector",
            "--native-receipt",
            str(spec["child_roots"]["native"] / _RECEIPT_NAMES["native"]),
        ]
    if stage in {"gcmma", "mma"}:
        max_solves = spec["optimizer_max_solves"] if stage == "gcmma" else spec["mma_max_solves"]
        max_iterations = spec["max_iterations"] if stage == "gcmma" else spec["mma_max_iterations"]
        max_wall_time_seconds = (
            spec["max_wall_time_seconds"] if stage == "gcmma" else spec["mma_max_wall_time_seconds"]
        )
        command = [
            sys.executable,
            "-m",
            "development_kit.scripts.native_optimizer_licensed_gate",
            *_common_args(
                spec,
                root,
                max_solves=max_solves,
                max_iterations=max_iterations,
                max_wall_time_seconds=max_wall_time_seconds,
            ),
            "--max-elements-per-model",
            str(spec["max_elements_per_model"]),
            "--minimum-element-quality",
            format(spec["minimum_element_quality"], ".17g"),
        ]
        method_index = command.index("--optimizer-method") + 1
        command[method_index] = stage
        return command
    raise ValueError("unknown robust gradient ladder stage")


def _startup_admission(spec: dict[str, Any]) -> dict[str, Any]:
    available = int(psutil.virtual_memory().available)
    runtime_free = int(shutil.disk_usage(spec["root"]).free)
    admitted = (
        available >= spec["minimum_available_memory_bytes"]
        and runtime_free >= spec["minimum_runtime_free_bytes"]
    )
    return {
        "available_memory_bytes": available,
        "minimum_available_memory_bytes": spec["minimum_available_memory_bytes"],
        "runtime_free_bytes": runtime_free,
        "minimum_runtime_free_bytes": spec["minimum_runtime_free_bytes"],
        "check_frequency": "startup_only",
        "admitted": admitted,
    }


def _run_child(spec: dict[str, Any], stage: str) -> dict[str, Any]:
    root = spec["child_roots"][stage]
    root.mkdir(parents=False, exist_ok=False)
    stdout_path = spec["root"] / f"{stage}.stdout.log"
    stderr_path = spec["root"] / f"{stage}.stderr.log"
    command = _stage_command(spec, stage)
    stage_wall_time = (
        spec["mma_max_wall_time_seconds"] if stage == "mma" else spec["max_wall_time_seconds"]
    )
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        completed = subprocess.run(  # noqa: S603
            command,
            cwd=REPOSITORY_ROOT,
            stdout=stdout,
            stderr=stderr,
            timeout=stage_wall_time + 300,
            check=False,
            creationflags=creationflags,
        )
    receipt_path = root / _RECEIPT_NAMES[stage]
    if not receipt_path.is_file():
        raise RuntimeError(f"{stage} did not publish its required receipt")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    return {
        "returncode": completed.returncode,
        "receipt": receipt,
        "receipt_sha256": _sha(receipt_path),
        "stdout_sha256": _sha(stdout_path),
        "stderr_sha256": _sha(stderr_path),
    }


def _verify_stage(stage: str, result: dict[str, Any], *, revision: str, source_sha256: str) -> None:
    receipt = result["receipt"]
    if receipt.get("source_revision") != revision or receipt.get("source_sha256") != source_sha256:
        raise ValueError(f"{stage} receipt identity differs from the ladder source")
    if stage in {"gcmma", "mma"} and receipt.get("optimizer_method") != stage:
        raise ValueError(f"{stage} receipt reports a different optimizer method")
    if stage != "mma" and (result["returncode"] != 0 or receipt.get("success") is not True):
        raise ValueError(f"{stage} licensed stage failed")


def _gradient_checks(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    native = results["native"]["receipt"]
    finite_difference = results["finite_difference"]["receipt"]
    directional = results["directional"]["receipt"]
    variables = [item["variable_id"] for item in native.get("derivatives", [])]
    fd_variables = [item["variable_id"] for item in finite_difference.get("derivatives", [])]
    selected_errors = [
        float(item["selected"]["relative_error"])
        for item in finite_difference.get("derivatives", [])
    ]
    selected_signs = [
        item["selected"].get("sign_agreement") is True
        for item in finite_difference.get("derivatives", [])
    ]
    steps_match = all(
        [float(step["relative_step"]) for step in item.get("steps", [])] == [0.01, 0.003, 0.001]
        for item in finite_difference.get("derivatives", [])
    )
    checks = {
        "variable_order_matches": variables == ["patch_length_x", "patch_length_y"] == fd_variables,
        "three_step_policy_matches": steps_match,
        "component_relative_errors_within_10_percent": bool(selected_errors)
        and all(item <= 0.10 for item in selected_errors),
        "component_signs_agree": bool(selected_signs) and all(selected_signs),
        "cosine_at_least_0_995": float(finite_difference.get("cosine_similarity", -2.0)) >= 0.995,
        "directional_relative_error_within_5_percent": float(directional.get("relative_error", 2.0))
        <= 0.05,
        "directional_sign_agrees": directional.get("sign_agreement") is True,
    }
    return {"checks": checks, "passed": all(checks.values())}


def _optimizer_disposition(result: dict[str, Any]) -> dict[str, Any]:
    receipt = result["receipt"]
    success = result["returncode"] == 0 and receipt.get("success") is True
    fresh_delta = receipt.get("fresh_forward_delta")
    accepted = (
        success
        and not isinstance(fresh_delta, bool)
        and isinstance(fresh_delta, (int, float))
        and math.isfinite(float(fresh_delta))
        and float(fresh_delta) > 0.0
    )
    return {
        "method": receipt.get("optimizer_method"),
        "execution_success": success,
        "fresh_forward_delta": fresh_delta,
        "disposition": "accepted" if accepted else "rejected",
        "automatic_fallback_used": False,
    }


def _dry_run(spec: dict[str, Any]) -> dict[str, Any]:
    stages = [*_STAGES, *(["mma"] if spec["run_mma"] else [])]
    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": True,
        "dry_run": True,
        "source_sha256": spec["source_sha256"],
        "stages": stages,
        "commands": {stage: _stage_command(spec, stage)[2] for stage in stages},
        "budgets": {
            key: spec[key]
            for key in (
                "cores",
                "validation_max_solves",
                "optimizer_max_solves",
                "max_iterations",
                "max_wall_time_seconds",
                "max_commit_fraction",
                "max_disk_bytes",
                "max_review_items",
                "max_elements_per_model",
                "minimum_element_quality",
                "minimum_available_memory_bytes",
                "minimum_runtime_free_bytes",
            )
        },
        "mma_budget": (
            {
                "max_solves": spec["mma_max_solves"],
                "max_iterations": spec["mma_max_iterations"],
                "max_wall_time_seconds": spec["mma_max_wall_time_seconds"],
            }
            if spec["run_mma"]
            else None
        ),
        "solver_started": False,
        "filesystem_modified": False,
        "paths_included": False,
    }


def _verify_startup_ownership(ownership: Any) -> None:
    status = ownership.status(require_fresh_inventory=True)
    inventory = status.get("process_inventory", {})
    lease = status.get("lease", {})
    durable_jobs = status.get("durable_jobs", {})
    if inventory.get("complete") is not True:
        raise RuntimeError("fresh solver process inventory is incomplete")
    if lease.get("state") != "absent":
        raise RuntimeError("solver lease is not absent before ladder acquisition")
    if status.get("external_solver_processes"):
        raise RuntimeError("external COMSOL or MPh process blocks the ladder")
    if durable_jobs.get("available") is not True or durable_jobs.get("active_count") != 0:
        raise RuntimeError("active or unreadable durable job inventory blocks the ladder")


def _verify_post_stage_cleanup(ownership: Any, spec: dict[str, Any], stage: str) -> None:
    if not ownership.heartbeat(model_path=str(spec["source"]), refresh_server_processes=True):
        raise RuntimeError(f"solver ownership heartbeat failed after {stage}")
    status = ownership.status(require_fresh_inventory=True)
    inventory = status.get("process_inventory", {})
    lease = status.get("lease", {})
    lease_payload = lease.get("lease", {})
    durable_jobs = status.get("durable_jobs", {})
    if inventory.get("complete") is not True:
        raise RuntimeError(f"fresh process inventory is incomplete after {stage}")
    if lease.get("state") != "active" or lease.get("owned_by_current_process") is not True:
        raise RuntimeError(f"solver lease identity changed after {stage}")
    if lease_payload.get("comsol_server_processes"):
        raise RuntimeError(f"owned COMSOL or Java residue remains after {stage}")
    if status.get("external_solver_processes"):
        raise RuntimeError(f"external COMSOL or MPh residue remains after {stage}")
    if durable_jobs.get("available") is not True or durable_jobs.get("active_count") != 0:
        raise RuntimeError(f"active or unreadable durable job inventory remains after {stage}")


def _run(
    spec: dict[str, Any],
    *,
    child_runner: Callable[[dict[str, Any], str], dict[str, Any]] = _run_child,
    ownership_factory: Callable[[], Any] = SolverOwnership,
) -> tuple[dict[str, Any], dict[str, Any]]:
    git = _git_identity()
    if not git["clean"]:
        raise RuntimeError("robust gradient ladder requires a clean source tree")
    admission = _startup_admission(spec)
    if not admission["admitted"]:
        raise RuntimeError("robust gradient ladder startup admission failed")
    receipt: dict[str, Any] = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": False,
        "dry_run": False,
        "source_revision": git["revision"],
        "source_sha256": spec["source_sha256"],
        "startup_admission": admission,
        "stage_receipts": {},
        "paths_included": False,
    }
    private: dict[str, Any] = {
        "stage_roots": {key: str(value) for key, value in spec["child_roots"].items()}
    }
    ownership = ownership_factory()
    lease_acquired = False
    results: dict[str, dict[str, Any]] = {}
    try:
        _verify_startup_ownership(ownership)
        lease = ownership.acquire(
            mode="alpha7.2_s4_licensed_ladder", model_path=str(spec["source"])
        )
        if not lease.get("success") or not lease.get("acquired"):
            raise RuntimeError("exclusive solver ownership could not be acquired")
        lease_acquired = True
        for stage in _GRADIENT_STAGES:
            if not ownership.heartbeat(
                model_path=str(spec["source"]), refresh_server_processes=True
            ):
                raise RuntimeError(f"solver ownership heartbeat failed before {stage}")
            try:
                result = child_runner(spec, stage)
            finally:
                _verify_post_stage_cleanup(ownership, spec, stage)
            _verify_stage(
                stage,
                result,
                revision=git["revision"],
                source_sha256=spec["source_sha256"],
            )
            results[stage] = result
            receipt["stage_receipts"][stage] = {
                "receipt_sha256": result["receipt_sha256"],
                "returncode": result["returncode"],
                "success": result["receipt"].get("success") is True,
                "stdout_sha256": result["stdout_sha256"],
                "stderr_sha256": result["stderr_sha256"],
            }
        gradient = _gradient_checks(results)
        receipt["gradient_acceptance"] = gradient
        if not gradient["passed"]:
            raise ValueError("gradient acceptance failed before optimizer execution")
        optimizer_stages = ["gcmma", *(["mma"] if spec["run_mma"] else [])]
        for stage in optimizer_stages:
            if not ownership.heartbeat(
                model_path=str(spec["source"]), refresh_server_processes=True
            ):
                raise RuntimeError(f"solver ownership heartbeat failed before {stage}")
            try:
                result = child_runner(spec, stage)
            finally:
                _verify_post_stage_cleanup(ownership, spec, stage)
            _verify_stage(
                stage,
                result,
                revision=git["revision"],
                source_sha256=spec["source_sha256"],
            )
            results[stage] = result
            receipt["stage_receipts"][stage] = {
                "receipt_sha256": result["receipt_sha256"],
                "returncode": result["returncode"],
                "success": result["receipt"].get("success") is True,
                "stdout_sha256": result["stdout_sha256"],
                "stderr_sha256": result["stderr_sha256"],
            }
        gcmma = _optimizer_disposition(results["gcmma"])
        mma = _optimizer_disposition(results["mma"]) if "mma" in results else None
        receipt.update(
            {
                "gcmma": gcmma,
                "mma": mma,
                "success": gradient["passed"] and gcmma["disposition"] == "accepted",
            }
        )
    except Exception as exc:
        receipt["error"] = {"code": "robust_gradient_ladder_failed", "type": type(exc).__name__}
        private["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        cleanup = {
            "lease_released": not lease_acquired,
            "source_unchanged": _sha(spec["source"]) == spec["source_sha256"],
        }
        if lease_acquired:
            released = ownership.release()
            cleanup["lease_released"] = bool(released.get("success") and released.get("released"))
            if not cleanup["lease_released"]:
                private["lease_cleanup_error"] = released
        receipt["cleanup"] = cleanup
        receipt["success"] = receipt.get("success") is True and all(cleanup.values())
    return receipt, private


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    spec = _spec(args)
    if args.dry_run:
        print(json.dumps(_dry_run(spec), ensure_ascii=False, sort_keys=True))
        return 0
    receipt, private = _run(spec)
    atomic_write_json(spec["receipt"], receipt)
    atomic_write_json(spec["private_receipt"], private)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
