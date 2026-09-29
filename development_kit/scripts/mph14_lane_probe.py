"""Run the solver-free adapter and surrogate suites on the isolated MPh 1.4 lane.

The declared support range is ``mph>=1.3.1,<1.4``, so 1.4.0 is not a supported
lane and this script does not change that. It exists because the 2026-09-25
licensed acceptance repair gate asks for the 1.4 lane to be exercised
separately, and doing that by hand invites two mistakes that this script avoids.

The overlay keeps the selected interpreter unchanged. A venv launcher can have
its own parent/worker process identities; that is a separate environment
compatibility question, not evidence that a project ownership failure is safe
to ignore. The lane is enforced before pytest and again inside its interpreter.

Usage::

    python development_kit/scripts/mph14_lane_probe.py --output D:\\mcp_tests\\lane14\\report.json
"""

from __future__ import annotations

import argparse
import hashlib
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
        [
            sys.executable,
            "-c",
            "import json, mph, sys; "
            "print(json.dumps(dict(version=mph.__version__, module=mph.__file__, "
            "interpreter=sys.executable)))",
        ],
        capture_output=True,
        text=True,
        env=environment,
        cwd=ROOT,
        timeout=30,
    )
    try:
        identity = json.loads(version.stdout) if version.returncode == 0 else {}
    except ValueError, TypeError:
        identity = {}
    if not isinstance(identity, dict):
        identity = {}
    report: dict = {
        "schema_name": "comsol_mcp.mph14_lane_probe",
        "schema_version": "1.0.0",
        "interpreter": sys.executable,
        "overlay": str(args.overlay),
        "mph_version_under_overlay": identity.get("version"),
        "lane_identity": identity,
        "suites": list(SUITES),
        "success": False,
    }
    module = Path(str(identity.get("module") or "")).resolve()
    if (
        version.returncode != 0
        or identity.get("version") != "1.4.0"
        or not args.overlay.is_dir()
        or not module.is_relative_to(args.overlay.resolve())
        or not module.is_file()
        or Path(str(identity.get("interpreter") or "")).resolve() != Path(sys.executable).resolve()
    ):
        report["error"] = "MPh 1.4.0 must be imported from the selected overlay by this interpreter"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 1
    report["mph_module_sha256"] = hashlib.sha256(module.read_bytes()).hexdigest()
    report["source_sha"] = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()

    report["driver_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report["suite_sha256"] = {
        p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in SUITES
    }
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "import mph, os, pathlib, pytest, sys; "
            "assert mph.__version__ == '1.4.0'; "
            "assert pathlib.Path(mph.__file__).resolve().is_relative_to("
            "pathlib.Path(os.environ['PYTHONPATH']).resolve()); "
            "sys.exit(pytest.main(sys.argv[1:]))",
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
        timeout=600,
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
