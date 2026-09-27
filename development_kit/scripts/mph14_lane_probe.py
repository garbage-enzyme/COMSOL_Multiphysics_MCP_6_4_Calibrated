"""Run the solver-free adapter and surrogate suites on the isolated MPh 1.4 lane.

The declared support range is ``mph>=1.3.1,<1.4``, so 1.4.0 is not a supported
lane and this script does not change that. It exists because the 2026-09-25
licensed acceptance repair gate asks for the 1.4 lane to be exercised
separately, and doing that by hand invites two mistakes that this script avoids.

**Do not use a venv for this.** Measured 2026-09-26: a ``venv`` ``python.exe``
re-execs a real interpreter, so ``process_identity`` from the parent and from the
child disagree on the PID, and the durable-job tests fail with
``durable job is already bound to another worker``. That is an artifact of the
measurement environment, not of the lane. This script therefore overlays MPh
1.4.0 onto the *existing* interpreter with ``PYTHONPATH``, which keeps process
identity semantics intact.

Usage::

    python development_kit/scripts/mph14_lane_probe.py --output D:\\mcp_tests\\lane14\\report.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(ROOT))

SUITES = (
    "development_kit/tests/test_adapter_protocol.py",
    "development_kit/tests/test_adapter_migration_inventory.py",
    "development_kit/tests/test_surrogate_dnn_adapter.py",
    "development_kit/tests/test_surrogate_manifests.py",
    "development_kit/tests/test_surrogate_training_job.py",
    "development_kit/tests/test_robust_adapter_configuration.py",
    "development_kit/tests/test_robust_shape_worker.py",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--overlay",
        type=Path,
        required=True,
        help="directory holding an MPh 1.4.0 install (pip install --target)",
    )
    parser.add_argument("--basetemp", type=Path, default=Path("D:/mcp_tests/a75lane14"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    started = time.monotonic()
    import os

    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(args.overlay)

    version = subprocess.run(
        [sys.executable, "-c", "import mph; print(mph.__version__)"],
        capture_output=True,
        text=True,
        env=environment,
        cwd=ROOT,
    )
    report: dict = {
        "schema_name": "comsol_mcp.mph14_lane_probe",
        "schema_version": "1.0.0",
        "interpreter": sys.executable,
        "overlay": str(args.overlay),
        "mph_version_under_overlay": version.stdout.strip(),
        "suites": list(SUITES),
        "success": False,
    }
    if version.returncode != 0:
        report["error"] = f"overlay does not provide MPh: {version.stderr.strip()[-400:]}"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 1

    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(args.basetemp),
            *SUITES,
        ],
        capture_output=True,
        text=True,
        env=environment,
        cwd=ROOT,
    )
    tail = (done.stdout + done.stderr).strip().splitlines()
    report["pytest_returncode"] = done.returncode
    report["pytest_summary"] = tail[-1] if tail else ""
    report["pytest_failures"] = [line for line in tail if line.startswith(("FAILED", "ERROR"))]
    report["success"] = done.returncode == 0
    report["duration_seconds"] = round(time.monotonic() - started, 3)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
