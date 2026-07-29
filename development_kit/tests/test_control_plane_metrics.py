"""control-plane metrics bounded latency, overload outcome, and fairness evidence."""

from __future__ import annotations

import asyncio
import inspect
import json
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import src.knowledge.lexical_manual as lexical_module
import src.tools.jobs as jobs_module
import src.tools.ownership as ownership_module
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from src.jobs.store import JOB_SCHEMA_VERSION, JobStore
from src.tools.capabilities import get_capabilities
from src.tools.ownership import SolverOwnership
from src.tools.profiles import ProfileSelection, register_profiled
from src.utils.control_plane import ControlPlaneMetrics, control_plane_metrics


@pytest.fixture(autouse=True)
def reset_control_metrics():
    control_plane_metrics.reset()
    yield
    control_plane_metrics.reset()


@pytest.fixture()
def runtime_root():
    root = Path("D:/comsol_runtime_test/control_plane") / uuid.uuid4().hex
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_metrics_window_is_bounded_and_reports_nearest_rank_latency():
    metrics = ControlPlaneMetrics(window_size=256)
    for index in range(300):
        metrics.record("solver_status", index / 1000.0, {"success": True})
    summary = metrics.summary("solver_status")

    assert summary["window_capacity"] == 256
    assert summary["window_samples"] == 256
    assert summary["total_recorded"] == 300
    assert summary["outcomes"] == {
        "success": 256,
        "busy": 0,
        "timeout": 0,
        "error": 0,
    }
    assert summary["latency"] == {
        "p50_seconds": 0.171,
        "p95_seconds": 0.287,
        "max_seconds": 0.299,
    }


def test_metrics_classify_structured_busy_timeout_and_error():
    metrics = ControlPlaneMetrics(window_size=8)
    metrics.record(
        "manual_search",
        0.1,
        {"success": False, "error": {"code": "busy", "message": "worker queue is full"}},
    )
    metrics.record(
        "manual_search",
        0.2,
        {"success": False, "error_type": "TimeoutError", "error": "deadline exceeded"},
    )
    metrics.record(
        "manual_search",
        0.3,
        {"success": False, "error_type": "WorkerError", "error": "invalid JSON"},
    )

    assert metrics.summary("manual_search")["outcomes"] == {
        "success": 0,
        "busy": 1,
        "timeout": 1,
        "error": 1,
    }


def test_capability_job_and_manual_tools_attach_bounded_evidence(runtime_root, monkeypatch):
    capabilities = get_capabilities()
    assert capabilities["control_plane"]["operation"] == "capabilities"
    assert capabilities["control_plane"]["outcome"] == "success"

    monkeypatch.setattr(jobs_module, "job_manager", jobs_module.JobManager(runtime_root / "jobs"))
    job_server = FastMCP("control-plane-jobs")
    jobs_module.register_job_tools(job_server)
    status = job_server._tool_manager._tools["job_status"].fn("missing")
    tail = job_server._tool_manager._tools["job_tail"].fn("missing", 5)
    assert status["control_plane"]["operation"] == "job_status"
    assert status["control_plane"]["outcome"] == "error"
    assert tail["control_plane"]["operation"] == "job_tail"

    responses = iter([
        {"success": False, "error": {"code": "busy", "message": "worker queue is full"}},
        {"success": False, "error_type": "TimeoutError", "error": "deadline exceeded"},
    ])
    monkeypatch.setattr(lexical_module, "run_bounded", lambda *_args, **_kwargs: next(responses))
    manual_server = FastMCP("control-plane-manuals")
    lexical_module.register_lexical_manual_tools(manual_server)
    busy = manual_server._tool_manager._tools["manual_search"].fn("query")
    timeout = manual_server._tool_manager._tools["manual_read_pages"].fn("manual.pdf", [1])
    assert busy["control_plane"]["outcome"] == "busy"
    assert timeout["control_plane"]["outcome"] == "timeout"


def test_slow_inventory_does_not_starve_durable_cancel_request(runtime_root, monkeypatch):
    inventory_started = threading.Event()

    def slow_inventory():
        inventory_started.set()
        time.sleep(0.6)
        return []

    monkeypatch.setattr(ownership_module, "_system_processes", slow_inventory)
    monkeypatch.setattr(ownership_module, "PROCESS_INVENTORY_STATUS_TIMEOUT_SECONDS", 0.4)
    ownership = SolverOwnership(
        runtime_root / "solver",
        pid=900_000,
        parent_pid=0,
        create_time=1.0,
        command_line=["python.exe", "-m", "src.server"],
        owner="control-plane-fairness",
    )
    store = JobStore(runtime_root / "jobs")
    job_id = store.create(
        {"schema_version": JOB_SCHEMA_VERSION, "job_type": "fairness"},
        {
            "schema_version": JOB_SCHEMA_VERSION,
            "status": "running",
            "attempt": 1,
            "worker_pid": None,
            "worker_process_create_time": None,
            "worker_command_signature": None,
        },
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        status_future = executor.submit(ownership.status)
        assert inventory_started.wait(timeout=1)
        cancel_started = time.monotonic()
        cancel = store.request_cancel(
            job_id,
            requester_identity={"pid": 123, "process_create_time": 1.0},
        )
        cancel_latency = time.monotonic() - cancel_started
        status = status_future.result(timeout=2)

    assert cancel["accepted"] is True
    assert cancel_latency < 0.3
    assert store.read_state(job_id)["status"] == "cancel_requested"
    assert status["process_inventory"]["complete"] is False
    assert status["collision"] is True


def test_concurrent_wrappers_record_bounded_latency_and_overload_outcomes(
    runtime_root, monkeypatch
):
    monkeypatch.setattr(jobs_module, "job_manager", jobs_module.JobManager(runtime_root / "jobs"))
    jobs_server = FastMCP("control-plane-concurrent-jobs")
    jobs_module.register_job_tools(jobs_server)

    own = SolverOwnership(
        runtime_root / "solver",
        process_provider=lambda: [],
        pid=910_000,
        parent_pid=0,
        create_time=1.0,
        command_line=["python.exe", "-m", "src.server"],
        owner="control-plane-concurrent",
    )
    monkeypatch.setattr(ownership_module, "ownership_manager", own)
    ownership_server = FastMCP("control-plane-concurrent-ownership")
    ownership_module.register_ownership_tools(ownership_server)

    response_lock = threading.Lock()
    response_index = 0

    def manual_response(*_args, **_kwargs):
        nonlocal response_index
        with response_lock:
            index = response_index
            response_index += 1
        if index % 3 == 0:
            return {"success": True, "count": 0, "results": []}
        if index % 3 == 1:
            return {"success": False, "error": {"code": "busy", "message": "queue is full"}}
        return {"success": False, "error_type": "TimeoutError", "error": "deadline exceeded"}

    monkeypatch.setattr(lexical_module, "run_bounded", manual_response)
    manual_server = FastMCP("control-plane-concurrent-manual")
    lexical_module.register_lexical_manual_tools(manual_server)

    calls = []
    for index in range(30):
        calls.extend([
            lambda: get_capabilities(),
            lambda: ownership_server._tool_manager._tools["solver_status"].fn(),
            lambda: jobs_server._tool_manager._tools["job_status"].fn("missing"),
            lambda: jobs_server._tool_manager._tools["job_tail"].fn("missing", 5),
            lambda: manual_server._tool_manager._tools["manual_search"].fn("query"),
        ])

    with ThreadPoolExecutor(max_workers=16) as executor:
        responses = list(executor.map(lambda callback: callback(), calls))

    assert len(responses) == 150
    for operation in (
        "capabilities",
        "solver_status",
        "job_status",
        "job_tail",
        "manual_search",
    ):
        summary = control_plane_metrics.summary(operation)
        assert summary["window_samples"] == 30
        assert summary["total_recorded"] == 30
        assert summary["latency"]["p50_seconds"] is not None
        assert summary["latency"]["p95_seconds"] is not None
        assert summary["latency"]["max_seconds"] < 2.0
    manual_outcomes = control_plane_metrics.summary("manual_search")["outcomes"]
    assert manual_outcomes == {
        "success": 10,
        "busy": 10,
        "timeout": 10,
        "error": 0,
    }
    assert max(
        len(json.dumps(response["control_plane"], ensure_ascii=False))
        for response in responses
    ) < 1000


def _profiled_preflight_server() -> FastMCP:
    server = FastMCP("control-plane-preflight")
    register_profiled(
        server,
        ownership_module.register_ownership_tools,
        frozenset({"solver_preflight"}),
        ProfileSelection(
            name="core",
            environment_variable="COMSOL_MCP_SETTINGS_PATH",
            default_used=False,
            source="test",
        ),
    )
    return server


def test_public_solver_preflight_runs_entire_callback_off_event_loop(monkeypatch):
    import src.tools.session as session_module

    entered = threading.Event()
    release = threading.Event()
    worker_threads = {}

    class OwnershipStub:
        def preflight(self, **kwargs):
            worker_threads["preflight"] = threading.get_ident()
            worker_threads["session_state"] = kwargs["session_state"]
            entered.set()
            assert release.wait(timeout=2.0)
            return {"success": True, "ready": True, "blockers": []}

    def get_status():
        worker_threads["session"] = threading.get_ident()
        return {"connected": False, "starting": False}

    monkeypatch.setattr(ownership_module, "ownership_manager", OwnershipStub())
    monkeypatch.setattr(session_module.session_manager, "get_status", get_status)
    server = _profiled_preflight_server()
    tool = server._tool_manager._tools["solver_preflight"]

    async def exercise():
        event_loop_thread = threading.get_ident()
        task = asyncio.create_task(tool.run({}))
        assert await asyncio.to_thread(entered.wait, 1.0)
        event_loop_progress_thread = threading.get_ident()
        release.set()
        result = await asyncio.wait_for(task, timeout=2.0)
        return event_loop_thread, event_loop_progress_thread, result

    event_loop_thread, progress_thread, result = asyncio.run(exercise())

    assert tool.is_async is True
    assert inspect.iscoroutinefunction(tool.fn)
    assert progress_thread == event_loop_thread
    assert worker_threads["preflight"] != event_loop_thread
    assert worker_threads["session"] == worker_threads["preflight"]
    assert worker_threads["session_state"] == {
        "connected": False,
        "starting": False,
    }
    assert result["ready"] is True
    assert result["control_plane"]["operation"] == "solver_preflight"
    assert result["control_plane"]["outcome"] == "success"
    assert result["path_policy"]["accepted"] is True


@pytest.mark.parametrize("result", [{}, {"success": "yes"}, {"success": 1}])
def test_metrics_require_explicit_boolean_success(result):
    metrics = ControlPlaneMetrics(window_size=8)

    metrics.record("callback", 0.1, result)

    assert metrics.summary("callback")["outcomes"] == {
        "success": 0,
        "busy": 0,
        "timeout": 0,
        "error": 1,
    }


def test_public_solver_preflight_worker_exception_is_transported(monkeypatch):
    class OwnershipStub:
        def preflight(self, **_kwargs):
            raise RuntimeError("bounded preflight failure")

    monkeypatch.setattr(ownership_module, "ownership_manager", OwnershipStub())
    tool = _profiled_preflight_server()._tool_manager._tools["solver_preflight"]

    async def exercise():
        with pytest.raises(ToolError, match="bounded preflight failure"):
            await asyncio.wait_for(tool.run({}), timeout=2.0)

    asyncio.run(exercise())


def test_lightweight_job_summaries_avoid_campaign_imports(tmp_path):
    jobs_root = tmp_path / "jobs"
    assert ownership_module._lightweight_job_summaries(jobs_root) == {
        "available": True,
        "root": str(jobs_root),
        "count_returned": 0,
        "active_count": 0,
        "active": [],
        "recent": [],
    }

    active = jobs_root / "active-job"
    active.mkdir(parents=True)
    (active / "state.json").write_text(
        json.dumps(
            {
                "status": "running",
                "attempt": 2,
                "progress": {"completed": 3},
                "worker_pid": 123,
                "updated_at_epoch": 5.0,
                "last_error": None,
            }
        ),
        encoding="utf-8",
    )
    malformed = jobs_root / "malformed-job"
    malformed.mkdir()
    (malformed / "state.json").write_text("not-json", encoding="utf-8")

    result = ownership_module._lightweight_job_summaries(jobs_root)

    assert result["count_returned"] == 2
    assert result["active_count"] == 1
    assert result["active"][0]["job_id"] == "active-job"
    unreadable = next(item for item in result["recent"] if item["status"] == "unreadable")
    assert unreadable["job_id"] == "malformed-job"
    assert unreadable["error"].startswith("JSONDecodeError:")


def test_lightweight_job_summaries_find_active_jobs_older_than_recent_limit(tmp_path):
    jobs_root = tmp_path / "jobs"
    active = jobs_root / "active-oldest"
    active.mkdir(parents=True)
    (active / "state.json").write_text(
        json.dumps({"status": "running", "updated_at_epoch": 1.0}),
        encoding="utf-8",
    )
    for index in range(21):
        completed = jobs_root / f"completed-{index:02d}"
        completed.mkdir()
        state_path = completed / "state.json"
        state_path.write_text(
            json.dumps({"status": "completed", "updated_at_epoch": index + 2.0}),
            encoding="utf-8",
        )
        state_path.touch()

    result = ownership_module._lightweight_job_summaries(jobs_root, limit=20)

    assert result["count_returned"] == 20
    assert len(result["recent"]) == 20
    assert result["active_count"] == 1
    assert [item["job_id"] for item in result["active"]] == ["active-oldest"]


def test_standard_library_comsol_discovery_matches_mph_windows_shape(
    tmp_path, monkeypatch
):
    import winreg

    root = tmp_path / "Multiphysics"
    binary = root / "bin" / "win64"
    jvm = root / "java" / "win64" / "jre" / "bin" / "server" / "jvm.dll"
    binary.mkdir(parents=True)
    jvm.parent.mkdir(parents=True)
    for path in (binary / "comsol.exe", binary / "comsolmphserver.exe", jvm):
        path.touch()
    (root / "plugins").mkdir()
    (root / "apiplugins").mkdir()
    (binary / "comsol.ini").write_text(
        "-vm\n../../java/win64/jre/bin/server/jvm.dll\n",
        encoding="utf-8",
    )

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    main_key = Key()
    child_key = Key()

    def open_key(parent, name):
        if parent == winreg.HKEY_LOCAL_MACHINE:
            assert name == r"SOFTWARE\Comsol"
            return main_key
        assert parent is main_key and name == "Comsol64"
        return child_key

    def enum_key(key, index):
        assert key is main_key
        if index == 0:
            return "Comsol64"
        raise OSError("end")

    monkeypatch.setattr(winreg, "OpenKey", open_key)
    monkeypatch.setattr(winreg, "EnumKey", enum_key)
    monkeypatch.setattr(winreg, "QueryValueEx", lambda key, name: (str(root), 1))
    monkeypatch.setattr(ownership_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(ownership_module.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(ownership_module.platform, "architecture", lambda: ("64bit", "WindowsPE"))
    monkeypatch.setattr(ownership_module.importlib.metadata, "version", lambda _name: "1.3.1")
    monkeypatch.setattr(
        ownership_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="COMSOL Multiphysics 6.4.0.293\n", stderr=""
        ),
    )

    version, backends, error = ownership_module._discover_comsol_backends_without_mph()

    assert version == "1.3.1"
    assert error is None
    assert backends == [{"name": "6.4", "root": str(root), "jvm": str(jvm)}]
