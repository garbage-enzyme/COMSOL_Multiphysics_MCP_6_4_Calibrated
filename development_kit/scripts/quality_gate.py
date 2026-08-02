"""Run the ratcheted lint, type, test, coverage, license, and budget gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import uuid
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
    "comsol_mcp/contracts/simulation_configuration.py",
    "comsol_mcp/contracts/thermal_radiation.py",
    "comsol_mcp/contracts/thermal_material.py",
    "comsol_mcp/contracts/thermo_optomechanical.py",
    "comsol_mcp/evidence/simulation_configuration.py",
    "comsol_mcp/evidence/thermal_radiation.py",
    "comsol_mcp/evidence/thermal_material.py",
    "comsol_mcp/evidence/spectral_model_comparison.py",
    "comsol_mcp/native_runtime.py",
    "comsol_mcp/schema_registry.py",
    "comsol_mcp/jobs/thermo_optomechanical_replay.py",
    "comsol_mcp/jobs/thermo_optomechanical_replay_execution.py",
    "comsol_mcp/jobs/thermo_optomechanical_replay_rows.py",
    "comsol_mcp/jobs/thermo_optomechanical_replay_runner.py",
    "comsol_mcp/jobs/thermo_optomechanical_replay_worker.py",
    "comsol_mcp/standalone",
    "comsol_mcp/tools/acoustics_pde.py",
    "comsol_mcp/tools/catalog.py",
    "comsol_mcp/tools/configuration.py",
    "comsol_mcp/tools/thermal_radiation.py",
    "comsol_mcp/tools/thermal_material.py",
    "comsol_mcp/tools/geometry_selections.py",
    "comsol_mcp/tools/session_status.py",
    "comsol_mcp/tools/standalone.py",
    "src/__init__.py",
    "development_kit/scripts/dependency_license_gate.py",
    "development_kit/scripts/quality_gate.py",
    "development_kit/scripts/standalone_licensed_gate.py",
    "development_kit/tests/conftest.py",
    "development_kit/tests/test_control_plane_startup.py",
    "development_kit/tests/test_dependency_license_gate.py",
    "development_kit/tests/test_durable_primitives.py",
    "development_kit/tests/test_namespace_compatibility.py",
    "development_kit/tests/test_native_runtime.py",
    "development_kit/tests/test_public_input_contracts.py",
    "development_kit/tests/test_quality_gate.py",
    "development_kit/tests/test_quality_properties.py",
    "development_kit/tests/test_schema_registry.py",
    "development_kit/tests/test_standalone_acceptance_runner.py",
    "development_kit/tests/test_standalone_executable.py",
    "development_kit/tests/test_standalone_tools.py",
    "development_kit/tests/test_tool_catalog.py",
)
MYPY_GROUPS = (
    (
        "comsol_mcp/contracts/job_submission.py",
        "comsol_mcp/contracts/simulation_configuration.py",
        "comsol_mcp/contracts/thermal_radiation.py",
        "comsol_mcp/contracts/thermal_material.py",
        "comsol_mcp/contracts/thermo_optomechanical.py",
        "comsol_mcp/contracts/structural.py",
    ),
    (
        "comsol_mcp/durable/canonical.py",
        "comsol_mcp/durable/io.py",
    ),
    (
        "--follow-imports=skip",
        "comsol_mcp/evidence/spectral_model_comparison.py",
        "comsol_mcp/evidence/simulation_configuration.py",
        "comsol_mcp/evidence/thermal_radiation.py",
        "comsol_mcp/evidence/thermal_material.py",
        "comsol_mcp/jobs/thermo_optomechanical_replay.py",
        "comsol_mcp/jobs/thermo_optomechanical_replay_execution.py",
        "comsol_mcp/jobs/thermo_optomechanical_replay_rows.py",
        "comsol_mcp/jobs/thermo_optomechanical_replay_runner.py",
        "comsol_mcp/jobs/thermo_optomechanical_replay_worker.py",
        "comsol_mcp/tools/acoustics_pde.py",
        "comsol_mcp/tools/catalog.py",
        "comsol_mcp/tools/configuration.py",
        "comsol_mcp/tools/thermal_radiation.py",
        "comsol_mcp/tools/thermal_material.py",
        "comsol_mcp/tools/geometry_selections.py",
        "comsol_mcp/schema_registry.py",
        "comsol_mcp/compatibility.py",
        "comsol_mcp/tools/session_status.py",
        "comsol_mcp/native_runtime.py",
        "comsol_mcp/standalone",
        "src/__init__.py",
    ),
)
PRODUCTION_ROOTS = ("comsol_mcp", "src")
LINT_EXCLUSIONS_SHA256 = "c3306e6115cbe533a5317054ce1af85cab33bff2147ec1b7feff990cf4ed9fe8"
MYPY_EXCLUSIONS_SHA256 = "8d5b4a970ff2235f0dd3ba3bafc1827d8e6b27975127bac9c221fcf07ae2f7db"
PARALLEL_TEST_WORKERS = 4
SERIAL_TEST_TARGETS = ("development_kit/tests/test_control_plane_startup.py",)


class QualityCommandError(RuntimeError):
    """One named quality command failed without exposing its command line."""

    def __init__(self, stage: str, returncode: int) -> None:
        super().__init__(stage)
        self.stage = stage
        self.returncode = returncode


def _targeted_production_modules(targets: tuple[str, ...], modules: set[str]) -> set[str]:
    configured = [target for target in targets if target.split("/", 1)[0] in PRODUCTION_ROOTS]
    return {
        module
        for module in modules
        if any(
            module == target or module.startswith(target.rstrip("/") + "/") for target in configured
        )
    }


def _exclusion_digest(modules: set[str]) -> str:
    payload = json.dumps(sorted(modules), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload, usedforsecurity=False).hexdigest()


def validate_quality_target_inventory(root: Path = ROOT) -> dict[str, Any]:
    """Fail when a production module bypasses the frozen lint/type classification."""
    modules = {
        path.relative_to(root).as_posix()
        for production_root in PRODUCTION_ROOTS
        for path in (root / production_root).rglob("*.py")
    }
    mypy_targets = tuple(
        target for group in MYPY_GROUPS for target in group if not target.startswith("-")
    )
    lint_exclusions = modules - _targeted_production_modules(LINT_TARGETS, modules)
    mypy_exclusions = modules - _targeted_production_modules(mypy_targets, modules)
    observed = {
        "lint": _exclusion_digest(lint_exclusions),
        "typing": _exclusion_digest(mypy_exclusions),
    }
    expected = {
        "lint": LINT_EXCLUSIONS_SHA256,
        "typing": MYPY_EXCLUSIONS_SHA256,
    }
    if observed != expected:
        raise ValueError("production quality-target classification changed")
    return {
        "production_module_count": len(modules),
        "lint_targeted_count": len(modules - lint_exclusions),
        "typing_targeted_count": len(modules - mypy_exclusions),
        "exclusion_digests": observed,
    }


def _main_pytest_command(pytest_root: Path, *, hosted_ci: bool) -> list[str]:
    """Build the main-suite command, keeping hosted Python 3.14 execution serial."""
    command = [sys.executable, "-m", "pytest", "-q"]
    if not hosted_ci:
        command.extend(["-n", str(PARALLEL_TEST_WORKERS), "--dist", "loadscope"])
    command.extend(
        [
            "--basetemp",
            str(pytest_root / "main"),
            *(argument for target in SERIAL_TEST_TARGETS for argument in ("--ignore", target)),
            "--cov=comsol_mcp",
            "--cov-branch",
            "--cov-report=",
        ]
    )
    return command


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes(), usedforsecurity=False).hexdigest()


def _default_artifact_root() -> Path:
    configured = os.environ.get("COMSOL_MCP_QUALITY_ROOT") or os.environ.get("RUNNER_TEMP")
    if configured:
        return Path(configured) / "comsol_mcp_quality"
    if os.name == "nt" and Path("D:/").exists():
        return Path("D:/comsol_runtime/quality_gate")
    return Path(tempfile.gettempdir()) / "comsol_mcp_quality"


def _run(
    arguments: list[str],
    *,
    stage: str,
    environment: dict[str, str] | None = None,
) -> None:
    try:
        subprocess.run(  # noqa: S603
            arguments,
            cwd=ROOT,
            env=environment,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise QualityCommandError(stage, exc.returncode) from None


def _create_quality_run_root(artifact_root: Path) -> Path:
    """Create one collision-free evidence directory below a caller-owned root."""
    if os.name == "nt" and not str(artifact_root).isascii():
        raise ValueError("quality artifact root must be ASCII on Windows")
    artifact_root.mkdir(parents=True, exist_ok=True)
    for _ in range(8):
        candidate = artifact_root / f"run-{os.getpid()}-{uuid.uuid4().hex}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError("could not allocate a unique quality evidence directory")


def _create_short_pytest_root() -> Path:
    """Allocate an isolated short ASCII root for deeply nested Windows fixtures."""
    candidates = []
    configured = os.environ.get("COMSOL_MCP_TEST_ASCII_ROOT")
    if configured:
        candidates.append(Path(configured))
    if os.name == "nt" and Path("D:/").exists():
        candidates.append(Path("D:/comsol_pytest"))
    candidates.append(Path(tempfile.gettempdir()))
    for parent in candidates:
        if os.name == "nt" and not str(parent).isascii():
            continue
        try:
            parent.mkdir(parents=True, exist_ok=True)
            candidate = parent / f"q-{os.getpid()}-{uuid.uuid4().hex[:8]}"
            candidate.mkdir()
        except OSError:
            continue
        return candidate
    raise OSError("no short ASCII pytest root is available")


def _write_quality_receipt(run_root: Path, receipt: dict[str, Any]) -> None:
    output = run_root / "quality-receipt.json"
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(receipt, handle, indent=2, sort_keys=True)
        handle.write("\n")


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
            or not math.isfinite(float(threshold))
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
    if (
        isinstance(global_percent, bool)
        or not isinstance(global_percent, (int, float))
        or not math.isfinite(float(global_percent))
    ):
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
        if (
            isinstance(observed, bool)
            or not isinstance(observed, (int, float))
            or not math.isfinite(float(observed))
        ):
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
    run_root = _create_quality_run_root(artifact_root)
    coverage_data = run_root / ".coverage"
    coverage_json = run_root / "coverage.json"
    pytest_root = _create_short_pytest_root()
    environment = dict(os.environ)
    environment["COVERAGE_FILE"] = str(coverage_data)
    run_id = run_root.name

    try:
        validate_quality_target_inventory()
        _run([sys.executable, "-m", "ruff", "check", *LINT_TARGETS], stage="lint")
        _run(
            [sys.executable, "-m", "ruff", "format", "--check", *LINT_TARGETS],
            stage="format",
        )
        for index, group in enumerate(MYPY_GROUPS, start=1):
            _run(
                [
                    sys.executable,
                    "-m",
                    "mypy",
                    "--strict",
                    "--ignore-missing-imports",
                    "--no-error-summary",
                    *group,
                ],
                stage=f"typing_{index}",
            )
        _run(
            [sys.executable, "-m", "coverage", "erase"],
            stage="coverage_erase",
            environment=environment,
        )
        _run(
            _main_pytest_command(
                pytest_root,
                hosted_ci=os.environ.get("GITHUB_ACTIONS", "").casefold() == "true",
            ),
            stage="parallel_tests",
            environment=environment,
        )
        _run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                *SERIAL_TEST_TARGETS,
                "--basetemp",
                str(pytest_root / "serial"),
            ],
            stage="serial_tests",
            environment=environment,
        )
        _run(
            [sys.executable, "-m", "coverage", "json", "-o", str(coverage_json)],
            stage="coverage_report",
            environment=environment,
        )
    except QualityCommandError as exc:
        receipt = {
            "schema_name": "comsol_mcp.quality_gate_receipt",
            "schema_version": "1.0.0",
            "as_of": as_of.isoformat(),
            "run_id": run_id,
            "status": "failed",
            "failures": ["command"],
            "command_failure": {
                "stage": exc.stage,
                "returncode": exc.returncode,
            },
            "coverage": None,
            "dependency_licenses": None,
            "coverage_policy_sha256": _sha256(POLICY_PATH),
            "solver_started": False,
        }
        _write_quality_receipt(run_root, receipt)
        return receipt

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
    receipt = {
        "schema_name": "comsol_mcp.quality_gate_receipt",
        "schema_version": "1.0.0",
        "as_of": as_of.isoformat(),
        "run_id": run_id,
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "coverage": coverage_receipt,
        "dependency_licenses": license_receipt,
        "coverage_policy_sha256": _sha256(POLICY_PATH),
        "solver_started": False,
    }
    _write_quality_receipt(run_root, receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", type=Path, default=_default_artifact_root())
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()

    receipt = run_quality_gate(args.artifact_root, as_of=args.as_of)
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
