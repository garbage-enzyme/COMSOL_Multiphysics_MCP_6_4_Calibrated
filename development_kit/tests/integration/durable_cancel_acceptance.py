"""Manual real-COMSOL cancellation probe using an explicit local fixture."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time


ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT))

from src.evidence.real_fixture import controlled_fixture_from_environment
from src.jobs.manager import JobManager


fixture = controlled_fixture_from_environment()
runtime = Path(os.environ.get("COMSOL_MCP_RUNTIME_DIR", "D:/comsol_runtime"))
manager = JobManager(runtime / "durable_cancel" / "jobs", cancel_grace_seconds=10, cancel_terminate_seconds=2)
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
deadline = time.time() + 150
while time.time() < deadline:
    status = manager.status(job_id)
    if status["status"] == "running":
        print(json.dumps(manager.cancel(job_id)), flush=True)
        break
    time.sleep(0.2)
while time.time() < deadline:
    status = manager.status(job_id)
    if status["status"] in {"cancelled", "failed", "interrupted", "completed"}:
        print(json.dumps(status), flush=True)
        break
    time.sleep(0.2)
else:
    raise SystemExit("timeout")
