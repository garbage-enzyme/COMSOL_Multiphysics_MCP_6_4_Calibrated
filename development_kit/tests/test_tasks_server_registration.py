"""Real-server Tasks registration, advertisement, and dispatch-boundary tests.

The mapping unit tests prove the contract in isolation. These tests prove the
same contract through a *real* :class:`~mcp.server.mcpserver.MCPServer` built by
the production factory, so a regression in registration or advertisement cannot
pass unnoticed. Nothing here starts COMSOL: the durable manager is lazy and no
solver-bound tool is called.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from comsol_mcp.jobs.tasks_extension import (
    REMOVED_TASKS_METHODS,
    TASKS_METHOD_CANCEL,
    TASKS_METHOD_GET,
    TASKS_METHOD_UPDATE,
)
from comsol_mcp.protocol_identity import TASKS_EXTENSION_IDENTIFIER
from comsol_mcp.server import create_server

EXISTING_TOOL_COUNT = 56


def test_the_real_server_advertises_the_tasks_extension() -> None:
    server = create_server("tasks-advertisement", profile="core")
    advertised = server._lowlevel_server.extensions
    assert TASKS_EXTENSION_IDENTIFIER in advertised
    # No extension-specific settings are defined by the specification.
    assert advertised[TASKS_EXTENSION_IDENTIFIER] == {}


def test_registering_the_extension_does_not_change_the_tool_surface() -> None:
    """The extension is additive; it must not add or remove any tool."""
    server = create_server("tasks-tool-surface", profile="core")
    tools = asyncio.run(server.list_tools())
    assert len(tools) == EXISTING_TOOL_COUNT
    names = {tool.name for tool in tools}
    assert {"job_submit", "job_status", "job_tail", "job_cancel", "job_resume"} <= names


@pytest.mark.parametrize("method", [TASKS_METHOD_GET, TASKS_METHOD_CANCEL, TASKS_METHOD_UPDATE])
def test_the_extension_methods_are_registered_on_the_lowlevel_server(method: str) -> None:
    server = create_server("tasks-methods", profile="core")
    assert server._lowlevel_server.get_request_handler(method) is not None


@pytest.mark.parametrize("method", REMOVED_TASKS_METHODS)
def test_the_removed_methods_are_not_registered(method: str) -> None:
    """The redesign deleted these; registering them would be a wire regression."""
    server = create_server("tasks-removed", profile="core")
    assert server._lowlevel_server.get_request_handler(method) is None


def test_the_location_free_profile_still_builds_with_the_extension() -> None:
    """Cold discovery must stay solver-free and import-light."""
    server = create_server("tasks-offline", profile="comsolless_read_only")
    tools = asyncio.run(server.list_tools())
    assert len(tools) == 5
    assert TASKS_EXTENSION_IDENTIFIER in server._lowlevel_server.extensions


def test_server_capabilities_carry_the_extension_for_modern_clients() -> None:
    server = create_server("tasks-capabilities", profile="core")
    capabilities = server._lowlevel_server.get_capabilities(
        extensions=dict(server._lowlevel_server.extensions)
    )
    assert capabilities.extensions == {TASKS_EXTENSION_IDENTIFIER: {}}


def test_an_ordinary_tool_call_result_is_never_task_shaped() -> None:
    """A client that does not opt in must receive the ordinary result shape."""
    server = create_server("tasks-ordinary", profile="core")
    result = asyncio.run(server.call_tool("capabilities", {}))
    serialized = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert serialized.get("resultType") != "task"
    assert "taskId" not in serialized


def test_the_extension_binds_are_additive_and_cannot_shadow_spec_methods() -> None:
    """The extension must not attempt to replace a spec-defined method."""
    from mcp.server.extension import SPEC_CLIENT_METHODS

    bound = {TASKS_METHOD_GET, TASKS_METHOD_CANCEL, TASKS_METHOD_UPDATE}
    assert not bound & set(SPEC_CLIENT_METHODS)
    assert "tools/call" in SPEC_CLIENT_METHODS


def test_the_extension_import_does_not_pull_in_a_comsol_client() -> None:
    """Importing the adapter must not import mph, jpype, or start a JVM."""
    import subprocess
    import sys

    code = (
        "import sys;"
        "import comsol_mcp.jobs.tasks_extension;"
        "import comsol_mcp.jobs.tasks_bridge;"
        "leaked=[m for m in ('mph','jpype','jpype1','com.comsol') if m in sys.modules];"
        "print('LEAKED=' + ','.join(leaked))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[2]),
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert "LEAKED=" in completed.stdout
    assert completed.stdout.strip().endswith("LEAKED="), completed.stdout


def test_the_owner_binding_is_taken_from_the_configured_identity(tmp_path: Path) -> None:
    """Each task is bound to one owner, taken from the configured identity."""
    from comsol_mcp.jobs.tasks_bridge import TasksBridge, TasksMappingStore
    from comsol_mcp.jobs.tasks_extension import build_tasks_extension

    class Engine:
        def submit(self, spec: dict[str, Any]) -> dict[str, Any]:
            return {"success": True, "job_id": "job-1", "status": "submitted"}

        def status(self, job_id: str) -> dict[str, Any]:
            return {"job_id": job_id, "status": "running"}

        def cancel(self, job_id: str, *, expected_attempt: int | None = None) -> dict[str, Any]:
            return {"job_id": job_id}

    bridge = TasksBridge(
        engine=Engine(), store=TasksMappingStore(tmp_path / "tasks"), owner="owner-x"
    )
    extension = build_tasks_extension(bridge)
    assert extension.identifier == TASKS_EXTENSION_IDENTIFIER
    handle = bridge.submit_task(
        {"job_type": "staged_sweep"},
        client_extensions={TASKS_EXTENSION_IDENTIFIER: {}},
    )
    row = bridge._store.latest_for_task(handle["taskId"])
    assert row is not None and row.owner == "owner-x"
