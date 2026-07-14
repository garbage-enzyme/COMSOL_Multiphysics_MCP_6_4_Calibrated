"""H4b containment and protocol gates for the isolated fake worker."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import uuid

import pytest

from src.knowledge.semantic_contracts import PUBLIC_LIMITS, WORKER_PROTOCOL_SCHEMA_VERSION
from src.knowledge.semantic_process import SemanticWorkerManager
from src.knowledge.lexical_manual import build_index_from_records, search_index
from src.tools.capabilities import get_capabilities
from src.tools.ownership import SolverOwnership
from src.jobs.manager import JobManager


def _raw_request(port: int, payload: dict, *, maximum: int = 200_000) -> dict:
    with socket.create_connection(("127.0.0.1", port), timeout=3.0) as connection:
        connection.settimeout(3.0)
        connection.sendall(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
        data = bytearray()
        while not data.endswith(b"\n"):
            block = connection.recv(4096)
            if not block:
                break
            data.extend(block)
            if len(data) > maximum:
                break
    return json.loads(bytes(data).decode("utf-8"))


def _request(manager: SemanticWorkerManager, request_id: str, **fields: object) -> dict:
    return {
        "schema_version": WORKER_PROTOCOL_SCHEMA_VERSION,
        "request_id": request_id,
        "token": manager._token,
        **fields,
    }


def test_happy_path_reuses_one_worker_and_reset_verifies_absence():
    with SemanticWorkerManager(startup_deadline=2.0, query_deadline=2.0) as manager:
        first = manager.query("CopyFace source destination", limit=2)
        pid = manager.status(probe=False)["identity"]["pid"]
        second = manager.query("alpha1_inc", limit=1)
        health = manager.health()

        assert first["success"] is second["success"] is health["success"] is True
        assert len(first["results"]) == 2
        assert health["status"]["query_count"] == 2
        assert health["status"]["load_count"] == 0
        assert manager.status(probe=False)["identity"]["pid"] == pid
        reset = manager.reset()
        assert reset["success"] is True
        assert reset["reset"]["absent"] is True
        assert manager.status()["state"] == "stopped"


@pytest.mark.parametrize("fault", [
    "query_hang",
    "invalid_json",
    "oversized_json",
    "wrong_request_id",
    "crash_before_response",
])
def test_query_protocol_faults_are_contained_without_retry(fault: str):
    manager = SemanticWorkerManager(startup_deadline=2.0, query_deadline=0.25, fault=fault)
    result = manager.query("bounded fault probe")

    assert result["success"] is False
    assert result["error"]["code"] == "worker_protocol_failure"
    assert result["retried"] is False
    assert result["cleanup"]["absent"] is True
    assert manager.status()["state"] == "stopped"


def test_startup_hang_and_port_collision_are_contained():
    hanging = SemanticWorkerManager(startup_deadline=0.2, fault="startup_hang")
    result = hanging.start()
    assert result["success"] is False
    assert result["cleanup"]["absent"] is True

    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        collision = SemanticWorkerManager(startup_deadline=1.0, forced_port=occupied.getsockname()[1])
        result = collision.start()
    assert result["success"] is False
    assert result["cleanup"]["absent"] is True


def test_authentication_schema_and_message_bounds_do_not_kill_worker():
    with SemanticWorkerManager(startup_deadline=2.0) as manager:
        assert manager.start()["success"] is True
        port = int(manager._port)
        wrong = _raw_request(port, {
            "schema_version": WORKER_PROTOCOL_SCHEMA_VERSION,
            "request_id": "wrong-token",
            "token": "0" * 64,
            "operation": "health",
        })
        schema = _raw_request(port, {
            "schema_version": "999",
            "request_id": "wrong-schema",
            "token": manager._token,
            "operation": "health",
        })
        assert wrong["error"]["code"] == "unauthorized"
        assert schema["error"]["code"] == "invalid_schema"
        assert manager.health()["success"] is True

        with socket.create_connection(("127.0.0.1", port), timeout=2.0) as connection:
            connection.sendall(b"{" + b"x" * 17_000 + b"\n")
            oversized = json.loads(connection.makefile("rb").readline().decode("utf-8"))
        assert oversized["error"]["code"] == "invalid_request"
        assert manager.health()["success"] is True


def test_queue_overflow_is_bounded_and_worker_recovers():
    with SemanticWorkerManager(startup_deadline=2.0, query_delay=0.2) as manager:
        assert manager.start()["success"] is True
        port = int(manager._port)
        barrier = threading.Barrier(12)

        def call(index: int) -> dict:
            barrier.wait(timeout=2.0)
            return _raw_request(port, _request(
                manager,
                f"burst-{index}",
                operation="query",
                query=f"query {index}",
                limit=1,
            ))

        with ThreadPoolExecutor(max_workers=12) as pool:
            responses = list(pool.map(call, range(12)))
        busy = [item for item in responses if not item["success"] and item["error"]["code"] == "busy"]
        assert busy
        assert manager.health()["success"] is True


def test_stale_identity_refuses_action_until_exact_record_is_restored():
    manager = SemanticWorkerManager(startup_deadline=2.0)
    assert manager.start()["success"] is True
    original = dict(manager._identity)
    manager._identity["process_create_time"] -= 10.0

    refused = manager.reset()
    assert refused["success"] is False
    assert refused["reset"]["refused"] is True
    assert manager._process is not None and manager._process.poll() is None
    restart = manager.start()
    assert restart["success"] is False
    assert restart["error"]["code"] == "worker_identity_uncertain"
    assert manager._process is not None and manager._process.pid == original["pid"]

    manager._identity = original
    assert manager.reset()["success"] is True


def test_crash_after_response_is_observed_without_process_leak():
    manager = SemanticWorkerManager(startup_deadline=2.0, fault="crash_after_response")
    response = manager.query("respond then crash")
    assert response["success"] is True
    deadline = time.monotonic() + 2.0
    while manager._process is not None and manager._process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert manager._process is not None and manager._process.poll() is not None
    assert manager.reset()["success"] is True


def test_idle_ttl_stops_worker_lazily():
    manager = SemanticWorkerManager(startup_deadline=2.0, idle_ttl=0.05)
    assert manager.health()["success"] is True
    time.sleep(0.08)
    assert manager.status()["state"] == "stopped"


def test_hanging_semantic_worker_does_not_delay_control_plane_or_lexical_search():
    root = Path("D:/comsol_semantic_h4b_test") / uuid.uuid4().hex
    index = root / "manuals.sqlite3"
    runtime = root / "runtime"
    build_index_from_records([{
        "source": "fake/manual.pdf", "module": "fake", "page": 1,
        "heading": "CopyFace", "text": "CopyFace source destination mesh",
    }], index, corpus_fingerprint="h4b-test")
    manager = SemanticWorkerManager(startup_deadline=2.0, query_deadline=0.5, fault="query_hang")
    result: dict = {}

    job_manager = JobManager(root / "jobs", reconcile_on_start=False)
    job_id = job_manager.store.create(
        {"schema_version": "2", "job_type": "test_sequence"},
        {"schema_version": "2", "status": "completed", "worker_pid": None},
    )
    baseline_started = time.perf_counter()
    assert get_capabilities()["success"] is True
    assert SolverOwnership(runtime_dir=runtime).status()["lease"]["state"] == "absent"
    assert job_manager.status(job_id)["status"] == "completed"
    baseline_elapsed = time.perf_counter() - baseline_started

    thread = threading.Thread(target=lambda: result.update(manager.query("hang")), daemon=True)
    thread.start()
    time.sleep(0.1)
    control_started = time.perf_counter()
    capabilities = get_capabilities()
    ownership = SolverOwnership(runtime_dir=runtime).status()
    job_status = job_manager.status(job_id)
    control_elapsed = time.perf_counter() - control_started
    lexical_started = time.perf_counter()
    lexical = search_index("CopyFace", index_path=index)
    lexical_elapsed = time.perf_counter() - lexical_started
    join_budget = min(8.0, max(4.0, baseline_elapsed * 2.0 + 1.0))
    thread.join(timeout=join_budget)

    assert capabilities["success"] is True
    assert ownership["lease"]["state"] == "absent"
    # External solver discovery is host-wide. A user-owned standalone solve may
    # legitimately be present; this containment test requires responsiveness
    # and lease isolation, not an otherwise idle host.
    assert isinstance(ownership["external_solver_processes"], list)
    assert job_status["success"] is True and job_status["status"] == "completed"
    assert lexical["success"] is True and lexical["results"]
    # Host-wide process inventory latency changes when a user-owned solver is
    # factorizing. Compare against an immediately measured no-hang baseline and
    # retain an absolute containment ceiling.
    assert control_elapsed < 8.0
    assert control_elapsed < max(4.0, baseline_elapsed * 2.0 + 0.5)
    assert lexical_elapsed < 4.0
    assert not thread.is_alive()
    assert result["success"] is False and result["cleanup"]["absent"] is True
    assert not (runtime / "solver_owner.json").exists()
    if root.exists():
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def test_parent_import_is_stdlib_only_and_spawns_nothing():
    code = """
import json, sys
import src.knowledge.semantic_process
for name in ('chromadb', 'torch', 'sentence_transformers', 'mph', 'psutil'):
    assert name not in sys.modules, name
print(json.dumps({'ok': True}))
"""
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["ok"] is True
