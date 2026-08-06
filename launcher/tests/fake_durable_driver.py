from __future__ import annotations

import importlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(os.environ["DURABLE_TEST_ROOT"])
HELPER_DIR = Path(os.environ["DURABLE_TEST_HELPER_DIR"])
JOB_ID = os.environ.get("DURABLE_TEST_JOB_ID", "durable-launcher-test-v1-2")
SPEC_ID = os.environ.get("DURABLE_TEST_SPEC_ID", "fake-spec-v1")
POINTS = int(os.environ.get("DURABLE_TEST_POINTS", "8"))
POINT_SECONDS = float(os.environ.get("DURABLE_TEST_POINT_SECONDS", "0.2"))

sys.path.insert(0, str(HELPER_DIR))
durable_control = importlib.import_module("durable_control")


def process_is_alive(pid: object) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError, OverflowError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_lock(lock_path: Path) -> dict[str, object] | None:
    try:
        owner = json.loads(lock_path.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        return None
    return owner if isinstance(owner, dict) else None


def claim_lock(lock_path: Path) -> str:
    claim_id = uuid.uuid4().hex
    for attempt in range(20):
        if lock_path.is_file():
            owner = _read_lock(lock_path)
            if owner is None:
                if attempt == 19:
                    raise RuntimeError("fake durable driver lock publication did not complete")
                time.sleep(0.01)
                continue
            owner_pid = owner.get("pid")
            if process_is_alive(owner_pid):
                raise RuntimeError(f"fake durable driver is already active: pid={owner_pid}")
            lock_path.unlink(missing_ok=True)
        try:
            with lock_path.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump({"pid": os.getpid(), "spec_id": SPEC_ID, "claim_id": claim_id}, handle)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            return claim_id
        except FileExistsError:
            if attempt == 19:
                raise RuntimeError("fake durable driver lock claim remained contended")
            time.sleep(0.01)
    raise RuntimeError("fake durable driver lock claim failed")


def append_jsonl(path: Path, value: object) -> None:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(encoded + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def completed_rows(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    results_path = ROOT / "results.jsonl"
    status_path = ROOT / "status.json"
    control_dir = ROOT / "control"
    lock_path = ROOT / "run.lock"
    claim_id = claim_lock(lock_path)
    started = time.time()
    try:
        completed = completed_rows(results_path)
        for index in range(len(completed), POINTS):
            request = durable_control.pending_pause_request(
                control_dir, job_id=JOB_ID, spec_id=SPEC_ID
            )
            if request is not None:
                latest = None if not completed else str(completed[-1]["point_id"])
                durable_control.atomic_json(
                    status_path,
                    {
                        "status": "paused_after_point",
                        "spec_id": SPEC_ID,
                        "completed": len(completed),
                        "planned": POINTS,
                        "latest_point_id": latest,
                        "elapsed_seconds": time.time() - started,
                        "pause_request_id": request["request_id"],
                    },
                )
                durable_control.acknowledge_pause(
                    control_dir,
                    request,
                    job_id=JOB_ID,
                    spec_id=SPEC_ID,
                    completed=len(completed),
                    planned=POINTS,
                    latest_point_id=latest,
                )
                return
            point_id = f"point_{index:03d}"
            durable_control.atomic_json(
                status_path,
                {
                    "status": "running",
                    "spec_id": SPEC_ID,
                    "completed": len(completed),
                    "planned": POINTS,
                    "latest_point_id": None if not completed else completed[-1]["point_id"],
                    "active_point_id": point_id,
                    "elapsed_seconds": time.time() - started,
                },
            )
            time.sleep(POINT_SECONDS)
            row = {
                "status": "ok",
                "spec_id": SPEC_ID,
                "point_id": point_id,
                "completed_at_epoch": time.time(),
            }
            append_jsonl(results_path, row)
            completed.append(row)
            durable_control.atomic_json(
                status_path,
                {
                    "status": "running" if len(completed) < POINTS else "complete",
                    "spec_id": SPEC_ID,
                    "completed": len(completed),
                    "planned": POINTS,
                    "latest_point_id": point_id,
                    "elapsed_seconds": time.time() - started,
                },
            )
        durable_control.atomic_json(
            status_path,
            {
                "status": "complete",
                "spec_id": SPEC_ID,
                "completed": len(completed),
                "planned": POINTS,
                "latest_point_id": None if not completed else completed[-1]["point_id"],
                "elapsed_seconds": time.time() - started,
            },
        )
    finally:
        owner = _read_lock(lock_path)
        if owner is not None and owner.get("claim_id") == claim_id:
            lock_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
