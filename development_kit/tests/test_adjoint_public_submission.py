"""Public dispatch preserves the native adjoint execution entry point."""

import asyncio
from types import SimpleNamespace

from comsol_mcp.server import create_server
from comsol_mcp.tools import jobs
from development_kit.tests.mcp_test_support import decode_tool_result
from development_kit.tests.test_adjoint_optimization import _write_manifest


def test_native_adjoint_submission_reaches_manager(monkeypatch, ascii_tmp_path):
    monkeypatch.setattr("comsol_mcp.platform_support.native_solver_enabled", lambda: True)
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    received = []

    def submit(spec):
        received.append(spec)
        return {"success": True, "job_id": "dispatch-only"}

    monkeypatch.setattr(jobs, "job_manager", SimpleNamespace(submit=submit))
    server = create_server("native-adjoint-dispatch", profile="core")
    result = decode_tool_result(asyncio.run(server.call_tool("job_submit", {"spec": envelope})))
    assert result["job_id"] == "dispatch-only"
    assert received == [envelope]


def test_native_adjoint_preview_refuses_changed_manifest(ascii_tmp_path):
    envelope, _, manifest = _write_manifest(ascii_tmp_path)
    server = create_server("native-adjoint-preview", profile="core")
    result = decode_tool_result(
        asyncio.run(server.call_tool("job_spec_preview", {"spec": envelope}))
    )
    assert result["success"] is True
    assert result["preview_guarantees"]["solver_started"] is False
    manifest.write_bytes(manifest.read_bytes() + b" ")
    result = decode_tool_result(
        asyncio.run(server.call_tool("job_spec_preview", {"spec": envelope}))
    )
    assert result["success"] is False
