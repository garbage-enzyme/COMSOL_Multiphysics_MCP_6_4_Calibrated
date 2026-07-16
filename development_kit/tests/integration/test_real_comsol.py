"""Opt-in COMSOL integration probes, each isolated in a fresh process."""

from pathlib import Path
import subprocess
import sys
import time

import pytest


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


def _terminate_owned_process_tree(process: subprocess.Popen) -> None:
    """Terminate only the exact subprocess tree created by this test."""
    if process.poll() is not None:
        return
    subprocess.run(
        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=15)


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
    try:
        output, _ = process.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        _terminate_owned_process_tree(process)
        pytest.fail(f"Integration probe timed out: {probe}")
    finally:
        _terminate_owned_process_tree(process)

    time.sleep(2)
    after = _comsol_pids()
    leaked = after - before

    assert process.returncode == 0, output
    assert not leaked, f"Integration probe leaked COMSOL PIDs {sorted(leaked)}\n{output}"
