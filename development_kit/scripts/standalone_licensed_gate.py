"""Run the explicit serial licensed acceptance for the standalone Windows launcher."""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from importlib import import_module
from pathlib import Path
from typing import Any, Callable

import psutil

ROOT = Path(__file__).parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

atomic_write_json = import_module("comsol_mcp.durable.io").atomic_write_json
process_control = import_module("comsol_mcp.jobs.process_control")
inspect_identity = process_control.inspect_identity
terminate_exact = process_control.terminate_exact
build_standalone_executable = import_module(
    "comsol_mcp.standalone.builder"
).build_standalone_executable
standalone_control = import_module("comsol_mcp.standalone.control")
launch_standalone_campaign = standalone_control.launch_standalone_campaign
read_standalone_results = standalone_control.read_standalone_results
request_standalone_pause = standalone_control.request_standalone_pause
read_campaign_status = import_module("comsol_mcp.standalone.inspection").read_campaign_status

CONFIRMATION = "RUN_REAL_COMSOL"
RECEIPT_NAME = "standalone-licensed-acceptance.json"
PROCESS_NAMES = frozenset(
    {
        "comsol-mcp-standalone",
        "comsol",
        "comsolbatch",
        "comsolcompile",
        "comsolmphserver",
        "java",
        "javaw",
    }
)


def _relevant_processes() -> list[dict[str, Any]]:
    processes: list[dict[str, Any]] = []
    for process in psutil.process_iter(("pid", "name", "create_time")):
        try:
            name = str(process.info.get("name") or "").removesuffix(".exe").casefold()
            if name in PROCESS_NAMES:
                processes.append(
                    {
                        "pid": int(process.info["pid"]),
                        "name": name,
                        "process_create_time": float(process.info["create_time"]),
                    }
                )
        except psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess:
            continue
    return sorted(processes, key=lambda item: (item["name"], item["pid"]))


def _wait_for_status(
    output_directory: Path,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    deadline: float,
) -> dict[str, Any]:
    while time.monotonic() < deadline:
        try:
            status = read_campaign_status(output_directory)
        except FileNotFoundError:
            time.sleep(0.05)
            continue
        if predicate(status):
            return status
        time.sleep(0.05)
    raise TimeoutError("standalone licensed acceptance exceeded its deadline")


def run_acceptance(
    *,
    comsol_root: Path,
    output_directory: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Build, pause, resume, and validate one three-point COMSOL 6.4 campaign."""
    before = _relevant_processes()
    if before:
        raise RuntimeError("standalone acceptance requires a free COMSOL host")
    build = build_standalone_executable(output_directory)
    deadline = time.monotonic() + timeout_seconds
    first_launch = launch_standalone_campaign(output_directory, comsol_root)
    owner = first_launch["owner"]
    try:
        _wait_for_status(
            output_directory,
            lambda value: (
                value.get("status") == "running"
                and value.get("phase") == "solving"
                and value.get("completed") == 0
            ),
            deadline=deadline,
        )
        pause_request = request_standalone_pause(output_directory)
        paused = _wait_for_status(
            output_directory,
            lambda value: value.get("status") == "paused",
            deadline=deadline,
        )
        if paused.get("completed") != 1:
            raise RuntimeError("standalone pause was not acknowledged after one durable point")
        resumed = launch_standalone_campaign(output_directory, comsol_root, resume=True)
        owner = resumed["owner"]
        completed = _wait_for_status(
            output_directory,
            lambda value: value.get("status") == "completed",
            deadline=deadline,
        )
    except Exception:
        if inspect_identity(owner)["state"] == "active":
            terminate_exact(owner, force=True)
        raise

    results = read_standalone_results(output_directory, limit=128)
    rows = results["rows"]
    attempts = Counter(str(row["attempt_id"]) for row in rows)
    after = _relevant_processes()
    if after:
        raise RuntimeError("standalone acceptance left solver process residue")
    if len(rows) != 3 or sorted(attempts.values()) != [1, 2]:
        raise RuntimeError("standalone acceptance did not preserve exact resume rows")
    physical = completed.get("physical_summary")
    if not isinstance(physical, dict) or physical.get("status") != "passed":
        raise RuntimeError("standalone acceptance physical summary did not pass")
    receipt = {
        "schema_name": "comsol_mcp.standalone_licensed_acceptance",
        "schema_version": "1.0.0",
        "status": "passed",
        "target_os": ["Windows 10 x64", "Windows 11 x64"],
        "comsol_version": rows[0]["comsol_version"],
        "launcher_sha256": build["launcher"]["sha256"],
        "results_sha256": results["results_sha256"],
        "point_count": len(rows),
        "attempt_count": len(attempts),
        "attempt_row_counts": sorted(attempts.values()),
        "pause_completed": paused["completed"],
        "pause_request_id": pause_request["request_id"],
        "physical_summary": physical,
        "terminal_status": results["terminal"]["status"],
        "process_residue_count": len(after),
        "python_required_at_target": False,
        "local_licensed_comsol_required": True,
        "comsol_root_included": False,
        "paths_included": False,
    }
    atomic_write_json(output_directory / RECEIPT_NAME, receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--comsol-root", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    args = parser.parse_args()
    if args.confirm != CONFIRMATION:
        raise SystemExit(f"--confirm must be exactly {CONFIRMATION}")
    if not 30.0 <= args.timeout_seconds <= 900.0:
        raise SystemExit("--timeout-seconds must be from 30 through 900")
    run_acceptance(
        comsol_root=args.comsol_root,
        output_directory=args.output_directory,
        timeout_seconds=args.timeout_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
