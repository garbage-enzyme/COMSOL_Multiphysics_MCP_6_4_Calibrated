"""Three-fresh-process acceptance gate for the native cancellation public cancel candidate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
PROBE = ROOT / "development_kit" / "tests" / "integration" / "native_cancel_signature_probe.py"
SYSTEM32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
POWERSHELL = SYSTEM32 / "WindowsPowerShell" / "v1.0" / "powershell.exe"
TASKKILL = SYSTEM32 / "taskkill.exe"


def _comsol_pids() -> set[int]:
    command = (
        "@(Get-Process -ErrorAction SilentlyContinue | "
        "Where-Object { $_.ProcessName -like 'comsol*' } | "
        "Select-Object -ExpandProperty Id) -join ','"
    )
    completed = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return {int(value) for value in completed.stdout.strip().split(",") if value}


def _terminate_owned_process_tree(process: subprocess.Popen) -> None:
    """Terminate only the exact subprocess tree launched by this gate."""
    if process.poll() is not None:
        return
    try:
        subprocess.run(
            [str(TASKKILL), "/PID", str(process.pid), "/T", "/F"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        pass
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=15)


def _timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _run_probe(environment: dict[str, str], *, timeout_seconds: float = 180.0) -> dict:
    process = subprocess.Popen(
        [sys.executable, str(PROBE)],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    timed_out = False
    stdout = ""
    stderr = ""
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = _timeout_output(exc.stdout)
        stderr = _timeout_output(exc.stderr)
        _terminate_owned_process_tree(process)
        tail_stdout, tail_stderr = process.communicate(timeout=15)
        stdout += tail_stdout or ""
        stderr += tail_stderr or ""
    finally:
        _terminate_owned_process_tree(process)
    return {
        "timed_out": timed_out,
        "returncode": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


@pytest.mark.integration
def test_progress_context_cancel_stops_real_study_in_three_fresh_processes():
    model_path = os.environ.get("COMSOL_durable cancellationA_PROBE_MODEL")
    if not model_path:
        pytest.skip(
            "set COMSOL_durable cancellationA_PROBE_MODEL to run the real native cancellation gate"
        )
    assert Path(model_path).is_file(), model_path

    before = _comsol_pids()
    runs = []
    failures = []
    try:
        for _index in range(3):
            environment = os.environ.copy()
            environment["COMSOL_durable cancellationA_PROBE_MODEL"] = model_path
            execution = _run_probe(environment)
            if execution["timed_out"]:
                failures.append("native cancellation probe timed out")
                break
            if execution["returncode"] != 0:
                failures.append(execution["stdout"] + execution["stderr"])
                break
            try:
                runs.append(json.loads(execution["stdout"]))
            except json.JSONDecodeError as exc:
                failures.append(
                    "native cancellation probe did not emit one JSON manifest: "
                    f"{exc}\n{execution['stdout']}"
                )
                break
    finally:
        time.sleep(2)
        after = _comsol_pids()

    assert after == before, f"native cancellation gate leaked COMSOL PIDs {sorted(after - before)}"
    assert not failures, "\n".join(failures)
    assert len(runs) == 3
    for result in runs:
        gate = result["progress_context_gate"]
        assert result["client"] == {"standalone": True, "port": None}
        assert (
            result["native_cancel"]
            == "progress_context_candidate_passed_one_run_pending_three_run_gate"
        )
        assert gate["solve_active_before_request"] is True
        assert gate["candidate_outcome"] == "returned"
        assert "<CANCEL>" in gate["solve_return"]
        assert gate["solve_elapsed_s"] < 15.0
        assert gate["thread_alive_after_join"] is False
        assert gate["cleanup_safe"] is True
        assert gate["source_sha256_before"] == gate["source_sha256_after"]
        assert result["cleanup"]["client_clear"] == "verified"
