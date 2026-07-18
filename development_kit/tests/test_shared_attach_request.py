"""Tests for the full pre-lease shared attach request gate."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from src.shared_session.attach_request import normalize_shared_server_attach_request
from src.shared_session.contracts import SHARED_SERVER_FEATURE_ENV


def _request():
    return {
        "endpoint": {"host": "127.0.0.1", "port": 2036},
        "user_confirmed": True,
    }


def test_attach_request_requires_all_static_and_per_call_gates():
    enabled = {SHARED_SERVER_FEATURE_ENV: "true"}

    with pytest.raises(ValueError, match="desktop_shared profile"):
        normalize_shared_server_attach_request(
            _request(), profile="wave_optics", environ=enabled
        )
    with pytest.raises(ValueError, match="static feature flag"):
        normalize_shared_server_attach_request(
            _request(), profile="desktop_shared", environ={}
        )
    unconfirmed = {**_request(), "user_confirmed": False}
    with pytest.raises(ValueError, match="user_confirmed=true"):
        normalize_shared_server_attach_request(
            unconfirmed, profile="desktop_shared", environ=enabled
        )


def test_attach_request_normalizes_exact_endpoint():
    result = normalize_shared_server_attach_request(
        _request(),
        profile="desktop_shared",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )

    assert result.endpoint.host == "127.0.0.1"
    assert result.user_confirmed is True
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

    with pytest.raises(ValueError):
        normalized = normalize_shared_server_attach_request(
            raw_request,
            profile="desktop_shared",
            environ={SHARED_SERVER_FEATURE_ENV: "true"},
        )
        lease_calls.append(normalized)

    assert lease_calls == []


def test_disabled_profile_fails_before_mph_import():
    code = """
import os
import sys
os.environ.pop('COMSOL_MCP_ENABLE_SHARED_SERVER', None)
try:
    from src.server import create_server
    create_server('disabled-shared', profile='desktop_shared')
except ValueError as exc:
    assert 'COMSOL_MCP_ENABLE_SHARED_SERVER=true' in str(exc)
else:
    raise AssertionError('disabled shared profile unexpectedly started')
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
