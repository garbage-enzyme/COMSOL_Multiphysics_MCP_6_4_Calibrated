"""Three-fresh-process acceptance gate for the native cancellation public cancel candidate."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).parents[3]
PROBE = ROOT / "development_kit" / "tests" / "integration" / "native_cancel_signature_probe.py"


def _comsol_pids() -> set[int]:
    command = (
        "@(Get-Process -ErrorAction SilentlyContinue | "
        "Where-Object { $_.ProcessName -like 'comsol*' } | "
        "Select-Object -ExpandProperty Id) -join ','"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return {int(value) for value in completed.stdout.strip().split(",") if value}


@pytest.mark.integration
def test_progress_context_cancel_stops_real_study_in_three_fresh_processes():
    model_path = os.environ.get("COMSOL_durable cancellationA_PROBE_MODEL")
    if not model_path:
        pytest.skip("set COMSOL_durable cancellationA_PROBE_MODEL to run the real native cancellation gate")
    assert Path(model_path).is_file(), model_path

    before = _comsol_pids()
    runs = []
    for index in range(3):
        environment = os.environ.copy()
        environment["COMSOL_durable cancellationA_PROBE_MODEL"] = model_path
        completed = subprocess.run(
            [sys.executable, str(PROBE)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            pytest.fail(f"native cancellation probe did not emit one JSON manifest: {exc}\n{completed.stdout}")
        runs.append(result)

    time.sleep(2)
    assert _comsol_pids() == before
    for result in runs:
        gate = result["progress_context_gate"]
        assert result["client"] == {"standalone": True, "port": None}
        assert result["native_cancel"] == "progress_context_candidate_passed_one_run_pending_three_run_gate"
        assert gate["solve_active_before_request"] is True
        assert gate["candidate_outcome"] == "returned"
        assert "<CANCEL>" in gate["solve_return"]
        assert gate["solve_elapsed_s"] < 15.0
        assert gate["thread_alive_after_90_s"] is False
        assert gate["source_sha256_before"] == gate["source_sha256_after"]
