"""Public shared-session profile and adapter tests."""

from __future__ import annotations

import asyncio

import pytest
from src.server import create_server
from src.shared_session.attach_request import normalize_shared_server_attach_request
from src.shared_session.contracts import SHARED_SERVER_FEATURE_ENV
from src.shared_session.lifecycle import SharedSessionManager


def test_shared_profile_capabilities_and_tools_are_explicit(monkeypatch):
    monkeypatch.setenv(SHARED_SERVER_FEATURE_ENV, "true")
    server = create_server("shared-tools", profile="desktop_shared")
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    capabilities = server._tool_manager._tools["capabilities"].fn()

    assert {
        "shared_server_preflight",
        "shared_server_attach",
        "shared_server_detach",
        "shared_server_status",
        "shared_server_models",
        "shared_model_lock",
        "shared_model_verify",
        "shared_model_unlock",
        "shared_model_snapshot",
        "shared_model_adopt",
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
        "durable_execution": {
            "available": True,
            "execution_backend": "attached_shared_server",
            "job_types": ["staged_sweep"],
            "control_tools": [
                "job_spec_preview",
                "job_submit",
                "job_status",
                "job_tail",
                "job_cancel",
                "job_resume",
            ],
            "requires_automation_exclusive_handoff": True,
            "requires_immutable_source": True,
            "checkpoint_save_copy": True,
            "exact_durable_revision_resume": True,
            "external_server_is_termination_target": False,
            "terminal_completion_requires_preservation_receipt": True,
        },
        "restart_required_after_change": True,
    }
    assert capabilities["tool_count"] == 20


def test_shared_attach_public_schema_requires_confirmation(monkeypatch):
    monkeypatch.setenv(SHARED_SERVER_FEATURE_ENV, "true")
    server = create_server("shared-schema", profile="desktop_shared")
    schemas = {tool.name: tool.inputSchema for tool in asyncio.run(server.list_tools())}
    attach = schemas["shared_server_attach"]

    assert set(attach["required"]) == {"host", "port", "user_confirmed"}
    assert attach["properties"]["user_confirmed"]["type"] == "boolean"
    assert set(schemas["shared_model_adopt"]["required"]) == {"model_tag"}


def test_shared_status_uses_manager_without_constructing_client(monkeypatch):
    monkeypatch.setenv(SHARED_SERVER_FEATURE_ENV, "true")
    server = create_server("shared-status", profile="desktop_shared")
    import src.tools.shared_session as module

    client_constructions = []
    manager = SharedSessionManager(
        client_factory=lambda *_args: client_constructions.append(True),
    )
    monkeypatch.setattr(module, "shared_session_manager", manager)
    monkeypatch.setattr(
        module,
        "get_operation_status",
        lambda: {"state": "idle", "active_operation": None},
    )

    result = server._tool_manager._tools["shared_server_status"].fn()

    assert result["success"] is True
    assert result["state"] == "detached"
    assert result["attached"] is False
    assert result["operation"]["state"] == "idle"
    assert client_constructions == []


def test_shared_attach_adapter_propagates_and_enforces_confirmation(monkeypatch):
    monkeypatch.setenv(SHARED_SERVER_FEATURE_ENV, "true")
    server = create_server("shared-confirmation", profile="desktop_shared")
    import src.tools.shared_session as module

    calls = []

    class ValidatingManager:
        def attach(self, request, *, profile):
            calls.append((request, profile))
            normalized = normalize_shared_server_attach_request(
                request,
                profile=profile,
                environ={SHARED_SERVER_FEATURE_ENV: "true"},
            )
            return {
                "success": True,
                "user_confirmed": normalized.user_confirmed,
            }

    monkeypatch.setattr(module, "shared_session_manager", ValidatingManager())
    attach = server._tool_manager._tools["shared_server_attach"].fn

    with pytest.raises(ValueError, match="user_confirmed=true"):
        attach("127.0.0.1", 2036, False)
    accepted = attach("127.0.0.1", 2036, True)

    assert accepted["success"] is True
    assert accepted["user_confirmed"] is True
    assert accepted["operation_gate"]["release"]["released"] is True
    assert calls == [
        (
            {
                "endpoint": {"host": "127.0.0.1", "port": 2036},
                "user_confirmed": False,
            },
            "desktop_shared",
        ),
        (
            {
                "endpoint": {"host": "127.0.0.1", "port": 2036},
                "user_confirmed": True,
            },
            "desktop_shared",
        ),
    ]


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
        "adopt_model",
        lambda selector: calls.append(("adopt", selector)) or {"success": True},
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
    monkeypatch.setattr(
        module.shared_session_manager,
        "snapshot_model",
        lambda **kwargs: calls.append(("snapshot", kwargs)) or {"success": True},
    )
    tools = server._tool_manager._tools

    assert tools["shared_server_models"].fn()["sentinel"] == "models"
    assert tools["shared_model_adopt"].fn("Model_1", "Shared", None, True)["success"] is True
    assert tools["shared_model_lock"].fn("interactive_inspection", None, None)["success"] is True
    assert tools["shared_model_verify"].fn("a" * 64, "b" * 64)["success"] is True
    assert tools["shared_model_unlock"].fn("a" * 64, "Desktop turn")["success"] is True
    assert tools["shared_model_snapshot"].fn("a" * 64, "b" * 64, 1024)["success"] is True
    assert calls == [
        (
            "adopt",
            {
                "tag": "Model_1",
                "expected_label": "Shared",
                "expected_unsaved": True,
            },
        ),
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
        (
            "snapshot",
            {
                "expected_lock_sha256": "a" * 64,
                "expected_revision_sha256": "b" * 64,
                "max_snapshot_bytes": 1024,
            },
        ),
    ]
