"""coordinator recovery helper: stop only the exact coordinator after its durable claim."""

from __future__ import annotations

from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from src.jobs.process_control import terminate_exact
from src.jobs.store import JobStore, atomic_write_json


def main(root: str, job_id: str, timeout_seconds: float = 30.0) -> int:
    store = JobStore(Path(root))
    evidence_path = store.job_dir(job_id) / "coordinator_restart_probe.json"
    deadline = time.monotonic() + float(timeout_seconds)
    while time.monotonic() < deadline:
        try:
            state = store.read_state(job_id)
        except RuntimeError:
            time.sleep(0.005)
            continue
        cancel = state.get("cancel") if isinstance(state.get("cancel"), dict) else {}
        coordinator = cancel.get("coordinator")
        if state.get("status") == "cancelling" and isinstance(coordinator, dict):
            try:
                action = terminate_exact(coordinator)
                evidence = {
                    "job_id": job_id,
                    "observed_status": state.get("status"),
                    "observed_phase": cancel.get("phase"),
                    "coordinator": coordinator,
                    "before": action.get("before"),
                    "action": action,
                    "timestamp_epoch": time.time(),
                }
                exit_code = 0 if action.get("acted") else 2
            except Exception as exc:
                evidence = {
                    "job_id": job_id,
                    "observed_status": state.get("status"),
                    "observed_phase": cancel.get("phase"),
                    "coordinator": coordinator,
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                    "timestamp_epoch": time.time(),
                }
                exit_code = 2
            atomic_write_json(evidence_path, evidence)
            return exit_code
        time.sleep(0.005)
    atomic_write_json(
        evidence_path,
        {"job_id": job_id, "error": "timed out waiting for a claimed coordinator"},
    )
    return 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else 30.0))
