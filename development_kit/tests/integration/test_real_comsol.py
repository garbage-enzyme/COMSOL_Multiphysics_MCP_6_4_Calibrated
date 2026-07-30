"""Opt-in COMSOL integration probes, each isolated in a fresh process."""

import subprocess
import sys
import time
from pathlib import Path

import pytest
from src.jobs.process_control import OwnedJobObject

ROOT = Path(__file__).parents[3]
PROBES = (
    pytest.param("development_kit/tests/integration/probes/study_mesh.py", id="study_mesh"),
    pytest.param("development_kit/tests/integration/probes/capacitor.py", id="capacitor"),
    pytest.param("development_kit/tests/integration/probes/unicode_save.py", id="unicode_save"),
    "development_kit/tests/integration/native_cancel_signature_probe.py",
    "development_kit/tests/integration/clientapi_property_acceptance.py",
    "development_kit/tests/integration/wave_optics_preflight_acceptance.py",
    "development_kit/tests/integration/periodic_mesh_acceptance.py",
    "development_kit/tests/integration/derived_geometry_acceptance.py",
    "development_kit/tests/integration/incidence_configuration_acceptance.py",
    "development_kit/tests/integration/resource_admission_acceptance.py",
    "development_kit/tests/integration/wave_optics_point_audit_acceptance.py",
    "development_kit/tests/integration/live_profile_acceptance.py",
)


def _comsol_pids() -> set[int]:
    """Return live COMSOL process IDs without starting COMSOL."""
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
    output = completed.stdout.strip()
    return {int(value) for value in output.split(",") if value}


def _terminate_owned_process_tree(
    process: subprocess.Popen,
    containment: OwnedJobObject | None,
) -> dict[str, object]:
    """Terminate only the exact subprocess tree created by this test."""
    errors = []
    if containment is not None:
        try:
            containment.close()
        except Exception as exc:
            errors.append({"stage": "job_object_close", "type": type(exc).__name__})
    if process.poll() is None:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
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
        "passed": root_absent and not errors and containment is not None,
        "root_absent": root_absent,
        "job_object_contained": containment is not None,
        "errors": errors,
    }


@pytest.mark.integration
@pytest.mark.parametrize("probe", PROBES)
def test_real_comsol_probe_in_fresh_process(probe):
    before = _comsol_pids()
    creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(
        [sys.executable, str(ROOT / probe)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=creation_flags,
    )
    containment = OwnedJobObject.assign(process.pid)
    cleanup = None
    if containment is None:
        _terminate_owned_process_tree(process, None)
        pytest.fail(f"Integration probe could not contain its process tree: {probe}")
    try:
        output, _ = process.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        _terminate_owned_process_tree(process, containment)
        containment = None
        pytest.fail(f"Integration probe timed out: {probe}")
    finally:
        cleanup = _terminate_owned_process_tree(process, containment)

    time.sleep(2)
    after = _comsol_pids()
    leaked = after - before

    assert process.returncode == 0, output
    assert cleanup["passed"] is True, cleanup
    assert not leaked, f"Integration probe leaked COMSOL PIDs {sorted(leaked)}\n{output}"
