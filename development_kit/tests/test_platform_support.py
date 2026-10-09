"""Independent platform refusal and cold-import regression checks."""

import asyncio
import inspect
import os

import pytest

from comsol_mcp import platform_support as platform


def test_refusal_precedes_every_native_tool_body(monkeypatch):
    from comsol_mcp.tools.catalog import TOOL_METADATA

    monkeypatch.setattr(platform, "native_solver_enabled", lambda: False)
    called = []

    def native_body():
        called.append(True)
        raise AssertionError("native body must not run")

    rejected = []
    for name, spec in TOOL_METADATA.items():
        if spec.concurrency_class in {"solver_free", "control_plane"}:
            continue
        guarded = platform.guard_platform_call(
            native_body, tool_name=name, concurrency_class=spec.concurrency_class
        )
        result = guarded()
        assert result["reason_code"] == "linux_solver_not_enabled", name
        assert result["solver_started"] is False
        assert result["filesystem_modified"] is False
        rejected.append(name)
    assert "comsol_start" in rejected
    assert "job_submit" in rejected
    assert "model_create" in rejected
    assert not called


def test_async_platform_guard_preserves_signature_and_refuses(monkeypatch):
    monkeypatch.setattr(platform, "native_solver_enabled", lambda: False)

    async def native(value: str) -> dict:
        raise AssertionError("native body must not run")

    guarded = platform.guard_platform_call(
        native, tool_name="comsol_start", concurrency_class="solver_exclusive"
    )
    assert inspect.signature(guarded) == inspect.signature(native)
    assert asyncio.run(guarded("ignored"))["reason_code"] == "linux_solver_not_enabled"


def test_offline_body_still_executes(monkeypatch):
    monkeypatch.setattr(platform, "native_solver_enabled", lambda: False)
    guarded = platform.guard_platform_call(
        lambda value: value + 1, tool_name="mph_inspect", concurrency_class="solver_free"
    )
    assert guarded(7) == 8


def test_linux_production_tasks_submission_never_loads_manager(monkeypatch):
    from comsol_mcp.tools.jobs import _LazyJobManager

    monkeypatch.setattr(platform, "native_solver_enabled", lambda: False)
    manager = _LazyJobManager()
    for operation, value in ((manager.submit, {}), (manager.resume, "missing")):
        with pytest.raises(RuntimeError, match="^linux_solver_not_enabled$"):
            operation(value)
    assert manager._manager is None


def test_windows_call_behavior_is_preserved(monkeypatch):
    monkeypatch.setattr(platform, "native_solver_enabled", lambda: True)
    guarded = platform.guard_platform_call(
        lambda: {"original": True}, tool_name="comsol_start", concurrency_class="solver_exclusive"
    )
    assert guarded() == {"original": True}


@pytest.mark.skipif(os.name == "nt", reason="Linux production ownership mutation refusal")
def test_linux_real_solver_lease_mutation_is_refused_before_artifacts(tmp_path):
    from comsol_mcp.tools.ownership import _lease_operation_lock

    target = tmp_path / "runtime" / "lease.lock"
    with pytest.raises(RuntimeError, match="requires supported Windows locking"):
        with _lease_operation_lock(target):
            raise AssertionError("production lease mutation must not run")
    assert not target.parent.exists()


def test_registered_linux_preflight_is_refused_before_callback(monkeypatch):
    from comsol_mcp.server import create_server
    from comsol_mcp.tools import ownership
    from development_kit.tests.mcp_test_support import decode_tool_result

    monkeypatch.setattr(platform, "native_solver_enabled", lambda: False)

    class Forbidden:
        def preflight(self, **kwargs):
            raise AssertionError("Linux must not probe Windows solver readiness")

    monkeypatch.setattr(ownership, "ownership_manager", Forbidden())
    server = create_server(profile="core")
    result = decode_tool_result(asyncio.run(server.call_tool("solver_preflight", {})))
    assert result["classification"] == "unsupported_platform"
    assert result["solver_started"] is False
    assert result["filesystem_modified"] is False


@pytest.mark.skipif(os.name == "nt", reason="Linux cold native-import refusal")
def test_fresh_linux_server_never_imports_native_solver_modules():
    import subprocess
    import sys
    from pathlib import Path

    code = """
import asyncio, importlib.abc, sys
class DenyNative(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'mph', 'jpype'}:
            raise AssertionError('native solver import: ' + fullname)
sys.meta_path.insert(0, DenyNative())
from comsol_mcp.server import create_server, _preload_native_runtime
from development_kit.tests.mcp_test_support import decode_tool_result
receipt = _preload_native_runtime()
assert not {'mph', 'jpype'} & receipt.keys()
server = create_server(profile='core')
for name in ('comsol_start', 'solver_preflight'):
    result = decode_tool_result(asyncio.run(server.call_tool(name, {})))
    assert result['reason_code'] == 'linux_solver_not_enabled'
    assert not result['solver_started']
    assert not result['filesystem_modified']
assert not {'mph', 'jpype'} & sys.modules.keys()
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
