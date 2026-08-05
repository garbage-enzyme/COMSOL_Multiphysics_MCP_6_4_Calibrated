"""Durable operation-arbitration and responsive-control regression tests."""

from __future__ import annotations

import inspect
import json
import threading

import psutil
import src.operation_arbiter as arbiter_module
from src.operation_arbiter import OperationArbiter, get_operation_status, guard_tool_call
from src.tools.catalog import TOOL_METADATA


def test_concurrent_comsol_bound_calls_fail_fast_with_retry_evidence(tmp_path, monkeypatch):
    arbiter = OperationArbiter(
        tmp_path,
        pid=100,
        process_create_time=10.0,
        process_probe=lambda pid: 10.0,
        clock=lambda: 20.0,
    )
    monkeypatch.setattr("src.operation_arbiter.get_operation_arbiter", lambda: arbiter)
    entered = threading.Event()
    release = threading.Event()

    def blocking_call():
        entered.set()
        assert release.wait(2.0)
        return {"success": True, "value": "first"}

    first_tool = guard_tool_call(
        blocking_call,
        tool_name="study_solve",
        side_effect_class="solver_execution",
        concurrency_class="comsol_bound",
    )
    second_tool = guard_tool_call(
        lambda: {"success": True, "value": "second"},
        tool_name="param_set",
        side_effect_class="model_mutation",
        concurrency_class="comsol_bound",
    )
    first_result = {}
    thread_errors = []

    def run_first():
        try:
            first_result.update(first_tool())
        except BaseException as exc:
            thread_errors.append(exc)

    thread = threading.Thread(target=run_first)
    thread.start()
    try:
        assert entered.wait(1.0)
        busy = second_tool()
    finally:
        release.set()
        thread.join(2.0)
        assert not thread.is_alive()
    if thread_errors:
        raise thread_errors[0]

    assert busy["success"] is False
    assert busy["operation_gate"]["state"] == "active"
    assert busy["operation_gate"]["retryable"] is True
    assert busy["operation_gate"]["active_operation"]["tool_name"] == "study_solve"
    assert first_result["success"] is True
    assert first_result["operation_gate"]["release"]["verified"] is True
    assert not arbiter.lock_path.exists()


def test_control_plane_call_remains_responsive_while_solver_call_blocks(
    tmp_path, monkeypatch
):
    arbiter = OperationArbiter(
        tmp_path,
        pid=100,
        process_create_time=10.0,
        process_probe=lambda pid: 10.0,
    )
    monkeypatch.setattr("src.operation_arbiter.get_operation_arbiter", lambda: arbiter)
    claim, _ = arbiter.try_acquire(
        tool_name="study_solve", side_effect_class="solver_execution"
    )
    assert claim is not None
    called = []
    status = guard_tool_call(
        lambda: called.append(True) or {"success": True},
        tool_name="solver_status",
        side_effect_class="read_only",
        concurrency_class="control_plane",
    )

    assert status()["success"] is True
    assert called == [True]
    assert arbiter.release(claim)["verified"] is True


def test_solver_free_status_reports_active_shared_operation(tmp_path, monkeypatch):
    arbiter = OperationArbiter(
        tmp_path,
        pid=100,
        process_create_time=10.0,
        process_probe=lambda pid: 10.0,
        clock=lambda: 20.0,
    )
    monkeypatch.setattr("src.operation_arbiter.get_operation_arbiter", lambda: arbiter)
    claim, _ = arbiter.try_acquire(
        tool_name="shared_model_snapshot",
        side_effect_class="filesystem_write",
    )
    assert claim is not None

    status = get_operation_status()

    assert status == {
        "state": "active",
        "retryable": True,
        "retry_after_ms": 250,
        "reason": "recorded process identity is active",
        "active_operation": {
            "operation_id": claim.operation_id,
            "tool_name": "shared_model_snapshot",
            "side_effect_class": "filesystem_write",
            "pid": 100,
            "process_create_time": 10.0,
            "acquired_at_epoch": 20.0,
        },
    }
    assert arbiter.lock_path.exists()
    assert arbiter.release(claim)["verified"] is True


def test_status_does_not_recover_stale_or_malformed_lock(tmp_path):
    missing_pid = 999_999_991

    def probe(pid):
        raise psutil.NoSuchProcess(pid)

    stale = {
        "schema_name": "comsol_mcp.operation_lock",
        "schema_version": "1.0.0",
        "operation_id": "old-operation",
        "tool_name": "shared_model_snapshot",
        "side_effect_class": "filesystem_write",
        "pid": missing_pid,
        "process_create_time": 1.0,
        "acquired_at_epoch": 2.0,
    }
    lock_path = tmp_path / "operation.lock"
    lock_path.write_text(
        json.dumps(stale, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    arbiter = OperationArbiter(
        tmp_path,
        pid=200,
        process_create_time=20.0,
        process_probe=probe,
    )

    assert arbiter.inspect()["state"] == "stale"
    assert lock_path.exists()
    lock_path.write_bytes(b"not-json")
    uncertain = arbiter.inspect()
    assert uncertain["state"] == "uncertain"
    assert uncertain["retryable"] is False
    assert lock_path.read_bytes() == b"not-json"


def test_stale_lock_is_recovered_after_coordinator_restart(tmp_path):
    missing_pid = 999_999_991

    def probe(pid):
        if pid == missing_pid:
            raise psutil.NoSuchProcess(pid)
        return 20.0

    stale = {
        "schema_name": "comsol_mcp.operation_lock",
        "schema_version": "1.0.0",
        "operation_id": "old-operation",
        "tool_name": "param_set",
        "side_effect_class": "model_mutation",
        "pid": missing_pid,
        "process_create_time": 1.0,
        "acquired_at_epoch": 2.0,
    }
    (tmp_path / "operation.lock").write_text(
        json.dumps(stale, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    arbiter = OperationArbiter(
        tmp_path,
        pid=200,
        process_create_time=20.0,
        process_probe=probe,
    )

    claim, evidence = arbiter.try_acquire(
        tool_name="study_solve", side_effect_class="solver_execution"
    )

    assert claim is not None
    assert evidence["state"] == "acquired"
    assert evidence["recovered_stale_lock"] is True
    assert arbiter.release(claim)["verified"] is True


def test_malformed_lock_fails_closed(tmp_path):
    (tmp_path / "operation.lock").write_bytes(b"not-json")
    arbiter = OperationArbiter(
        tmp_path,
        pid=200,
        process_create_time=20.0,
        process_probe=lambda pid: 20.0,
    )

    claim, evidence = arbiter.try_acquire(
        tool_name="study_solve", side_effect_class="solver_execution"
    )

    assert claim is None
    assert evidence["state"] == "uncertain"
    assert evidence["retryable"] is True
    assert evidence["retry_after_ms"] == 250
    assert (tmp_path / "operation.lock").read_bytes() == b"not-json"


def test_lock_close_failure_returns_uncertain_and_removes_owned_partial(tmp_path, monkeypatch):
    arbiter = OperationArbiter(
        tmp_path,
        pid=200,
        process_create_time=20.0,
        process_probe=lambda _pid: 20.0,
    )
    real_close = arbiter_module.os.close
    calls = 0

    def fail_first_close(descriptor):
        nonlocal calls
        calls += 1
        if calls == 1:
            real_close(descriptor)
            raise OSError("simulated close uncertainty")
        return real_close(descriptor)

    monkeypatch.setattr(arbiter_module.os, "close", fail_first_close)

    claim, evidence = arbiter.try_acquire(
        tool_name="study_solve", side_effect_class="solver_execution"
    )

    assert claim is None
    assert evidence["state"] == "uncertain"
    assert "OSError" in evidence["error"]
    assert not arbiter.lock_path.exists()


def test_operation_lock_retries_short_writes_until_publication_is_exact(
    tmp_path, monkeypatch
):
    arbiter = OperationArbiter(
        tmp_path,
        pid=201,
        process_create_time=20.1,
        process_probe=lambda _pid: 20.1,
    )
    original_write = arbiter_module.os.write
    writes = []

    def short_write(descriptor, payload):
        writes.append(len(payload))
        return original_write(descriptor, payload[:1])

    monkeypatch.setattr(arbiter_module.os, "write", short_write)

    claim, evidence = arbiter.try_acquire(
        tool_name="study_solve", side_effect_class="solver_execution"
    )

    assert claim is not None
    assert evidence["state"] == "acquired"
    assert len(writes) == len(claim.lock_bytes)
    assert arbiter.lock_path.read_bytes() == claim.lock_bytes
    assert arbiter.release(claim)["verified"] is True


def test_non_mapping_result_becomes_failure_when_release_is_unverified(
    tmp_path, monkeypatch
):
    arbiter = OperationArbiter(
        tmp_path,
        pid=202,
        process_create_time=20.2,
        process_probe=lambda _pid: 20.2,
    )
    monkeypatch.setattr(arbiter_module, "get_operation_arbiter", lambda: arbiter)
    monkeypatch.setattr(
        arbiter,
        "release",
        lambda _claim: {
            "released": False,
            "verified": False,
            "reason": "simulated_unverified_release",
        },
    )
    guarded = guard_tool_call(
        lambda: "raw-result",
        tool_name="study_solve",
        side_effect_class="solver_execution",
        concurrency_class="comsol_bound",
    )

    result = guarded()

    assert result["success"] is False
    assert result["result"] == "raw-result"
    assert result["operation_gate"]["release"]["verified"] is False


def test_cached_arbiter_is_rebound_after_process_identity_change(tmp_path, monkeypatch):
    original_arbiters = dict(arbiter_module._ARBITERS)
    process = {"pid": 301}
    monkeypatch.setattr(arbiter_module, "default_runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(arbiter_module.os, "getpid", lambda: process["pid"])
    monkeypatch.setattr(
        arbiter_module,
        "_process_create_time",
        lambda pid: {301: 30.1, 302: 30.2}[pid],
    )
    try:
        arbiter_module._ARBITERS.clear()
        first = arbiter_module.get_operation_arbiter()
        repeated = arbiter_module.get_operation_arbiter()
        process["pid"] = 302
        rebound = arbiter_module.get_operation_arbiter()
    finally:
        arbiter_module._ARBITERS.clear()
        arbiter_module._ARBITERS.update(original_arbiters)

    assert repeated is first
    assert rebound is not first
    assert rebound.pid == 302
    assert rebound.process_create_time == 30.2


def test_metadata_keeps_required_tools_outside_comsol_mutex():
    for name in (
        "capabilities", "solver_status", "job_status", "job_cancel",
        "manual_search", "manual_read_pages", "spectral_characterize",
        "convergence_evaluate", "branch_continuation_plan",
    ):
        assert TOOL_METADATA[name].concurrency_class != "comsol_bound"
    assert TOOL_METADATA["study_solve"].concurrency_class == "comsol_bound"
    assert TOOL_METADATA["param_set"].concurrency_class == "comsol_bound"


def test_expected_model_revision_is_checked_after_lock_and_advances(
    tmp_path, monkeypatch
):
    from src.tools.session import session_manager

    class FakeModel:
        def name(self):
            return "model"

        def file(self):
            return None

    original = {
        "models": session_manager._models,
        "paths": session_manager._model_paths,
        "revisions": session_manager._model_revisions,
        "current": session_manager._current_model,
    }
    session_manager._models = {"model": FakeModel()}
    session_manager._model_paths = {}
    session_manager._model_revisions = {}
    session_manager._current_model = "model"
    initial = session_manager.get_model_revision("model")
    arbiter = OperationArbiter(
        tmp_path,
        pid=100,
        process_create_time=10.0,
        process_probe=lambda pid: 10.0,
    )
    monkeypatch.setattr("src.operation_arbiter.get_operation_arbiter", lambda: arbiter)
    calls = []
    revision_checks = []
    original_get_model_revision = session_manager.get_model_revision

    def get_model_revision_after_lock(model_name):
        assert arbiter.lock_path.is_file()
        revision_checks.append(model_name)
        return original_get_model_revision(model_name)

    monkeypatch.setattr(
        session_manager, "get_model_revision", get_model_revision_after_lock
    )

    def param_set(name: str, value: str, model_name: str | None = None):
        calls.append((name, value, model_name))
        return {"success": True}

    guarded = guard_tool_call(
        param_set,
        tool_name="param_set",
        side_effect_class="model_mutation",
        concurrency_class="comsol_bound",
        profile_name="core",
        requires_model_revision=True,
        advances_model_revision=True,
    )
    try:
        assert "expected_model_revision" in inspect.signature(guarded).parameters
        stale = guarded(
            "p", "1", expected_model_revision="0" * 64
        )
        assert stale["success"] is False
        assert calls == []
        accepted = guarded(
            "p", "1", expected_model_revision=initial["revision_sha256"]
        )
        assert accepted["success"] is True
        assert calls == [("p", "1", None)]
        assert accepted["model_revision"]["sequence"] == 1
        assert accepted["model_revision"]["revision_sha256"] != initial[
            "revision_sha256"
        ]
        replay = guarded(
            "p", "2", expected_model_revision=initial["revision_sha256"]
        )
        assert replay["success"] is False
        assert calls == [("p", "1", None)]
        assert revision_checks == ["model"] * 4
    finally:
        session_manager._models = original["models"]
        session_manager._model_paths = original["paths"]
        session_manager._model_revisions = original["revisions"]
        session_manager._current_model = original["current"]


def test_full_profile_tracks_revision_without_enforcing_expected_token(
    tmp_path, monkeypatch
):
    from src.tools.session import session_manager

    class FakeModel:
        pass

    original = (
        session_manager._models,
        session_manager._model_paths,
        session_manager._model_revisions,
        session_manager._current_model,
    )
    session_manager._models = {"model": FakeModel()}
    session_manager._model_paths = {}
    session_manager._model_revisions = {}
    session_manager._current_model = "model"
    arbiter = OperationArbiter(
        tmp_path,
        pid=100,
        process_create_time=10.0,
        process_probe=lambda pid: 10.0,
    )
    monkeypatch.setattr("src.operation_arbiter.get_operation_arbiter", lambda: arbiter)
    guarded = guard_tool_call(
        lambda model_name=None: {"success": True},
        tool_name="param_set",
        side_effect_class="model_mutation",
        concurrency_class="comsol_bound",
        profile_name="full",
        requires_model_revision=True,
        advances_model_revision=True,
    )
    try:
        result = guarded()
        assert result["success"] is True
        assert result["model_revision"]["sequence"] == 1
    finally:
        (
            session_manager._models,
            session_manager._model_paths,
            session_manager._model_revisions,
            session_manager._current_model,
        ) = original
