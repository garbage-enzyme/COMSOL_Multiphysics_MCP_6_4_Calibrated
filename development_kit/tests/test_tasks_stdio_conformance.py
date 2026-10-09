"""Independent real-wire Tasks recovery with explicit completion barriers."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from comsol_mcp.jobs.tasks_bridge import TasksMappingStore
from comsol_mcp.protocol_identity import TASKS_EXTENSION_IDENTIFIER
from development_kit.tests.test_mcp_sdk2_compatibility import _read_response, _write_message

ROOT = Path(__file__).parents[2]
META = {
    "io.modelcontextprotocol/clientCapabilities": {"extensions": {TASKS_EXTENSION_IDENTIFIER: {}}}
}


async def _start(root, owner="owner-a", profile="core", admit_native=True):
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("COMSOL_MCP_")
    }
    environment.pop("PYTHONPATH", None)
    environment.update(
        COMSOL_MCP_RUNTIME_DIR=str(root / "runtime"),
        COMSOL_MCP_MODEL_READ_ROOTS=str(root),
        COMSOL_MCP_ARTIFACT_WRITE_ROOT=str(root / "artifacts"),
        COMSOL_MCP_OWNER=owner,
        TASKS_TEST_ENGINE_ROOT=str(root / "engine"),
        TASKS_TEST_PROFILE=profile,
        TASKS_TEST_ADMIT_NATIVE=str(admit_native).lower(),
        COMSOL_MCP_SETTINGS_PATH=str(ROOT / "settings.json"),
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "development_kit.tests.tasks_stdio_fixture",
        cwd=ROOT,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=512 * 1024,
    )
    stderr = asyncio.create_task(process.stderr.read())
    try:
        listed = await _request(process, 0, "tools/list", {})
        assert "result" in listed, listed
        assert listed["result"]["tools"]
    except BaseException:
        await _stop(process, stderr)
        raise
    return process, stderr


async def _request(process, request_id, method, params):
    params = dict(params)
    params["_meta"] = {
        "io.modelcontextprotocol/clientCapabilities": {},
        **params.get("_meta", {}),
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    }
    await _write_message(
        process, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    )
    return await _read_response(process, request_id)


async def _stop(process, stderr):
    process.stdin.close()
    await process.stdin.wait_closed()
    try:
        await asyncio.wait_for(process.wait(), timeout=15)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    raw = (await stderr).decode(errors="replace")
    assert process.returncode == 0, raw


@pytest.mark.parametrize("request_cancel", [False, True])
@pytest.mark.parametrize("ttl_ms", [None, 0])
def test_real_wire_acknowledges_before_barrier_and_recovers_after_restart(
    ascii_tmp_path, request_cancel, ttl_ms
):
    root = ascii_tmp_path / "wire"
    (root / "engine").mkdir(parents=True)
    (root / "source.mph").write_bytes(b"fixed test-only model placeholder")
    arguments = {
        "spec": {
            "job_type": "staged_sweep",
            "source_model_path": str(root / "source.mph"),
            "parameter_name": "wl",
            "parameter_values": [5.0],
            "parameter_unit": "um",
            "expressions": ["ewfd.Rtotal"],
        }
    }

    meta = {
        "io.modelcontextprotocol/clientCapabilities": {
            "extensions": {TASKS_EXTENSION_IDENTIFIER: {"ttlMs": ttl_ms}}
        }
    }

    async def exercise():
        process, stderr = await _start(root)
        try:
            submit = await _request(
                process,
                1,
                "tools/call",
                {"name": "job_submit", "arguments": arguments, "_meta": meta},
            )
            assert "result" in submit, submit
            handle = submit["result"]
            assert handle["resultType"] == "task", submit
            assert handle["status"] == "working"
            assert handle["ttlMs"] == ttl_ms
            task_id = handle["taskId"]
            row = TasksMappingStore(root / "runtime/tasks").latest_for_task(task_id)
            assert row is not None and row.owner == "owner-a"
            assert not (root / "engine" / (row.job_id + ".release")).exists()
            retry = await _request(
                process,
                2,
                "tools/call",
                {"name": "job_submit", "arguments": arguments, "_meta": meta},
            )
            assert retry["result"]["taskId"] == task_id
            assert (root / "engine/submission_count").read_text() == "1"
            # Every request must opt in. A previous declaration grants nothing.
            missing = await _request(process, 3, "tasks/get", {"taskId": task_id})
            assert missing["error"]["code"] == -32021
        finally:
            await _stop(process, stderr)

        process, stderr = await _start(root)
        try:
            detail = await _request(process, 1, "tasks/get", {"taskId": task_id, "_meta": meta})
            assert detail["result"]["status"] == "working"
            if request_cancel:
                cancel = await _request(
                    process, 2, "tasks/cancel", {"taskId": task_id, "_meta": meta}
                )
                assert cancel["result"]["cancellationRequested"] is True
                assert cancel["result"]["cleanupPending"] is True
            detail = await _request(process, 3, "tasks/get", {"taskId": task_id, "_meta": meta})
            assert detail["result"]["status"] == "working"
            (root / "engine" / (row.job_id + ".release")).write_bytes(b"released")
            final = await _request(process, 4, "tasks/get", {"taskId": task_id, "_meta": meta})
            assert final["result"]["status"] == ("cancelled" if request_cancel else "completed")
            if not request_cancel:
                assert final["result"]["result"]["isError"] is False
                assert final["result"]["result"]["structuredContent"]["status"] == "completed"
            assert TasksMappingStore(root / "runtime/tasks").latest_for_task(task_id) is not None
            assert (root / "engine/submission_count").read_text() == "1"
        finally:
            await _stop(process, stderr)

        process, stderr = await _start(root, owner="owner-b")
        try:
            foreign = await _request(process, 1, "tasks/get", {"taskId": task_id, "_meta": meta})
            unknown = await _request(process, 2, "tasks/get", {"taskId": "unknown", "_meta": meta})
            assert foreign["error"]["code"] == unknown["error"]["code"] == -32602
            assert foreign["error"]["message"] == (
                unknown["error"]["message"].replace("'unknown'", repr(task_id))
            )
        finally:
            await _stop(process, stderr)

    asyncio.run(exercise())


def test_real_wire_submission_cannot_bypass_public_guards(ascii_tmp_path):
    root = ascii_tmp_path / "negative-wire"
    (root / "engine").mkdir(parents=True)
    source = root / "source.mph"
    source.write_bytes(b"fixed placeholder")
    spec = {
        "job_type": "staged_sweep",
        "source_model_path": str(source),
        "parameter_name": "wl",
        "parameter_values": [5.0],
        "expressions": ["1"],
    }
    cases = [
        ("core", True, {"spec": {**spec, "unknown": True}}),
        ("core", True, {"spec": spec, "unknown": True}),
        ("core", True, spec),  # A flat engine payload is not tools/call arguments.
        ("core", True, {"spec": {**spec, "source_model_path": str(root.parent / "foreign.mph")}}),
        ("comsolless_read_only", True, {"spec": spec}),
        ("core", False, {"spec": spec}),
    ]

    async def exercise():
        for profile, native, arguments in cases:
            process, stderr = await _start(root, profile=profile, admit_native=native)
            try:
                response = await _request(
                    process,
                    1,
                    "tools/call",
                    {"name": "job_submit", "arguments": arguments, "_meta": META},
                )
                assert response["error"]["code"] == -32602, response
                assert not list((root / "engine").iterdir())
                assert not (root / "runtime/tasks/tasks.jsonl").exists()
                assert source.read_bytes() == b"fixed placeholder"
            finally:
                await _stop(process, stderr)

    asyncio.run(exercise())
