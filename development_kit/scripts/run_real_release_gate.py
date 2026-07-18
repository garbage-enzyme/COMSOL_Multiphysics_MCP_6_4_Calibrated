"""Explicit serial real-COMSOL release gate for a licensed pinned host."""

from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

import psutil

from comsol_mcp.evidence.real_fixture import controlled_fixture_environment_from_reference_power_spec
from comsol_mcp.tools.ownership import SolverOwnership


ROOT = Path(__file__).resolve().parents[2]

def _comsol_pids() -> set[int]:
    names = {"comsol", "comsolmphserver", "comsolbatch"}
    found: set[int] = set()
    for process in psutil.process_iter(["pid", "name"]):
        try:
            if (process.info.get("name") or "").lower().split(".")[0] in names:
                found.add(int(process.info["pid"]))
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return found


def _wait_clean_ownership(
    owner: SolverOwnership,
    *,
    timeout_seconds: float = 30.0,
    poll_seconds: float = 0.25,
    clock=time.monotonic,
    sleeper=time.sleep,
) -> dict:
    """Wait for fresh, complete, collision-free cleanup without using stale evidence."""
    deadline = clock() + timeout_seconds
    latest = owner.status()
    while True:
        inventory = latest.get("process_inventory") or {}
        if (
            inventory.get("complete") is True
            and inventory.get("fresh") is True
            and latest.get("collision") is False
            and latest.get("lease", {}).get("state") == "absent"
        ):
            return latest
        if clock() >= deadline:
            return latest
        sleeper(poll_seconds)
        latest = owner.status()


def _completed_summary(completed: subprocess.CompletedProcess | None) -> dict:
    if completed is None:
        return {"started": False, "returncode": None, "stdout_tail": "", "stderr_tail": ""}
    return {
        "started": True,
        "returncode": completed.returncode,
        "stdout_tail": (completed.stdout or "")[-4000:],
        "stderr_tail": (completed.stderr or "")[-4000:],
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_release_gate(
    args,
    *,
    command_runner=subprocess.run,
    owner=None,
    pid_provider=_comsol_pids,
    wait_clean=_wait_clean_ownership,
) -> dict:
    """Run optional mandatory-reference-power mode followed by the existing serial suite."""
    owner = owner or SolverOwnership()
    before_status = wait_clean(owner)
    before_pids = pid_provider()
    if before_status["collision"] or before_status["lease"]["state"] != "absent":
        raise RuntimeError("real release gate requires no external solver and no lease")

    require_reference_power = bool(
        getattr(args, "require_reference_power", getattr(args, "require_h1", False))
    )
    reference_power_spec = getattr(
        args, "reference_power_spec", getattr(args, "h1_spec", None)
    )
    reference_power_cores = getattr(
        args, "reference_power_cores", getattr(args, "h1_cores", None)
    )
    reference_power_timeout_seconds = getattr(
        args,
        "reference_power_timeout_seconds",
        getattr(args, "h1_timeout_seconds", None),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    reference_power_completed = None
    reference_power_receipt = None
    reference_power_timed_out = False
    reference_power_receipt_path = args.output.with_name(f"{args.output.stem}.reference_power.json")
    reference_power_passed = not require_reference_power
    if require_reference_power:
        if reference_power_spec is None or reference_power_cores is None or reference_power_timeout_seconds is None:
            raise ValueError(
                "--require-reference-power requires --reference-power-spec, "
                "--reference-power-cores, and --reference-power-timeout-seconds"
            )
        if not 1 <= int(reference_power_cores) <= 64:
            raise ValueError("--reference-power-cores must be in 1..64")
        if not 1.0 <= float(reference_power_timeout_seconds) <= 7200.0:
            raise ValueError("--reference-power-timeout-seconds must be in 1..7200")
        if args.output.exists() or reference_power_receipt_path.exists():
            raise ValueError("reference-power release receipts must use new output paths")
        reference_power_command = [
            sys.executable,
            str(
                ROOT
                / "development_kit"
                / "tests"
                / "integration"
                / "reference_power_acceptance.py"
            ),
            "--confirm", "RUN_REAL_COMSOL",
            "--spec", str(reference_power_spec),
            "--output", str(reference_power_receipt_path),
            "--cores", str(reference_power_cores),
            "--timeout-seconds", str(reference_power_timeout_seconds),
        ]
        try:
            reference_power_completed = command_runner(
                reference_power_command,
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=float(reference_power_timeout_seconds) + 90.0,
            )
        except subprocess.TimeoutExpired as exc:
            reference_power_timed_out = True
            reference_power_completed = subprocess.CompletedProcess(
                reference_power_command,
                124,
                exc.stdout or "",
                exc.stderr or "",
            )
        if reference_power_receipt_path.is_file():
            reference_power_receipt = json.loads(reference_power_receipt_path.read_text(encoding="utf-8"))
        reference_power_passed = (
            reference_power_completed.returncode == 0
            and isinstance(reference_power_receipt, dict)
            and reference_power_receipt.get("success") is True
            and reference_power_receipt.get("cleanup", {}).get("passed") is True
        )

    suite_completed = None
    if reference_power_passed:
        fixture_spec = getattr(args, "fixture_spec", None)
        if fixture_spec is None and require_reference_power:
            fixture_spec = reference_power_spec
        fixture_environment = (
            controlled_fixture_environment_from_reference_power_spec(fixture_spec)
            if fixture_spec is not None
            else os.environ.copy()
        )
        suite_completed = command_runner(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-m",
                "integration",
                "development_kit/tests/integration/test_real_comsol.py",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            env=fixture_environment,
        )

    after_status = wait_clean(owner)
    after_pids = pid_provider()
    cleanup_passed = (
        after_pids == before_pids
        and after_status["lease"]["state"] == "absent"
        and not after_status["collision"]
    )
    suite_passed = suite_completed is not None and suite_completed.returncode == 0
    overall = reference_power_passed and suite_passed and cleanup_passed
    reference_power_phase = {
        **_completed_summary(reference_power_completed),
        "required": require_reference_power,
        "passed": reference_power_passed,
        "timed_out": reference_power_timed_out,
        "receipt_path": reference_power_receipt_path.name if require_reference_power else None,
        "receipt_sha256": (
            _sha256_file(reference_power_receipt_path)
            if reference_power_receipt_path.is_file() else None
        ),
    }
    return {
        "schema_version": "1.2.0",
        "gate": "serial_real_comsol_release",
        "require_reference_power": require_reference_power,
        "require_h1": require_reference_power,
        "returncode": 0 if overall else 1,
        "phases": {
            "reference_power": reference_power_phase,
            "h1": reference_power_phase,
            "licensed_regression": {
                **_completed_summary(suite_completed),
                "test_target": "development_kit/tests/integration/test_real_comsol.py",
                "passed": suite_passed,
                "skipped_reason": None if suite_completed is not None else "reference-power did not pass",
                "fixture_spec_sha256": (
                    _sha256_file(Path(fixture_spec))
                    if suite_completed is not None and fixture_spec is not None
                    else None
                ),
            },
        },
        "cleanup": {
            "comsol_pid_set_unchanged": after_pids == before_pids,
            "lease_absent": after_status["lease"]["state"] == "absent",
            "collision_absent": not after_status["collision"],
            "passed": cleanup_passed,
        },
        "environment": {
            "python": platform.python_version(),
            "mph": version("mph"),
            "mcp": version("mcp"),
            "comsol_build": "must match development_kit/release/support_matrix.json and probe evidence",
            "java": "must match development_kit/release/support_matrix.json and probe evidence",
        },
        "legacy_compatibility": {
            "cli_aliases": ["--require-h1", "--h1-spec", "--h1-cores", "--h1-timeout-seconds"],
            "receipt_aliases": ["require_h1", "phases.h1"],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", required=True, choices=["RUN_REAL_COMSOL"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-reference-power", action="store_true")
    parser.add_argument(
        "--require-h1",
        dest="require_reference_power",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--reference-power-spec", type=Path)
    parser.add_argument(
        "--h1-spec",
        dest="reference_power_spec",
        type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--fixture-spec", type=Path)
    parser.add_argument("--reference-power-cores", type=int)
    parser.add_argument(
        "--h1-cores",
        dest="reference_power_cores",
        type=int,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--reference-power-timeout-seconds", type=float)
    parser.add_argument(
        "--h1-timeout-seconds",
        dest="reference_power_timeout_seconds",
        type=float,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    started = time.monotonic()
    try:
        receipt = run_release_gate(args)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    receipt["duration_seconds"] = round(time.monotonic() - started, 3)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return int(receipt["returncode"])


if __name__ == "__main__":
    raise SystemExit(main())
