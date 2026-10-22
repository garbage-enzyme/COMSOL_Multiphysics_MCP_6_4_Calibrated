"""Tasks must bind to the same normalized effective owner as ownership tools."""

from __future__ import annotations

import comsol_mcp.jobs.tasks_bridge as tasks_bridge_module
import comsol_mcp.tools.ownership as ownership_module
import comsol_mcp.utils.runtime_paths as runtime_paths_module
from comsol_mcp.server import _tasks_extensions
from comsol_mcp.tools.ownership import _agent_owner_label


class _FakeProcess:
    def __init__(self, command_line):
        self._command_line = command_line

    def cmdline(self):
        return self._command_line


def test_settings_only_owner_is_the_effective_owner(monkeypatch):
    monkeypatch.delenv("COMSOL_MCP_OWNER", raising=False)
    monkeypatch.setattr(
        ownership_module,
        "settings_environment",
        lambda *args, **kwargs: {"COMSOL_MCP_OWNER": "settings-owner"},
    )

    assert _agent_owner_label() == "settings-owner"


def test_owner_label_derives_from_the_parent_agent_when_unconfigured(monkeypatch):
    monkeypatch.setattr(ownership_module, "settings_environment", lambda *args, **kwargs: {})
    for command, expected in (
        (["C:/tools/opencode/bin/node.exe", "server.js"], "opencode-mcp"),
        (["C:/tools/codex/agent.exe", "--run"], "codex-mcp"),
        (["C:/Python314/python.exe", "-m", "anything"], "comsol-mcp"),
    ):
        monkeypatch.setattr(ownership_module.psutil, "Process", lambda _pid: _FakeProcess(command))
        assert _agent_owner_label() == expected


def test_tasks_extensions_bind_the_normalized_effective_owner(
    monkeypatch, tmp_path, ascii_tmp_path
):
    monkeypatch.setattr(
        ownership_module,
        "settings_environment",
        lambda *args, **kwargs: {"COMSOL_MCP_OWNER": "settings-owner"},
    )
    monkeypatch.setattr(
        runtime_paths_module,
        "default_runtime_dir",
        lambda *args, **kwargs: ascii_tmp_path / "runtime",
    )
    recorded = {}

    class RecordingBridge(tasks_bridge_module.TasksBridge):
        def __init__(self, *, engine, store, owner):
            recorded["owner"] = owner
            super().__init__(engine=engine, store=store, owner=owner)

    monkeypatch.setattr(tasks_bridge_module, "TasksBridge", RecordingBridge)

    extensions = _tasks_extensions(engine=object())

    assert extensions
    assert recorded["owner"] == "settings-owner"
    assert recorded["owner"] == _agent_owner_label()


def test_two_owners_sharing_one_mapping_store_keep_separate_identities(monkeypatch, ascii_tmp_path):
    store_root = ascii_tmp_path / "tasks"
    engine = object()
    first = tasks_bridge_module.TasksBridge(
        engine=engine, store=tasks_bridge_module.TasksMappingStore(store_root), owner="owner-a"
    )
    second = tasks_bridge_module.TasksBridge(
        engine=engine, store=tasks_bridge_module.TasksMappingStore(store_root), owner="owner-b"
    )

    assert first._owner == "owner-a"
    assert second._owner == "owner-b"
