"""Process-only gates for the reference-power licensed coordinator/worker boundary."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import subprocess
import sys
import time
import uuid

from development_kit.tests.integration.reference_power_acceptance import (
    _admit_lightweight_status,
    _redacted_status,
    _worker_summary,
)


ROOT = Path(__file__).parents[2]
RUNNER = ROOT / "development_kit" / "tests" / "integration" / "reference_power_acceptance.py"


def test_runner_import_and_dry_run_do_not_import_mph_or_start_comsol():
    import_probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import development_kit.tests.integration.reference_power_acceptance; "
                "print('true' if 'mph' in sys.modules else 'false')"
            ),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    dry_run = subprocess.run(
        [sys.executable, str(RUNNER), "--dry-run"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert import_probe.returncode == 0, import_probe.stderr
    assert import_probe.stdout.strip() == "false"
    assert dry_run.returncode == 0, dry_run.stderr
    receipt = json.loads(dry_run.stdout)
    assert receipt["real_comsol_started"] is False
    assert receipt["contract_valid"] is True


def test_lightweight_admission_fails_closed_without_exposing_commands_or_paths():
    status = {
        "complete": True,
        "error": None,
        "lease_path": "D:/private/solver_owner.json",
        "lease_state": "present",
        "lease_sha256": "a" * 64,
        "collision": True,
        "external_solver_processes": [
            {
                "pid": 123,
                "parent_pid": 100,
                "create_time": 1.0,
                "kind": "python-mph-client-script",
                "command_line": ["python", "C:/private/solver.py"],
            }
        ],
    }

    admitted, blockers = _admit_lightweight_status(status)
    redacted = _redacted_status(status)

    assert admitted is False
    assert "solver lease is not absent" in blockers
    assert "external COMSOL/MPh solver process detected" in blockers
    serialized = json.dumps(redacted)
    assert "private" not in serialized
    assert "command_line" not in serialized
    assert redacted["external_solver_processes"][0]["pid"] == 123


def test_coordinator_summary_keeps_failure_details_in_worker_artifact_only():
    payload = {
        "success": False,
        "error": "material readback mismatch",
        "traceback": "C:/private/source.py:1",
        "reference_result": {"source_path": "C:/private/model.mph"},
        "evaluation": {"passed": False},
        "client_clear": True,
        "lease_release": {"success": True},
    }

    summary = _worker_summary(payload)

    assert summary["error"] == "material readback mismatch"
    assert "traceback" not in summary
    assert "reference_result" not in summary
    assert "private" not in json.dumps(summary)


def test_real_mode_requires_explicit_authority_and_resource_limits():
    completed = subprocess.run(
        [sys.executable, str(RUNNER)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode != 0
    assert "licensed run requires" in completed.stderr
    source = RUNNER.read_text(encoding="utf-8")
    assert "CREATE_NO_WINDOW" in source
    assert "--timeout-seconds" in source


def test_coordinator_refuses_collision_before_starting_worker(tmp_path):
    source = tmp_path / "dummy.mph"
    source.write_bytes(b"not-a-real-model")
    blocker_script = tmp_path / "owned_solver.py"
    blocker_script.write_text(
        "# mph.Client collision marker for the lightweight scanner\nimport time\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    artifact_dir = Path(f"D:/comsol_runtime_test/reference_power_collision_{uuid.uuid4().hex}")
    spec_path = tmp_path / "spec.json"
    output_path = tmp_path / "receipt.json"
    spec = {
        "schema_name": "comsol_mcp.h1_execution_spec",
        "schema_version": "1.0.0",
        "config_id": "collision-refusal",
        "source_model_path": str(source.resolve()),
        "expected_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "artifact_dir": str(artifact_dir),
        "model": {
            "component_tag": "comp1", "physics_tag": "ewfd", "study_tag": "std1",
            "study_step_tag": "wl_step", "study_step_property": "plist",
        },
        "wavelength": {"value": 4.37, "unit": "um", "parameter": "wl"},
        "reference_air": {
            "expected_material_tags": ["mat1"],
            "all_domain_ids": [1, 2],
            "top_air_domain_ids": [2],
            "top_air_coordinate_range": {"x": [0, 1], "y": [0, 1], "z": [0.8, 1]},
            "target_axis": "x", "aggregation": "rms_abs",
            "r_expression": "ewfd.Rtotal", "t_expression": "ewfd.Ttotal",
        },
        "declared_plane_flux": {
            "incident": {
                "expression": "inc", "selection_ids": [10], "plane_coordinate_m": 1e-6,
                "normal": [0, 0, -1], "medium_id": "air", "positive_power_sign": -1,
            },
            "reflected": {
                "expression": "ref", "selection_ids": [11], "plane_coordinate_m": 1e-6,
                "normal": [0, 0, 1], "medium_id": "air", "positive_power_sign": 1,
            },
            "transmitted": {
                "expression": "trn", "selection_ids": [12], "plane_coordinate_m": -1e-6,
                "normal": [0, 0, -1], "medium_id": "air", "positive_power_sign": -1,
            },
        },
    }
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    blocker = subprocess.Popen(
        [sys.executable, str(blocker_script)],
        cwd=ROOT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        time.sleep(0.25)
        completed = subprocess.run(
            [
                sys.executable, str(RUNNER),
                "--confirm", "RUN_REAL_COMSOL",
                "--spec", str(spec_path),
                "--output", str(output_path),
                "--cores", "1",
                "--timeout-seconds", "30",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    finally:
        blocker.terminate()
        blocker.wait(timeout=10)
    receipt = json.loads(output_path.read_text(encoding="utf-8"))

    assert completed.returncode == 2
    assert receipt["worker_started"] is False
    assert receipt["pre_import_admission"]["admitted"] is False
    assert "external COMSOL/MPh solver process detected" in receipt["pre_import_admission"]["blockers"]
    assert not (artifact_dir / "worker_result.json").exists()
    if artifact_dir.exists():
        artifact_dir.rmdir()
