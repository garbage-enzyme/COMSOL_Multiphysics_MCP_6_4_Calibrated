"""Three-fresh-process acceptance gate for the native cancellation public cancel candidate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from src.jobs.process_control import OwnedJobObject

ROOT = Path(__file__).parents[3]
PROBE = ROOT / "development_kit" / "tests" / "integration" / "native_cancel_signature_probe.py"
SYSTEM32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
TASKKILL = SYSTEM32 / "taskkill.exe"
NATIVE_CANCEL_PROBE_MODEL_ENV = "COMSOL_MCP_NATIVE_CANCEL_PROBE_MODEL"


def _terminate_owned_process_tree(
    process: subprocess.Popen, containment: OwnedJobObject | None
) -> dict[str, object]:
    """Terminate only the exact subprocess tree launched by this gate."""
    errors = []
    if containment is not None:
        try:
            containment.close()
        except Exception as exc:
            errors.append({"stage": "job_object_close", "type": type(exc).__name__})
    if process.poll() is None:
        try:
            subprocess.run(
                [str(TASKKILL), "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append({"stage": "taskkill", "type": type(exc).__name__})
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append({"stage": "direct_kill", "type": type(exc).__name__})
    except (OSError, subprocess.SubprocessError) as exc:
        errors.append({"stage": "wait", "type": type(exc).__name__})
    root_absent = process.poll() is not None
    return {
        "passed": root_absent and containment is not None and not errors,
        "root_absent": root_absent,
        "job_object_contained": containment is not None,
        "errors": errors,
    }


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
    containment = OwnedJobObject.assign(process.pid)
    timed_out = False
    stdout = ""
    stderr = ""
    cleanup = None
    if containment is None:
        cleanup = _terminate_owned_process_tree(process, None)
        return {
            "timed_out": False,
            "returncode": process.returncode,
            "stdout": stdout,
            "stderr": "probe process tree could not be contained",
            "cleanup": cleanup,
        }
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = _timeout_output(exc.stdout)
        stderr = _timeout_output(exc.stderr)
        cleanup = _terminate_owned_process_tree(process, containment)
        containment = None
        tail_stdout, tail_stderr = process.communicate(timeout=15)
        stdout += tail_stdout or ""
        stderr += tail_stderr or ""
    finally:
        if cleanup is None:
            cleanup = _terminate_owned_process_tree(process, containment)
    return {
        "timed_out": timed_out,
        "returncode": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "cleanup": cleanup,
    }


@pytest.mark.integration
def test_progress_context_cancel_stops_real_study_in_three_fresh_processes():
    model_path = os.environ.get(NATIVE_CANCEL_PROBE_MODEL_ENV)
    if not model_path:
        pytest.skip(f"set {NATIVE_CANCEL_PROBE_MODEL_ENV} to run the real native cancellation gate")
    assert Path(model_path).is_file(), model_path

    runs = []
    failures = []
    for _index in range(3):
        environment = os.environ.copy()
        environment[NATIVE_CANCEL_PROBE_MODEL_ENV] = model_path
        execution = _run_probe(environment)
        if execution["timed_out"]:
            failures.append("native cancellation probe timed out")
            break
        if execution["cleanup"]["passed"] is not True:
            failures.append(f"native cancellation process cleanup failed: {execution['cleanup']}")
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
