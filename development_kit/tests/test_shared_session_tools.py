"""Public shared-session profile and adapter tests."""

from __future__ import annotations

import asyncio

from src.server import create_server
from src.shared_session.contracts import SHARED_SERVER_FEATURE_ENV


def test_shared_profile_capabilities_and_tools_are_explicit(monkeypatch):
    monkeypatch.setenv(SHARED_SERVER_FEATURE_ENV, "true")
    server = create_server("shared-tools", profile="desktop_shared")
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    capabilities = server._tool_manager._tools["capabilities"].fn()

    assert {
        "shared_server_preflight", "shared_server_attach",
        "shared_server_detach", "shared_server_status",
        "shared_server_models", "shared_model_lock",
        "shared_model_verify", "shared_model_unlock",
    } <= set(tools)
    assert capabilities["shared_session"] == {
        "profile": "desktop_shared",
        "profile_active": True,
        "feature_flag": SHARED_SERVER_FEATURE_ENV,
        "feature_enabled": True,
        "gate_open": True,
        "maturity": "experimental",
        "endpoint_scope": "local_loopback_only",
        "server_ownership": "external_user_owned",
        "can_start_comsol": False,
        "model_scope": "one_exact_server_model",
        "restart_required_after_change": True,
    }
    assert capabilities["tool_count"] == 13


def test_shared_attach_public_schema_requires_confirmation(monkeypatch):
    monkeypatch.setenv(SHARED_SERVER_FEATURE_ENV, "true")
    server = create_server("shared-schema", profile="desktop_shared")
    schemas = {
        tool.name: tool.inputSchema for tool in asyncio.run(server.list_tools())
    }
    attach = schemas["shared_server_attach"]

    assert set(attach["required"]) == {"host", "port", "model_tag", "user_confirmed"}
    assert attach["properties"]["user_confirmed"]["type"] == "boolean"


def test_shared_status_uses_manager_without_constructing_client(monkeypatch):
    monkeypatch.setenv(SHARED_SERVER_FEATURE_ENV, "true")
    server = create_server("shared-status", profile="desktop_shared")
    import src.tools.shared_session as module

    monkeypatch.setattr(
        module.shared_session_manager,
        "status",
        lambda: {"success": True, "attached": False, "sentinel": "status-only"},
    )
    monkeypatch.setattr(
        module,
        "get_operation_status",
        lambda: {"state": "idle", "active_operation": None},
    )

    result = server._tool_manager._tools["shared_server_status"].fn()

    assert result["sentinel"] == "status-only"
    assert result["operation"]["state"] == "idle"


def test_shared_model_guard_tools_delegate_exact_caller_evidence(monkeypatch):
    monkeypatch.setenv(SHARED_SERVER_FEATURE_ENV, "true")
    server = create_server("shared-model-guards", profile="desktop_shared")
    import src.tools.shared_session as module

    calls = []
    monkeypatch.setattr(
        module.shared_session_manager,
        "models",
        lambda: {"success": True, "models": [], "sentinel": "models"},
    )
    monkeypatch.setattr(
        module.shared_session_manager,
        "lock_model",
        lambda **kwargs: calls.append(("lock", kwargs)) or {"success": True},
    )
    monkeypatch.setattr(
        module.shared_session_manager,
        "verify_model_lock",
        lambda **kwargs: calls.append(("verify", kwargs)) or {"success": True},
    )
    monkeypatch.setattr(
        module.shared_session_manager,
        "unlock_model",
        lambda **kwargs: calls.append(("unlock", kwargs)) or {"success": True},
    )
    tools = server._tool_manager._tools

    assert tools["shared_server_models"].fn()["sentinel"] == "models"
    assert tools["shared_model_lock"].fn(
        "interactive_inspection", None, None
    )["success"] is True
    assert tools["shared_model_verify"].fn("a" * 64, "b" * 64)["success"] is True
    assert tools["shared_model_unlock"].fn("a" * 64, "Desktop turn")["success"] is True
    assert calls == [
        (
            "lock",
            {"collaboration_mode": "interactive_inspection", "immutable_source": None},
        ),
        (
            "verify",
            {
                "expected_lock_sha256": "a" * 64,
                "expected_revision_sha256": "b" * 64,
            },
        ),
        (
            "unlock",
            {"expected_lock_sha256": "a" * 64, "reason": "Desktop turn"},
        ),
    ]
