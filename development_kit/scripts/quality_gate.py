"""Run the ratcheted lint, type, test, coverage, license, and budget gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

if __package__:
    from .dependency_license_gate import build_license_receipt
else:
    from dependency_license_gate import build_license_receipt  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "development_kit" / "release" / "coverage_policy.json"
LICENSE_REVIEW_PATH = ROOT / "development_kit" / "release" / "dependency_license_review.json"
LINT_TARGETS = (
    "comsol_mcp/compatibility.py",
    "comsol_mcp/contracts",
    "comsol_mcp/durable",
    "comsol_mcp/schema_registry.py",
    "comsol_mcp/tools/catalog.py",
    "comsol_mcp/tools/session_status.py",
    "src/__init__.py",
    "development_kit/scripts/dependency_license_gate.py",
    "development_kit/scripts/quality_gate.py",
    "development_kit/tests/conftest.py",
    "development_kit/tests/test_control_plane_startup.py",
    "development_kit/tests/test_dependency_license_gate.py",
    "development_kit/tests/test_durable_primitives.py",
    "development_kit/tests/test_namespace_compatibility.py",
    "development_kit/tests/test_public_input_contracts.py",
    "development_kit/tests/test_quality_gate.py",
    "development_kit/tests/test_quality_properties.py",
    "development_kit/tests/test_schema_registry.py",
    "development_kit/tests/test_tool_catalog.py",
)
MYPY_GROUPS = (
    (
        "comsol_mcp/contracts/job_submission.py",
        "comsol_mcp/contracts/structural.py",
    ),
    (
        "comsol_mcp/durable/canonical.py",
        "comsol_mcp/durable/io.py",
    ),
    (
        "--follow-imports=skip",
        "comsol_mcp/tools/catalog.py",
        "comsol_mcp/schema_registry.py",
        "comsol_mcp/compatibility.py",
        "comsol_mcp/tools/session_status.py",
        "src/__init__.py",
    ),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes(), usedforsecurity=False).hexdigest()


def _default_artifact_root() -> Path:
    configured = os.environ.get("COMSOL_MCP_QUALITY_ROOT") or os.environ.get("RUNNER_TEMP")
    if configured:
        return Path(configured) / "comsol_mcp_quality"
    if os.name == "nt" and Path("D:/").exists():
        return Path("D:/comsol_runtime/quality_gate")
    return Path(tempfile.gettempdir()) / "comsol_mcp_quality"


def _run(arguments: list[str], *, environment: dict[str, str] | None = None) -> None:
    subprocess.run(  # noqa: S603
        arguments,
        cwd=ROOT,
        env=environment,
        check=True,
    )


def load_coverage_policy(path: str | Path) -> dict[str, Any]:
    """Load one exact coverage floor and per-file safety target policy."""
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {
        "schema_name",
        "schema_version",
        "global",
        "targets",
    }:
        raise ValueError("coverage policy fields are invalid")
    if value["schema_name"] != "comsol_mcp.coverage_policy" or value["schema_version"] != "1.0.0":
        raise ValueError("coverage policy schema is unsupported")
    if not isinstance(value["global"], dict) or set(value["global"]) != {
        "minimum_percent_covered",
        "owner",
        "rationale",
        "removal_gate",
    }:
        raise ValueError("global coverage policy is invalid")
    targets = value["targets"]
    if not isinstance(targets, list) or not targets:
        raise ValueError("coverage targets are missing")
    paths = []
    for item in [value["global"], *targets]:
        if not isinstance(item, dict):
            raise ValueError("coverage policy item is invalid")
        threshold = item.get("minimum_percent_covered")
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not 0.0 <= float(threshold) <= 100.0
            or not isinstance(item.get("owner"), str)
            or not item["owner"].strip()
            or not isinstance(item.get("rationale"), str)
            or not item["rationale"].strip()
            or not isinstance(item.get("removal_gate"), str)
            or not item["removal_gate"].strip()
        ):
            raise ValueError("coverage policy item values are invalid")
        if item is value["global"]:
            continue
        if set(item) != {
            "path",
            "minimum_percent_covered",
            "owner",
            "rationale",
            "removal_gate",
        }:
            raise ValueError("coverage target fields are invalid")
        path_text = item["path"]
        if (
            not isinstance(path_text, str)
            or not path_text.startswith("comsol_mcp/")
            or "\\" in path_text
            or ".." in Path(path_text).parts
        ):
            raise ValueError("coverage target path is invalid")
        paths.append(path_text)
    if len(paths) != len(set(paths)):
        raise ValueError("coverage target paths must be unique")
    return value


def evaluate_coverage(
    report: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate branch-aware global and safety-file coverage floors."""
    totals = report.get("totals") if isinstance(report, dict) else None
    files = report.get("files") if isinstance(report, dict) else None
    if not isinstance(totals, dict) or not isinstance(files, dict):
        raise ValueError("coverage report is invalid")
    global_percent = totals.get("percent_covered")
    if not isinstance(global_percent, (int, float)):
        raise ValueError("global coverage percentage is missing")
    normalized_files = {path.replace("\\", "/"): value for path, value in files.items()}
    failures = []
    global_minimum = float(policy["global"]["minimum_percent_covered"])
    if float(global_percent) < global_minimum:
        failures.append(
            {
                "reason_code": "global_coverage_regressed",
                "observed": float(global_percent),
                "minimum": global_minimum,
            }
        )
    target_receipts = []
    for target in policy["targets"]:
        path = target["path"]
        file_record = normalized_files.get(path)
        summary = file_record.get("summary") if isinstance(file_record, dict) else None
        observed = summary.get("percent_covered") if isinstance(summary, dict) else None
        minimum = float(target["minimum_percent_covered"])
        if not isinstance(observed, (int, float)):
            failures.append({"path": path, "reason_code": "coverage_target_missing"})
        elif float(observed) < minimum:
            failures.append(
                {
                    "path": path,
                    "reason_code": "coverage_target_regressed",
                    "observed": float(observed),
                    "minimum": minimum,
                }
            )
        target_receipts.append(
            {
                "path": path,
                "observed": observed,
                "minimum": minimum,
                "owner": target["owner"],
                "rationale": target["rationale"],
                "removal_gate": target["removal_gate"],
            }
        )
    return {
        "status": "passed" if not failures else "failed",
        "global": {
            "observed": float(global_percent),
            "minimum": global_minimum,
            "covered_lines": totals.get("covered_lines"),
            "num_statements": totals.get("num_statements"),
            "covered_branches": totals.get("covered_branches"),
            "num_branches": totals.get("num_branches"),
        },
        "targets": target_receipts,
        "failures": failures,
    }


def run_quality_gate(artifact_root: Path, *, as_of: date) -> dict[str, Any]:
    """Run every quality command and return one path-free receipt."""
    if os.name == "nt" and not str(artifact_root).isascii():
        raise ValueError("quality artifact root must be ASCII on Windows")
    artifact_root.mkdir(parents=True, exist_ok=True)
    coverage_data = artifact_root / ".coverage"
    coverage_json = artifact_root / "coverage.json"
    environment = dict(os.environ)
    environment["COVERAGE_FILE"] = str(coverage_data)

    _run([sys.executable, "-m", "ruff", "check", *LINT_TARGETS])
    _run([sys.executable, "-m", "ruff", "format", "--check", *LINT_TARGETS])
    for group in MYPY_GROUPS:
        _run(
            [
                sys.executable,
                "-m",
                "mypy",
                "--strict",
                "--ignore-missing-imports",
                "--no-error-summary",
                *group,
            ]
        )
    _run([sys.executable, "-m", "coverage", "erase"], environment=environment)
    _run(
        [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--branch",
            "--source=comsol_mcp",
            "-m",
            "pytest",
            "-q",
        ],
        environment=environment,
    )
    _run(
        [sys.executable, "-m", "coverage", "json", "-o", str(coverage_json)],
        environment=environment,
    )

    policy = load_coverage_policy(POLICY_PATH)
    coverage_receipt = evaluate_coverage(
        json.loads(coverage_json.read_text(encoding="utf-8")),
        policy,
    )
    license_receipt = build_license_receipt(
        ROOT / "pyproject.toml",
        LICENSE_REVIEW_PATH,
        as_of=as_of,
    )
    failures = []
    if coverage_receipt["status"] != "passed":
        failures.append("coverage")
    if license_receipt["status"] != "passed":
        failures.append("dependency_licenses")
    return {
        "schema_name": "comsol_mcp.quality_gate_receipt",
        "schema_version": "1.0.0",
        "as_of": as_of.isoformat(),
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "coverage": coverage_receipt,
        "dependency_licenses": license_receipt,
        "coverage_policy_sha256": _sha256(POLICY_PATH),
        "solver_started": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", type=Path, default=_default_artifact_root())
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()

    receipt = run_quality_gate(args.artifact_root, as_of=args.as_of)
    output = args.artifact_root / "quality-receipt.json"
    output.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
