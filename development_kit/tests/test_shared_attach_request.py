"""Tests for the full pre-lease shared attach request gate."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from src.shared_session.attach_request import normalize_shared_server_attach_request
from src.shared_session.contracts import SHARED_SERVER_FEATURE_ENV
from src.shared_session.lifecycle import SharedSessionManager


def _request():
    return {
        "endpoint": {"host": "127.0.0.1", "port": 2036},
        "user_confirmed": True,
    }


def test_attach_request_requires_all_static_and_per_call_gates():
    enabled = {SHARED_SERVER_FEATURE_ENV: "true"}

    accepted = normalize_shared_server_attach_request(
        _request(), profile="wave_optics", environ=enabled
    )
    assert accepted.feature_gate["profile"] == "wave_optics"
    with pytest.raises(ValueError, match="static feature flag"):
        normalize_shared_server_attach_request(
            _request(), profile="core", environ={}
        )
    unconfirmed = {**_request(), "user_confirmed": False}
    with pytest.raises(ValueError, match="user_confirmed=true"):
        normalize_shared_server_attach_request(
            unconfirmed, profile="core", environ=enabled
        )


def test_attach_request_normalizes_exact_endpoint():
    result = normalize_shared_server_attach_request(
        _request(),
        profile="core",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )

    assert result.endpoint.host == "127.0.0.1"
    assert result.user_confirmed is True
    assert result.feature_gate["gate_open"] is True
    with pytest.raises(TypeError, match="frozen"):
        result.feature_gate["gate_open"] = False
    exported = result.to_dict()
    exported["feature_gate"]["gate_open"] = False
    assert result.feature_gate["gate_open"] is True


@pytest.mark.parametrize(
    "raw_request",
    [
        {**_request(), "endpoint": {"host": "10.0.0.1", "port": 2036}},
        {**_request(), "model_selector": {"tag": "Model_1"}},
        {**_request(), "lease_mode": "force"},
    ],
)
def test_malformed_attach_request_is_rejected_before_lease_callback(raw_request):
    lease_calls = []

    def ownership_factory():
        lease_calls.append("ownership_factory")
        return object()

    manager = SharedSessionManager(ownership_factory=ownership_factory)

    with pytest.raises(ValueError):
        manager.attach(
            raw_request,
            profile="core",
            environ={SHARED_SERVER_FEATURE_ENV: "true"},
        )

    assert lease_calls == []


def test_disabled_feature_registers_no_shared_tools_before_mph_import():
    code = """
import os
import sys
os.environ.pop('COMSOL_MCP_ENABLE_SHARED_SERVER', None)
import asyncio
from src.server import create_server
server = create_server('disabled-shared', profile='core')
names = {tool.name for tool in asyncio.run(server.list_tools())}
assert 'shared_server_attach' not in names
assert 'mph' not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
