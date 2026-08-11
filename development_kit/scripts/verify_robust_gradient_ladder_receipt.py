"""Independently verify an alpha7.2 licensed gradient-ladder evidence tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import stat
from pathlib import Path
from typing import Any

from comsol_mcp.durable import atomic_write_json
from comsol_mcp.research.gradient_contracts import normalize_native_optimizer_configuration
from comsol_mcp.research.robust_gradient_acceptance import assess_licensed_gradient_ladder
from comsol_mcp.research.robust_optimizer_policy import assess_robust_optimizer_execution

SCHEMA_NAME = "comsol_mcp.robust_gradient_ladder_verification"
SCHEMA_VERSION = "1.0.0"
LADDER_SCHEMA_NAME = "comsol_mcp.robust_gradient_ladder_licensed_gate"
_GRADIENT_POLICY = {
    "schema_name": "comsol_mcp.robust_gradient_acceptance_policy",
    "schema_version": "1.0.0",
    "component_relative_error_limit": 0.10,
    "directional_relative_error_limit": 0.05,
    "cosine_floor": 0.995,
    "require_sign": True,
    "required_relative_steps": [0.01, 0.003, 0.001],
}
_BASE_STAGES = ("native", "finite_difference", "directional", "gcmma")
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
_MAX_JSON_BYTES = 16 * 1024 * 1024
_MAX_STAGE_FILES = 100_000


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-root", type=Path, required=True)
    parser.add_argument("--source-model", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--cores", type=int, required=True)
    parser.add_argument("--validation-max-solves", type=int, required=True)
    parser.add_argument("--optimizer-max-solves", type=int, required=True)
    parser.add_argument("--total-max-solves", type=int, required=True)
    parser.add_argument("--max-iterations", type=int, required=True)
    parser.add_argument("--validation-max-wall-time-seconds", type=int, required=True)
    parser.add_argument("--optimizer-max-wall-time-seconds", type=int, required=True)
    parser.add_argument("--total-max-wall-time-seconds", type=int, required=True)
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
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > _MAX_JSON_BYTES:
        raise ValueError("required receipt is missing or exceeds the byte limit")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("receipt must be a JSON object")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _expected(args: argparse.Namespace) -> dict[str, Any]:
    root = args.test_root.resolve(strict=True)
    approved = Path("D:/mcp_tests").resolve(strict=False)
    if os.name == "nt" and (root.parent != approved or len(root.name) > 11):
        raise ValueError("test root must be a direct short child of D:/mcp_tests")
    if not str(root).isascii():
        raise ValueError("test root must be ASCII")
    source = args.source_model.resolve(strict=True)
    if source.suffix.casefold() != ".mph" or not source.is_file():
        raise ValueError("source model must be an existing MPH file")
    revision = args.expected_revision.casefold()
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise ValueError("expected revision must be a full Git SHA")
    integer_names = (
        "cores",
        "validation_max_solves",
        "optimizer_max_solves",
        "total_max_solves",
        "max_iterations",
        "validation_max_wall_time_seconds",
        "optimizer_max_wall_time_seconds",
        "total_max_wall_time_seconds",
        "max_disk_bytes",
        "max_review_items",
        "max_elements_per_model",
        "minimum_available_memory_bytes",
        "minimum_runtime_free_bytes",
    )
    budgets = {name: _positive_int(getattr(args, name), name) for name in integer_names}
    commit_fraction = float(args.max_commit_fraction)
    minimum_quality = float(args.minimum_element_quality)
    if not math.isfinite(commit_fraction) or not 0.0 < commit_fraction <= 1.0:
        raise ValueError("max_commit_fraction must be within (0, 1]")
    if not math.isfinite(minimum_quality) or not 0.0 < minimum_quality <= 1.0:
        raise ValueError("minimum_element_quality must be within (0, 1]")
    budgets["max_commit_fraction"] = commit_fraction
    budgets["minimum_element_quality"] = minimum_quality
    declared_solves = 3 * budgets["validation_max_solves"] + budgets["optimizer_max_solves"]
    declared_wall = (
        3 * budgets["validation_max_wall_time_seconds"] + budgets["optimizer_max_wall_time_seconds"]
    )
    if declared_solves > budgets["total_max_solves"]:
        raise ValueError("declared stage solve caps exceed the total")
    if declared_wall > budgets["total_max_wall_time_seconds"]:
        raise ValueError("declared stage wall caps exceed the total")
    mma_values = (args.mma_max_solves, args.mma_max_iterations, args.mma_max_wall_time_seconds)
    if args.run_mma:
        mma_budget = {
            "max_solves": _positive_int(mma_values[0], "mma_max_solves"),
            "max_iterations": _positive_int(mma_values[1], "mma_max_iterations"),
            "max_wall_time_seconds": _positive_int(mma_values[2], "mma_max_wall_time_seconds"),
        }
    else:
        if any(value is not None for value in mma_values):
            raise ValueError("MMA budgets require --run-mma")
        mma_budget = None
    stages = [*_BASE_STAGES, *(["mma"] if args.run_mma else [])]
    roots = {stage: root.with_name(root.name + _SUFFIXES[stage]) for stage in stages}
    return {
        "root": root,
        "source": source,
        "source_sha256": _sha256(source),
        "revision": revision,
        "budgets": budgets,
        "mma_budget": mma_budget,
        "stages": stages,
        "roots": roots,
    }


def _tree_bytes(root: Path) -> int:
    if not root.is_dir():
        raise ValueError("required stage root is missing")
    total = 0
    count = 0
    for path in root.rglob("*"):
        information = path.lstat()
        attributes = int(getattr(information, "st_file_attributes", 0))
        if path.is_symlink() or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("stage artifacts contain a reparse point")
        if path.is_file():
            count += 1
            if count > _MAX_STAGE_FILES:
                raise ValueError("stage artifact count exceeds the limit")
            total += information.st_size
    return total


def _review_items(stage: str, receipt: dict[str, Any]) -> int:
    if stage in {"native", "finite_difference"}:
        rows = receipt.get("derivatives")
        return len(rows) if isinstance(rows, list) else 0
    if stage == "directional":
        return 1 if "relative_error" in receipt else 0
    return 1 if receipt.get("optimizer_method") == stage else 0


def _optimizer_configuration(expected: dict[str, Any], stage: str) -> dict[str, Any]:
    budgets = expected["budgets"]
    if stage == "gcmma":
        max_solves = budgets["optimizer_max_solves"]
        max_iterations = budgets["max_iterations"]
        max_wall = budgets["optimizer_max_wall_time_seconds"]
    else:
        mma = expected["mma_budget"]
        if not isinstance(mma, dict):
            raise ValueError("MMA configuration was not declared")
        max_solves = mma["max_solves"]
        max_iterations = mma["max_iterations"]
        max_wall = mma["max_wall_time_seconds"]
    return normalize_native_optimizer_configuration(
        {
            "schema_name": "comsol_mcp.native_optimizer_configuration",
            "schema_version": "1.0.0",
            "optimizer_id": f"alpha72-{stage}-licensed-ladder",
            "backend": "comsol_native",
            "method": stage,
            "move_limit": 0.1,
            "optimality_tolerance": 1e-3,
            "constraint_tolerance": 1e-3,
            "budget": {
                "cores": budgets["cores"],
                "max_solves": max_solves,
                "max_iterations": max_iterations,
                "max_wall_time_seconds": max_wall,
                "max_commit_fraction": budgets["max_commit_fraction"],
                "max_disk_bytes": budgets["max_disk_bytes"],
                "max_review_items": budgets["max_review_items"],
            },
            "checkpoint_policy": {
                "every_accepted_iteration": True,
                "save_copy": True,
                "exact_native_resume_required": False,
            },
            "deterministic_seed": 71004,
        }
    )


def verify(expected: dict[str, Any]) -> dict[str, Any]:
    root = expected["root"]
    ladder_path = root / "ladder-receipt.json"
    ladder = _json(ladder_path)
    if ladder.get("schema_name") != LADDER_SCHEMA_NAME or ladder.get("schema_version") != "1.0.0":
        raise ValueError("ladder receipt schema identity is unsupported")
    if ladder.get("source_revision") != expected["revision"]:
        raise ValueError("ladder source revision differs from the expected revision")
    if ladder.get("source_sha256") != expected["source_sha256"]:
        raise ValueError("ladder source hash differs from the immutable source")
    if ladder.get("stages") != expected["stages"]:
        raise ValueError("ladder stage ordering differs from the declared sequence")
    if ladder.get("declared_budgets") != expected["budgets"]:
        raise ValueError("ladder caller budgets drifted")
    if ladder.get("declared_mma_budget") != expected["mma_budget"]:
        raise ValueError("ladder MMA budget drifted")
    if ladder.get("automatic_fallback_allowed") is not False:
        raise ValueError("ladder automatic-fallback policy is invalid")
    admission = ladder.get("startup_admission")
    if not isinstance(admission, dict) or admission.get("admitted") is not True:
        raise ValueError("ladder startup admission did not pass")
    if (
        admission.get("minimum_available_memory_bytes")
        != expected["budgets"]["minimum_available_memory_bytes"]
        or admission.get("minimum_runtime_free_bytes")
        != expected["budgets"]["minimum_runtime_free_bytes"]
    ):
        raise ValueError("ladder startup admission thresholds drifted")
    available_memory = admission.get("available_memory_bytes")
    runtime_free = admission.get("runtime_free_bytes")
    if (
        admission.get("check_frequency") != "startup_only"
        or isinstance(available_memory, bool)
        or not isinstance(available_memory, int)
        or available_memory < expected["budgets"]["minimum_available_memory_bytes"]
        or isinstance(runtime_free, bool)
        or not isinstance(runtime_free, int)
        or runtime_free < expected["budgets"]["minimum_runtime_free_bytes"]
    ):
        raise ValueError("ladder startup admission evidence is internally inconsistent")
    summaries = ladder.get("stage_receipts")
    if not isinstance(summaries, dict) or set(summaries) != set(expected["stages"]):
        raise ValueError("ladder stage receipt coverage is incomplete")
    receipts: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    artifact_bytes = 0
    review_items = 0
    for stage in expected["stages"]:
        stage_root = expected["roots"][stage]
        receipt_path = stage_root / _RECEIPT_NAMES[stage]
        stdout_path = root / f"{stage}.stdout.log"
        stderr_path = root / f"{stage}.stderr.log"
        if not stdout_path.is_file() or not stderr_path.is_file():
            raise ValueError(f"{stage} stdout or stderr log is missing")
        receipt = _json(receipt_path)
        summary = summaries.get(stage)
        if not isinstance(summary, dict):
            raise ValueError(f"{stage} stage summary is missing")
        returncode = summary.get("returncode")
        if isinstance(returncode, bool) or not isinstance(returncode, int):
            raise ValueError(f"{stage} return code is invalid")
        receipt_hash = _sha256(receipt_path)
        stdout_hash = _sha256(stdout_path)
        stderr_hash = _sha256(stderr_path)
        stage_bytes = (
            _tree_bytes(stage_root) + stdout_path.stat().st_size + stderr_path.stat().st_size
        )
        stage_review_items = _review_items(stage, receipt)
        expected_summary = {
            "receipt_sha256": receipt_hash,
            "returncode": returncode,
            "success": receipt.get("success") is True,
            "stdout_sha256": stdout_hash,
            "stderr_sha256": stderr_hash,
            "artifact_bytes": stage_bytes,
            "review_items": stage_review_items,
        }
        if summary != expected_summary:
            raise ValueError(f"{stage} stage receipt or log evidence drifted")
        if (
            receipt.get("source_revision") != expected["revision"]
            or receipt.get("source_sha256") != expected["source_sha256"]
        ):
            raise ValueError(f"{stage} source identity drifted")
        if stage != "mma" and (returncode != 0 or receipt.get("success") is not True):
            raise ValueError(f"{stage} falsely reports a completed required stage")
        if stage == "mma" and returncode not in {0, 1}:
            raise ValueError("MMA return code is outside the accepted explicit-attempt boundary")
        if stage in {"gcmma", "mma"} and receipt.get("optimizer_method") != stage:
            raise ValueError(f"{stage} optimizer method drifted")
        receipts[stage] = receipt
        hashes[stage] = receipt_hash
        artifact_bytes += stage_bytes
        review_items += stage_review_items
    usage = ladder.get("budget_usage")
    if not isinstance(usage, dict):
        raise ValueError("ladder budget usage is missing")
    if usage.get("artifact_bytes") != artifact_bytes or usage.get("review_items") != review_items:
        raise ValueError("ladder cumulative disk or review usage drifted")
    if (
        usage.get("max_disk_bytes") != expected["budgets"]["max_disk_bytes"]
        or usage.get("max_review_items") != expected["budgets"]["max_review_items"]
    ):
        raise ValueError("ladder cumulative budget limits drifted")
    if (
        artifact_bytes > expected["budgets"]["max_disk_bytes"]
        or review_items > expected["budgets"]["max_review_items"]
    ):
        raise ValueError("ladder exceeded a caller cumulative budget")
    elapsed = usage.get("base_wall_elapsed_seconds")
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not math.isfinite(float(elapsed))
        or float(elapsed) < 0.0
        or float(elapsed) > expected["budgets"]["total_max_wall_time_seconds"]
        or usage.get("total_max_wall_time_seconds")
        != expected["budgets"]["total_max_wall_time_seconds"]
    ):
        raise ValueError("ladder base wall usage is invalid or outside the caller cap")
    gradient = assess_licensed_gradient_ladder(
        _GRADIENT_POLICY,
        receipts["native"],
        receipts["finite_difference"],
        receipts["directional"],
        native_receipt_sha256=hashes["native"],
        finite_difference_receipt_sha256=hashes["finite_difference"],
        directional_receipt_sha256=hashes["directional"],
    )
    if gradient != ladder.get("gradient_acceptance") or gradient.get("passed") is not True:
        raise ValueError("ladder gradient acceptance is false or noncanonical")
    methods: dict[str, dict[str, Any]] = {}
    for stage in ("gcmma", *(["mma"] if expected["mma_budget"] is not None else [])):
        result = assess_robust_optimizer_execution(
            _optimizer_configuration(expected, stage),
            receipts[stage],
            native_receipt_sha256=hashes[stage],
            max_elements_per_model=expected["budgets"]["max_elements_per_model"],
            minimum_element_quality=expected["budgets"]["minimum_element_quality"],
        )
        if result != ladder.get(stage) or result.get("automatic_fallback_used") is not False:
            raise ValueError(f"{stage} optimizer disposition is noncanonical")
        methods[stage] = result
    if expected["mma_budget"] is None:
        mma_root = root.with_name(root.name + _SUFFIXES["mma"])
        if (
            ladder.get("mma") is not None
            or mma_root.exists()
            or (root / "mma.stdout.log").exists()
            or (root / "mma.stderr.log").exists()
        ):
            raise ValueError("undeclared MMA evidence is present")
    if methods["gcmma"].get("disposition") != "accepted":
        raise ValueError("GCMMA did not produce an accepted fresh-forward result")
    cleanup = ladder.get("cleanup")
    if cleanup != {"lease_released": True, "source_unchanged": True}:
        raise ValueError("ladder cleanup evidence is incomplete")
    if ladder.get("success") is not True:
        raise ValueError("ladder does not report success")
    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "passed": True,
        "ladder_receipt_sha256": _sha256(ladder_path),
        "source_revision": expected["revision"],
        "source_sha256": expected["source_sha256"],
        "stages": expected["stages"],
        "gradient_receipt_fingerprint": gradient["receipt_fingerprint"],
        "optimizer_receipt_fingerprints": {
            stage: result["receipt_fingerprint"] for stage, result in methods.items()
        },
        "budget_usage": {
            "artifact_bytes": artifact_bytes,
            "review_items": review_items,
            "base_wall_elapsed_seconds": float(elapsed),
        },
        "cleanup_verified": True,
        "automatic_fallback_used": False,
        "paths_included": False,
    }


def main(argv: list[str] | None = None) -> int:
    expected = _expected(_parser().parse_args(argv))
    receipt = verify(expected)
    atomic_write_json(expected["root"] / "ladder-verification-receipt.json", receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
