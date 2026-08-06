"""Manual real-COMSOL cancellation probe using an explicit local fixture."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable


ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT))

from src.evidence.outcome_contract import execution_from_terminal_job_state
from src.jobs.manager import JobManager


_TERMINAL_STATES = frozenset({"cancelled", "failed", "interrupted", "completed"})


def _wait_for_running(
    manager: Any,
    job_id: str,
    *,
    timeout_seconds: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    deadline = monotonic() + float(timeout_seconds)
    while monotonic() < deadline:
        status = manager.status(job_id)
        state = status.get("status")
        if state == "running":
            return status
        if state in _TERMINAL_STATES:
            raise RuntimeError(f"job became {state} before cancellation could be requested")
        sleep(0.2)
    raise TimeoutError("timed out waiting for the job to reach running")


def _wait_for_verified_cancelled(
    manager: Any,
    job_id: str,
    *,
    timeout_seconds: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    deadline = monotonic() + float(timeout_seconds)
    while monotonic() < deadline:
        status = manager.status(job_id)
        state = status.get("status")
        if state == "cancelled":
            try:
                execution = execution_from_terminal_job_state(status)
            except ValueError as exc:
                raise RuntimeError("cancelled state lacks verified cleanup evidence") from exc
            if execution["cleanup"]["verified"] is not True:
                raise RuntimeError("cancelled state lacks verified cleanup evidence")
            return status
        if state in _TERMINAL_STATES:
            raise RuntimeError(f"job became {state} instead of verified cancellation")
        sleep(0.2)
    raise TimeoutError("timed out waiting for verified cancellation")


def main() -> int:
    from src.evidence.real_fixture import controlled_fixture_from_environment

    fixture = controlled_fixture_from_environment()
    runtime = Path(os.environ.get("COMSOL_MCP_RUNTIME_DIR", "D:/comsol_runtime"))
    manager = JobManager(
        runtime / "durable_cancel" / "jobs",
        cancel_grace_seconds=10,
        cancel_terminate_seconds=2,
    )
    wavelength = fixture["wavelength_um"]
    submitted = manager.submit(
        {
            "job_type": "staged_sweep",
            "source_model_path": str(fixture["source"]),
            "parameter_name": "wl",
            "parameter_unit": "um",
            "parameter_values": [wavelength, wavelength + 0.002],
            "expressions": [
                "ewfd.Rtotal",
                "ewfd.Ttotal",
                "ewfd.Atotal",
                "ewfd.Rtotal+ewfd.Ttotal+ewfd.Atotal",
            ],
            "study_name": "std1",
            "version": "6.4",
            "cores": 14,
            "smoke_points": 1,
            "record_wavelength_controls": True,
            "physical_bounds": {
                "ewfd.Rtotal": [0, 1.001],
                "ewfd.Ttotal": [0, 1.001],
                "ewfd.Atotal": [0, 1.001],
                "ewfd.Rtotal+ewfd.Ttotal+ewfd.Atotal": [0.999, 1.001],
            },
        }
    )
    job_id = submitted["job_id"]
    _wait_for_running(manager, job_id, timeout_seconds=150.0)
    cancelled = manager.cancel(job_id)
    print(json.dumps(cancelled), flush=True)
    if cancelled.get("success") is not True:
        reason = cancelled.get("reason_code") or cancelled.get("error") or "cancel_refused"
        raise RuntimeError(f"durable cancellation request was refused: {reason}")
    status = _wait_for_verified_cancelled(manager, job_id, timeout_seconds=150.0)
    print(json.dumps(status), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
