"""Combine branch evidence from identical Windows and Linux source trees."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


def source_identity(root: Path) -> str:
    """Hash tracked content, including tests; normalize checkout line endings."""
    try:
        names = (
            subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode("utf-8").split("\0")
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("cannot read the tracked source inventory") from exc
    digest = hashlib.sha256()
    for name in sorted(set(names) - {""}):
        data = (root / name).read_bytes()
        if b"\0" not in data:
            data = data.replace(b"\r\n", b"\n")
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def _summary(
    lines: set[int],
    missing: set[int],
    arcs: set[tuple[int, int]],
    missing_arcs: set[tuple[int, int]],
) -> dict[str, Any]:
    total = len(lines) + len(arcs)
    covered = total - len(missing) - len(missing_arcs)
    return {
        "percent_covered": 100.0 * covered / total if total else 100.0,
        "num_statements": len(lines),
        "covered_lines": len(lines) - len(missing),
        "missing_lines": len(missing),
        "num_branches": len(arcs),
        "covered_branches": len(arcs) - len(missing_arcs),
        "missing_branches": len(missing_arcs),
    }


def merge_reports(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    for report in (left, right):
        if report.get("meta", {}).get("branch_coverage") is not True:
            raise ValueError("paired evidence requires branch coverage")
    a = {p.replace("\\", "/"): v for p, v in left["files"].items()}
    b = {p.replace("\\", "/"): v for p, v in right["files"].items()}
    if set(a) != set(b):
        raise ValueError("coverage file inventories differ")
    files = {}
    totals: dict[str, Any] = {
        key: 0
        for key in (
            "num_statements",
            "covered_lines",
            "missing_lines",
            "num_branches",
            "covered_branches",
            "missing_branches",
        )
    }
    for path in sorted(a):
        x, y = a[path], b[path]
        lines_x = set(x["executed_lines"]) | set(x["missing_lines"])
        lines_y = set(y["executed_lines"]) | set(y["missing_lines"])
        arcs_x = {tuple(v) for v in x["executed_branches"] + x["missing_branches"]}
        arcs_y = {tuple(v) for v in y["executed_branches"] + y["missing_branches"]}
        if lines_x != lines_y or arcs_x != arcs_y:
            raise ValueError(f"coverage statement or branch inventories differ: {path}")
        missing = set(x["missing_lines"]) & set(y["missing_lines"])
        missing_arcs = {tuple(v) for v in x["missing_branches"]} & {
            tuple(v) for v in y["missing_branches"]
        }
        summary = _summary(lines_x, missing, arcs_x, missing_arcs)
        files[path] = {
            "summary": summary,
            "missing_lines": sorted(missing),
            "missing_branches": sorted(missing_arcs),
        }
        for key in totals:
            totals[key] += summary[key]
    total = totals["num_statements"] + totals["num_branches"]
    totals["percent_covered"] = (
        100.0 * (totals["covered_lines"] + totals["covered_branches"]) / total if total else 100.0
    )
    return {"files": files, "totals": totals}


def verify_pair(paths: list[Path], policy: Path, expected_source: str) -> dict[str, Any]:
    from development_kit.scripts.quality_gate import evaluate_coverage, load_coverage_policy

    if len(paths) != 2:
        raise ValueError("exactly two platform receipts are required")
    receipts = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    if {r.get("platform") for r in receipts} != {"win32", "linux"}:
        raise ValueError("Windows and Linux peers are required")
    policy_hash = hashlib.sha256(policy.read_bytes()).hexdigest()
    reports = []
    for path, receipt in zip(paths, receipts, strict=True):
        if receipt.get("source_tree_sha256") != expected_source:
            raise ValueError("source identity mismatch")
        if receipt.get("coverage_policy_sha256") != policy_hash:
            raise ValueError("coverage policy mismatch")
        if receipt.get("status") not in {"passed", "awaiting_cross_platform_coverage"}:
            raise ValueError("peer quality gate failed")
        if receipt.get("dependency_licenses", {}).get("status") != "passed":
            raise ValueError("peer dependency license gate failed")
        raw = (path.parent / "coverage.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != receipt.get("coverage_json_sha256"):
            raise ValueError("coverage evidence hash mismatch")
        report = json.loads(raw)
        peer_result = evaluate_coverage(report, load_coverage_policy(policy))
        if any(
            item.get("path") != "comsol_mcp/durable/io.py"
            or item.get("reason_code") != "coverage_target_regressed"
            for item in peer_result["failures"]
        ):
            raise ValueError("peer has a coverage failure outside native IO")
        reports.append(report)
    result = evaluate_coverage(merge_reports(*reports), load_coverage_policy(policy))
    return {
        "schema_name": "comsol_mcp.paired_coverage_receipt",
        "source_tree_sha256": expected_source,
        "coverage_policy_sha256": policy_hash,
        "status": result["status"],
        "coverage": result,
        "solver_started": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--windows", type=Path, required=True)
    parser.add_argument("--linux", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    result = verify_pair(
        [args.windows, args.linux],
        root / "development_kit/release/coverage_policy.json",
        source_identity(root),
    )
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
