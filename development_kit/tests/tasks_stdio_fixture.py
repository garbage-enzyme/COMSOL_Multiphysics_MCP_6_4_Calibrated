"""Private barrier-controlled durable engine for real stdio Tasks tests."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from comsol_mcp.durable.io import atomic_write_json


class BarrierEngine:
    """Persist fixed fake jobs. Only a test-owned barrier permits completion."""

    def __init__(self, root: Path):
        self.root = root

    def submit(self, spec):
        fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
        job_id = "job-" + fingerprint[:32]
        path = self.root / (job_id + ".json")
        duplicate = path.exists()
        if not duplicate:
            atomic_write_json(path, {"job_id": job_id, "status": "running", "attempt": 1})
            count = self.root / "submission_count"
            count.write_text(str(int(count.read_text()) + 1 if count.exists() else 1))
        return {"success": True, "job_id": job_id, "status": "running", "duplicate": duplicate}

    def status(self, job_id):
        path = self.root / (job_id + ".json")
        state = json.loads(path.read_text())
        if (self.root / (job_id + ".release")).exists():
            state["status"] = "cancelled" if state.get("cancel_requested") else "completed"
            state["cleanup"] = {"lease_released": True, "worker_absent": True}
            atomic_write_json(path, state)
        return {"success": True, **state}

    def cancel(self, job_id, *, expected_attempt=None):
        state = self.status(job_id)
        if expected_attempt != state["attempt"]:
            raise ValueError("unexpected cancellation attempt")
        state.update(status="cancel_requested", cancel_requested=True)
        atomic_write_json(self.root / (job_id + ".json"), state)
        return {"success": True, "job_id": job_id, "status": "cancel_requested"}


def main():
    from comsol_mcp import platform_support
    from comsol_mcp.server import create_server
    from comsol_mcp.tools import jobs

    # This injection belongs only to the private fixture. Production Linux
    # refuses native submission. The fake engine imports no COMSOL module.
    platform_support.native_solver_enabled = lambda: (
        os.environ.get("TASKS_TEST_ADMIT_NATIVE", "true") == "true"
    )
    jobs.job_manager = BarrierEngine(Path(os.environ["TASKS_TEST_ENGINE_ROOT"]))
    create_server(profile=os.environ.get("TASKS_TEST_PROFILE", "core")).run()


if __name__ == "__main__":
    main()
