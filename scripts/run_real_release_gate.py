"""Explicit serial real-COMSOL release gate for a licensed pinned host."""

from __future__ import annotations

import argparse
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

import psutil

from src.evidence.real_fixture import controlled_fixture_environment_from_h1_spec
from src.tools.ownership import SolverOwnership


ROOT = Path(__file__).resolve().parents[1]

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


def run_release_gate(
    args,
    *,
    command_runner=subprocess.run,
    owner=None,
    pid_provider=_comsol_pids,
    wait_clean=_wait_clean_ownership,
) -> dict:
    """Run optional mandatory-H1 mode followed by the existing serial suite."""
    owner = owner or SolverOwnership()
    before_status = wait_clean(owner)
    before_pids = pid_provider()
    if before_status["collision"] or before_status["lease"]["state"] != "absent":
        raise RuntimeError("real release gate requires no external solver and no lease")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    h1_completed = None
    h1_receipt = None
    h1_timed_out = False
    h1_receipt_path = args.output.with_name(f"{args.output.stem}.h1.json")
    h1_passed = not args.require_h1
    if args.require_h1:
        if args.h1_spec is None or args.h1_cores is None or args.h1_timeout_seconds is None:
            raise ValueError("--require-h1 requires --h1-spec, --h1-cores, and --h1-timeout-seconds")
        if not 1 <= int(args.h1_cores) <= 64:
            raise ValueError("--h1-cores must be in 1..64")
        if not 1.0 <= float(args.h1_timeout_seconds) <= 7200.0:
            raise ValueError("--h1-timeout-seconds must be in 1..7200")
        if args.output.exists() or h1_receipt_path.exists():
            raise ValueError("H1 release receipts must use new output paths")
        h1_command = [
            sys.executable,
            str(ROOT / "tests" / "integration" / "h1_real_physical_evidence.py"),
            "--confirm", "RUN_REAL_COMSOL",
            "--spec", str(args.h1_spec),
            "--output", str(h1_receipt_path),
            "--cores", str(args.h1_cores),
            "--timeout-seconds", str(args.h1_timeout_seconds),
        ]
        try:
            h1_completed = command_runner(
                h1_command,
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=float(args.h1_timeout_seconds) + 90.0,
            )
        except subprocess.TimeoutExpired as exc:
            h1_timed_out = True
            h1_completed = subprocess.CompletedProcess(
                h1_command,
                124,
                exc.stdout or "",
                exc.stderr or "",
            )
        if h1_receipt_path.is_file():
            h1_receipt = json.loads(h1_receipt_path.read_text(encoding="utf-8"))
        h1_passed = (
            h1_completed.returncode == 0
            and isinstance(h1_receipt, dict)
            and h1_receipt.get("success") is True
            and h1_receipt.get("cleanup", {}).get("passed") is True
        )

    suite_completed = None
    if h1_passed:
        fixture_environment = (
            controlled_fixture_environment_from_h1_spec(args.h1_spec)
            if args.require_h1
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
                "tests/integration/test_real_comsol.py",
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
    overall = h1_passed and suite_passed and cleanup_passed
    return {
        "schema_version": "1.1.0",
        "gate": "serial_real_comsol_release",
        "require_h1": bool(args.require_h1),
        "returncode": 0 if overall else 1,
        "phases": {
            "h1": {
                **_completed_summary(h1_completed),
                "required": bool(args.require_h1),
                "passed": h1_passed,
                "timed_out": h1_timed_out,
                "receipt_path": h1_receipt_path.name if args.require_h1 else None,
                "receipt_sha256": (
                    __import__("hashlib").sha256(h1_receipt_path.read_bytes()).hexdigest()
                    if h1_receipt_path.is_file() else None
                ),
            },
            "licensed_regression": {
                **_completed_summary(suite_completed),
                "test_target": "tests/integration/test_real_comsol.py",
                "passed": suite_passed,
                "skipped_reason": None if suite_completed is not None else "H1 did not pass",
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
            "comsol_build": "must_match_release/support_matrix.json and probe evidence",
            "java": "must_match_release/support_matrix.json and probe evidence",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", required=True, choices=["RUN_REAL_COMSOL"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-h1", action="store_true")
    parser.add_argument("--h1-spec", type=Path)
    parser.add_argument("--h1-cores", type=int)
    parser.add_argument("--h1-timeout-seconds", type=float)
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
