"""Run the alpha7.2 owned native-gradient and bounded-optimizer S4 ladder."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

import psutil

from comsol_mcp.durable import atomic_write_json
from comsol_mcp.research.gradient_contracts import normalize_native_optimizer_configuration
from comsol_mcp.research.robust_gradient_acceptance import assess_licensed_gradient_ladder
from comsol_mcp.research.robust_optimizer_policy import assess_robust_optimizer_execution
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
_GRADIENT_POLICY = {
    "schema_name": "comsol_mcp.robust_gradient_acceptance_policy",
    "schema_version": "1.0.0",
    "component_relative_error_limit": 0.10,
    "directional_relative_error_limit": 0.05,
    "cosine_floor": 0.995,
    "require_sign": True,
    "required_relative_steps": [0.01, 0.003, 0.001],
}
_MAX_STAGE_RECEIPT_BYTES = 16 * 1024 * 1024
_MAX_STAGE_FILES = 100_000


class DeformationFeasibilityStageError(RuntimeError):
    """A licensed optimizer stage reported a deformation feasibility failure."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-root", type=Path, required=True)
    parser.add_argument("--source-model", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--tree-audit", type=Path, required=True)
    parser.add_argument("--cores", type=int, required=True)
    parser.add_argument("--validation-max-solves", type=int, required=True)
    parser.add_argument("--optimizer-max-solves", type=int, required=True)
    parser.add_argument("--total-max-solves", type=int, required=True)
    parser.add_argument("--max-iterations", type=int, required=True)
    parser.add_argument("--gcmma-optimizer-iterations", type=int, required=True)
    parser.add_argument("--gcmma-move-limit", type=float, required=True)
    parser.add_argument("--validation-max-wall-time-seconds", type=int, required=True)
    parser.add_argument("--optimizer-max-wall-time-seconds", type=int, required=True)
    parser.add_argument("--total-max-wall-time-seconds", type=int, required=True)
    parser.add_argument("--max-commit-fraction", type=float, required=True)
    parser.add_argument("--max-disk-bytes", type=int, required=True)
    parser.add_argument("--max-review-items", type=int, required=True)
    parser.add_argument("--max-elements-per-model", type=int, required=True)
    parser.add_argument("--minimum-element-quality", type=float, required=True)
    parser.add_argument("--deformation-jacobian-expression", required=True)
    parser.add_argument("--minimum-relative-jacobian", type=float, required=True)
    parser.add_argument("--minimum-available-memory-bytes", type=int, required=True)
    parser.add_argument("--minimum-runtime-free-bytes", type=int, required=True)
    parser.add_argument("--run-mma", action="store_true")
    parser.add_argument("--mma-max-solves", type=int)
    parser.add_argument("--mma-max-iterations", type=int)
    parser.add_argument("--mma-optimizer-iterations", type=int)
    parser.add_argument("--mma-move-limit", type=float)
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


def _positive_finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name} must be a caller-supplied positive finite number")
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a caller-supplied positive finite number") from exc
    if not math.isfinite(normalized) or normalized <= 0.0:
        raise ValueError(f"{name} must be a caller-supplied positive finite number")
    return normalized


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
        "total_max_solves": (args.total_max_solves, 500_000),
        "max_iterations": (args.max_iterations, 10_000),
        "validation_max_wall_time_seconds": (
            args.validation_max_wall_time_seconds,
            31_536_000,
        ),
        "optimizer_max_wall_time_seconds": (
            args.optimizer_max_wall_time_seconds,
            31_536_000,
        ),
        "total_max_wall_time_seconds": (args.total_max_wall_time_seconds, 63_072_000),
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
    gcmma_optimizer_iterations = _positive_integer(
        args.gcmma_optimizer_iterations,
        "gcmma_optimizer_iterations",
        maximum=10_000,
    )
    if gcmma_optimizer_iterations > normalized["max_iterations"]:
        raise ValueError("gcmma_optimizer_iterations exceeds max_iterations")
    gcmma_move_limit = _positive_finite(args.gcmma_move_limit, "gcmma_move_limit")
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
        mma_optimizer_iterations = _positive_integer(
            args.mma_optimizer_iterations,
            "mma_optimizer_iterations",
            maximum=10_000,
        )
        if mma_optimizer_iterations > normalized["mma_max_iterations"]:
            raise ValueError("mma_optimizer_iterations exceeds mma_max_iterations")
        mma_move_limit = _positive_finite(args.mma_move_limit, "mma_move_limit")
    elif any(value is not None for value, _maximum in mma_budget_values.values()) or (
        args.mma_optimizer_iterations is not None or args.mma_move_limit is not None
    ):
        raise ValueError("MMA budgets require explicit --run-mma")
    else:
        mma_optimizer_iterations = None
        mma_move_limit = None
    declared_base_solves = (
        len(_GRADIENT_STAGES) * normalized["validation_max_solves"]
        + normalized["optimizer_max_solves"]
    )
    if declared_base_solves > normalized["total_max_solves"]:
        raise ValueError("declared validation and GCMMA solve caps exceed total_max_solves")
    declared_base_wall_time = (
        len(_GRADIENT_STAGES) * normalized["validation_max_wall_time_seconds"]
        + normalized["optimizer_max_wall_time_seconds"]
    )
    if declared_base_wall_time > normalized["total_max_wall_time_seconds"]:
        raise ValueError(
            "declared validation and GCMMA wall caps exceed total_max_wall_time_seconds"
        )
    commit_fraction = float(args.max_commit_fraction)
    minimum_quality = float(args.minimum_element_quality)
    minimum_relative_jacobian = float(args.minimum_relative_jacobian)
    if not math.isfinite(commit_fraction) or not 0.0 < commit_fraction <= 1.0:
        raise ValueError("max_commit_fraction must be caller supplied in (0, 1]")
    if not math.isfinite(minimum_quality) or not 0.0 < minimum_quality <= 1.0:
        raise ValueError("minimum_element_quality must be caller supplied in (0, 1]")
    jacobian_expression = args.deformation_jacobian_expression
    if (
        not isinstance(jacobian_expression, str)
        or not jacobian_expression.strip()
        or len(jacobian_expression) > 256
        or any(ord(character) < 32 for character in jacobian_expression)
    ):
        raise ValueError("deformation_jacobian_expression must be bounded printable text")
    if not math.isfinite(minimum_relative_jacobian) or minimum_relative_jacobian < 0.0:
        raise ValueError("minimum_relative_jacobian must be caller supplied and nonnegative")
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
        "deformation_jacobian_expression": jacobian_expression.strip(),
        "minimum_relative_jacobian": minimum_relative_jacobian,
        "gcmma_optimizer_iterations": gcmma_optimizer_iterations,
        "gcmma_move_limit": gcmma_move_limit,
        "mma_optimizer_iterations": mma_optimizer_iterations,
        "mma_move_limit": mma_move_limit,
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
            spec["validation_max_wall_time_seconds"]
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
            spec["optimizer_max_wall_time_seconds"]
            if stage == "gcmma"
            else spec["mma_max_wall_time_seconds"]
        )
        optimizer_iterations = (
            spec["gcmma_optimizer_iterations"]
            if stage == "gcmma"
            else spec["mma_optimizer_iterations"]
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
            "--optimizer-iterations",
            str(optimizer_iterations),
            "--move-limit",
            format(
                spec["gcmma_move_limit"] if stage == "gcmma" else spec["mma_move_limit"],
                ".17g",
            ),
            "--max-elements-per-model",
            str(spec["max_elements_per_model"]),
            "--minimum-element-quality",
            format(spec["minimum_element_quality"], ".17g"),
            "--deformation-jacobian-expression",
            spec["deformation_jacobian_expression"],
            "--minimum-relative-jacobian",
            format(spec["minimum_relative_jacobian"], ".17g"),
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


def _bounded_tree_bytes(root: Path) -> int:
    total = 0
    files = 0
    for path in root.rglob("*"):
        information = path.lstat()
        attributes = int(getattr(information, "st_file_attributes", 0))
        if path.is_symlink() or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise RuntimeError("licensed stage artifacts contain a reparse point")
        if not path.is_file():
            continue
        files += 1
        if files > _MAX_STAGE_FILES:
            raise RuntimeError("licensed stage artifact count exceeds the bounded maximum")
        total += information.st_size
    return total


def _stage_review_items(stage: str, receipt: dict[str, Any]) -> int:
    if stage in {"native", "finite_difference"}:
        rows = receipt.get("derivatives")
        return len(rows) if isinstance(rows, list) else 0
    if stage == "directional":
        return 1 if "relative_error" in receipt else 0
    if stage in {"gcmma", "mma"}:
        return 1 if receipt.get("optimizer_method") == stage else 0
    raise ValueError("unknown licensed ladder stage")


def _run_child(spec: dict[str, Any], stage: str) -> dict[str, Any]:
    root = spec["child_roots"][stage]
    root.mkdir(parents=False, exist_ok=False)
    stdout_path = spec["root"] / f"{stage}.stdout.log"
    stderr_path = spec["root"] / f"{stage}.stderr.log"
    command = _stage_command(spec, stage)
    stage_wall_time = {
        "native": spec["validation_max_wall_time_seconds"],
        "finite_difference": spec["validation_max_wall_time_seconds"],
        "directional": spec["validation_max_wall_time_seconds"],
        "gcmma": spec["optimizer_max_wall_time_seconds"],
        "mma": spec["mma_max_wall_time_seconds"],
    }[stage]
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
    if receipt_path.stat().st_size > _MAX_STAGE_RECEIPT_BYTES:
        raise RuntimeError(f"{stage} receipt exceeds the bounded byte limit")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    artifact_bytes = (
        _bounded_tree_bytes(root) + stdout_path.stat().st_size + stderr_path.stat().st_size
    )
    return {
        "returncode": completed.returncode,
        "receipt": receipt,
        "receipt_sha256": _sha(receipt_path),
        "stdout_sha256": _sha(stdout_path),
        "stderr_sha256": _sha(stderr_path),
        "artifact_bytes": artifact_bytes,
        "review_items": _stage_review_items(stage, receipt),
    }


def _truthful_mma_terminal(result: dict[str, Any], receipt: dict[str, Any]) -> bool:
    """Admit a failed MMA comparison only with a truthful terminal child receipt."""
    error = receipt.get("error")
    cleanup = receipt.get("cleanup")
    return (
        result["returncode"] != 0
        and isinstance(error, dict)
        and isinstance(error.get("code"), str)
        and 0 < len(error["code"]) <= 128
        and isinstance(cleanup, dict)
        and cleanup.get("client_clear") is True
        and cleanup.get("source_unchanged") is True
    )


def _verify_stage(
    stage: str,
    result: dict[str, Any],
    *,
    revision: str,
    source_sha256: str,
    optimizer_iterations: int | None = None,
    move_limit: float | None = None,
    deformation_policy: dict[str, Any] | None = None,
) -> None:
    receipt = result["receipt"]
    if receipt.get("source_revision") != revision or receipt.get("source_sha256") != source_sha256:
        raise ValueError(f"{stage} receipt identity differs from the ladder source")
    if stage in {"gcmma", "mma"} and receipt.get("optimizer_method") != stage:
        raise ValueError(f"{stage} receipt reports a different optimizer method")
    if stage in {"gcmma", "mma"}:
        solver = receipt.get("solver_move_limit")
        if (
            optimizer_iterations is None
            or move_limit is None
            or deformation_policy is None
            or receipt.get("requested_optimizer_iterations") != optimizer_iterations
            or receipt.get("requested_move_limit") != move_limit
            or receipt.get("deformation_feasibility_policy") != deformation_policy
            or (solver is not None and not isinstance(solver, dict))
            or (isinstance(solver, dict) and solver.get("mmamaxiter") != str(optimizer_iterations))
            or (
                isinstance(solver, dict)
                and not math.isclose(
                    float(solver.get("movelimit", "nan")), move_limit, rel_tol=1e-12, abs_tol=0.0
                )
            )
            or (receipt.get("success") is True and not isinstance(solver, dict))
        ):
            raise ValueError(f"{stage} receipt reports different optimizer execution settings")
    if result["returncode"] != 0 or receipt.get("success") is not True:
        if (
            stage == "gcmma"
            and isinstance(receipt.get("error"), dict)
            and receipt["error"].get("code") == "deformation_feasibility_failed"
        ):
            raise DeformationFeasibilityStageError(
                "GCMMA crossed the caller-declared deformation feasibility boundary"
            )
        # The MMA comparison attempt may end in a truthful terminal failure that
        # is dispositioned downstream; a crash, missing error identity, or an
        # inconsistent exit code must never be silently accepted.
        if stage == "mma" and _truthful_mma_terminal(result, receipt):
            return
        raise ValueError(f"{stage} licensed stage failed")


def _gradient_checks(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    variable_order = [
        item.get("variable_id")
        for item in results["native"]["receipt"].get("derivatives", [])
        if isinstance(item, dict)
    ]
    if variable_order != ["patch_length_x", "patch_length_y"]:
        raise ValueError("licensed ladder native variable order differs from the frozen fixture")
    return assess_licensed_gradient_ladder(
        _GRADIENT_POLICY,
        results["native"]["receipt"],
        results["finite_difference"]["receipt"],
        results["directional"]["receipt"],
        native_receipt_sha256=results["native"]["receipt_sha256"],
        finite_difference_receipt_sha256=results["finite_difference"]["receipt_sha256"],
        directional_receipt_sha256=results["directional"]["receipt_sha256"],
    )


def _optimizer_configuration(spec: dict[str, Any], stage: str) -> dict[str, Any]:
    if stage not in {"gcmma", "mma"}:
        raise ValueError("optimizer configuration requires GCMMA or MMA")
    max_solves = spec["optimizer_max_solves"] if stage == "gcmma" else spec["mma_max_solves"]
    max_iterations = spec["max_iterations"] if stage == "gcmma" else spec["mma_max_iterations"]
    max_wall_time = (
        spec["optimizer_max_wall_time_seconds"]
        if stage == "gcmma"
        else spec["mma_max_wall_time_seconds"]
    )
    return normalize_native_optimizer_configuration(
        {
            "schema_name": "comsol_mcp.native_optimizer_configuration",
            "schema_version": "1.0.0",
            "optimizer_id": f"alpha72-{stage}-licensed-ladder",
            "backend": "comsol_native",
            "method": stage,
            "move_limit": spec[f"{stage}_move_limit"],
            "optimality_tolerance": 1e-3,
            "constraint_tolerance": 1e-3,
            "budget": {
                "cores": spec["cores"],
                "max_solves": max_solves,
                "max_iterations": max_iterations,
                "max_wall_time_seconds": max_wall_time,
                "max_commit_fraction": spec["max_commit_fraction"],
                "max_disk_bytes": spec["max_disk_bytes"],
                "max_review_items": spec["max_review_items"],
            },
            "checkpoint_policy": {
                "every_accepted_iteration": True,
                "save_copy": True,
                "exact_native_resume_required": False,
            },
            "deterministic_seed": 71004,
        }
    )


def _optimizer_disposition(
    spec: dict[str, Any], stage: str, result: dict[str, Any]
) -> dict[str, Any]:
    return assess_robust_optimizer_execution(
        _optimizer_configuration(spec, stage),
        result["receipt"],
        native_receipt_sha256=result["receipt_sha256"],
        max_elements_per_model=spec["max_elements_per_model"],
        minimum_element_quality=spec["minimum_element_quality"],
        deformation_jacobian_expression=spec["deformation_jacobian_expression"],
        minimum_relative_jacobian=spec["minimum_relative_jacobian"],
    )


def _declared_budgets(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        key: spec[key]
        for key in (
            "cores",
            "validation_max_solves",
            "optimizer_max_solves",
            "total_max_solves",
            "max_iterations",
            "validation_max_wall_time_seconds",
            "optimizer_max_wall_time_seconds",
            "total_max_wall_time_seconds",
            "max_commit_fraction",
            "max_disk_bytes",
            "max_review_items",
            "max_elements_per_model",
            "minimum_element_quality",
            "minimum_available_memory_bytes",
            "minimum_runtime_free_bytes",
        )
    }


def _declared_deformation_feasibility_policy(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "jacobian_expression": spec["deformation_jacobian_expression"],
        "minimum_relative_jacobian": spec["minimum_relative_jacobian"],
        "comparison": "strictly_greater_than",
        "scope": "fresh_forward_finalist_deformed_geometry",
    }


def _declared_mma_budget(spec: dict[str, Any]) -> dict[str, int] | None:
    if not spec["run_mma"]:
        return None
    return {
        "max_solves": spec["mma_max_solves"],
        "max_iterations": spec["mma_max_iterations"],
        "max_wall_time_seconds": spec["mma_max_wall_time_seconds"],
    }


def _declared_optimizer_execution(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "gcmma": {
            "optimizer_iterations": spec["gcmma_optimizer_iterations"],
            "move_limit": spec["gcmma_move_limit"],
        },
        "mma": (
            {
                "optimizer_iterations": spec["mma_optimizer_iterations"],
                "move_limit": spec["mma_move_limit"],
            }
            if spec["run_mma"]
            else None
        ),
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
        "gradient_policy": _GRADIENT_POLICY,
        # Emit the full subprocess command (interpreter + args), not just the
        # module name: a dry-run report must show what would actually run.
        "commands": {stage: _stage_command(spec, stage) for stage in stages},
        "budgets": _declared_budgets(spec),
        "mma_budget": _declared_mma_budget(spec),
        "optimizer_execution": _declared_optimizer_execution(spec),
        "deformation_feasibility_policy": _declared_deformation_feasibility_policy(spec),
        "optimizer_configuration_fingerprints": {
            "gcmma": _optimizer_configuration(spec, "gcmma")["optimizer_fingerprint"],
            "mma": (
                _optimizer_configuration(spec, "mma")["optimizer_fingerprint"]
                if spec["run_mma"]
                else None
            ),
        },
        "budget_semantics": {
            "max_disk_bytes": "cumulative_stage_roots_and_logs_checked_after_each_stage",
            "max_review_items": "cumulative_derivative_and_method_summary_items",
            "stage_receipt_max_bytes": _MAX_STAGE_RECEIPT_BYTES,
            "base_solve_cap": "three_validation_stage_caps_plus_gcmma_cap_within_total",
            "base_wall_cap": "three_validation_stage_caps_plus_gcmma_cap_within_total",
            "mma_scope": "separate_explicit_attempt_with_separate_budget",
        },
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
        "stages": [*_STAGES, *(["mma"] if spec["run_mma"] else [])],
        "declared_budgets": _declared_budgets(spec),
        "declared_mma_budget": _declared_mma_budget(spec),
        "declared_optimizer_execution": _declared_optimizer_execution(spec),
        "declared_deformation_feasibility_policy": _declared_deformation_feasibility_policy(spec),
        "automatic_fallback_allowed": False,
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
    artifact_bytes = 0
    review_items = 0
    base_started = 0.0
    try:
        _verify_startup_ownership(ownership)
        lease = ownership.acquire(
            mode="alpha7.2_s4_licensed_ladder", model_path=str(spec["source"])
        )
        if not lease.get("success") or not lease.get("acquired"):
            raise RuntimeError("exclusive solver ownership could not be acquired")
        lease_acquired = True
        base_started = time.monotonic()
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
            artifact_bytes += result["artifact_bytes"]
            review_items += result["review_items"]
            receipt["stage_receipts"][stage] = {
                "receipt_sha256": result["receipt_sha256"],
                "returncode": result["returncode"],
                "success": result["receipt"].get("success") is True,
                "stdout_sha256": result["stdout_sha256"],
                "stderr_sha256": result["stderr_sha256"],
                "artifact_bytes": result["artifact_bytes"],
                "review_items": result["review_items"],
            }
            receipt["budget_usage"] = {
                "artifact_bytes": artifact_bytes,
                "max_disk_bytes": spec["max_disk_bytes"],
                "review_items": review_items,
                "max_review_items": spec["max_review_items"],
                "base_wall_elapsed_seconds": time.monotonic() - base_started,
                "total_max_wall_time_seconds": spec["total_max_wall_time_seconds"],
            }
            if artifact_bytes > spec["max_disk_bytes"]:
                raise RuntimeError("licensed ladder exceeded the caller disk budget")
            if review_items > spec["max_review_items"]:
                raise RuntimeError("licensed ladder exceeded the caller review-item budget")
            if (
                receipt["budget_usage"]["base_wall_elapsed_seconds"]
                > spec["total_max_wall_time_seconds"]
            ):
                raise TimeoutError("licensed ladder exceeded the caller total wall budget")
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
                optimizer_iterations=(
                    spec["gcmma_optimizer_iterations"]
                    if stage == "gcmma"
                    else spec["mma_optimizer_iterations"]
                ),
                move_limit=(
                    spec["gcmma_move_limit"] if stage == "gcmma" else spec["mma_move_limit"]
                ),
                deformation_policy=_declared_deformation_feasibility_policy(spec),
            )
            results[stage] = result
            artifact_bytes += result["artifact_bytes"]
            review_items += result["review_items"]
            receipt["stage_receipts"][stage] = {
                "receipt_sha256": result["receipt_sha256"],
                "returncode": result["returncode"],
                "success": result["receipt"].get("success") is True,
                "stdout_sha256": result["stdout_sha256"],
                "stderr_sha256": result["stderr_sha256"],
                "artifact_bytes": result["artifact_bytes"],
                "review_items": result["review_items"],
            }
            receipt["budget_usage"] = {
                "artifact_bytes": artifact_bytes,
                "max_disk_bytes": spec["max_disk_bytes"],
                "review_items": review_items,
                "max_review_items": spec["max_review_items"],
                "base_wall_elapsed_seconds": (
                    time.monotonic() - base_started
                    if stage == "gcmma"
                    else receipt["budget_usage"]["base_wall_elapsed_seconds"]
                ),
                "total_max_wall_time_seconds": spec["total_max_wall_time_seconds"],
            }
            if artifact_bytes > spec["max_disk_bytes"]:
                raise RuntimeError("licensed ladder exceeded the caller disk budget")
            if review_items > spec["max_review_items"]:
                raise RuntimeError("licensed ladder exceeded the caller review-item budget")
            if (
                stage == "gcmma"
                and receipt["budget_usage"]["base_wall_elapsed_seconds"]
                > spec["total_max_wall_time_seconds"]
            ):
                raise TimeoutError("licensed ladder exceeded the caller total wall budget")
        gcmma = _optimizer_disposition(spec, "gcmma", results["gcmma"])
        mma = _optimizer_disposition(spec, "mma", results["mma"]) if "mma" in results else None
        receipt.update(
            {
                "gcmma": gcmma,
                "mma": mma,
                "success": gradient["passed"] and gcmma["disposition"] == "accepted",
            }
        )
    except Exception as exc:
        code = (
            "deformation_feasibility_failed"
            if isinstance(exc, DeformationFeasibilityStageError)
            else "robust_gradient_ladder_failed"
        )
        receipt["error"] = {"code": code, "type": type(exc).__name__}
        if code == "deformation_feasibility_failed":
            receipt["automatic_move_reduction_used"] = False
            receipt["automatic_method_fallback_used"] = False
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
