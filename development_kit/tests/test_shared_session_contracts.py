"""Solver-free tests for shared-session input contracts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from src.shared_session.contracts import (
    SHARED_SERVER_FEATURE_ENV,
    normalize_shared_server_endpoint,
    normalize_shared_server_feature_gate,
)


@pytest.mark.parametrize("profile", ["core", "wave_optics", "full"])
def test_shared_feature_is_default_off_for_existing_profiles(profile):
    gate = normalize_shared_server_feature_gate(profile, environ={})

    assert gate.feature_enabled is False
    assert gate.profile_selected is False
    assert gate.gate_open is False
    assert gate.restart_required_after_change is True


def test_shared_feature_requires_profile_and_strict_true_flag():
    enabled = {SHARED_SERVER_FEATURE_ENV: " TRUE "}

    wrong_profile = normalize_shared_server_feature_gate("wave_optics", environ=enabled)
    selected = normalize_shared_server_feature_gate(" DESKTOP_SHARED ", environ=enabled)

    assert wrong_profile.feature_enabled is True
    assert wrong_profile.gate_open is False
    assert selected.to_dict() == {
        "profile": "desktop_shared",
        "feature_enabled": True,
        "profile_selected": True,
        "gate_open": True,
        "environment_variable": SHARED_SERVER_FEATURE_ENV,
        "restart_required_after_change": True,
    }


@pytest.mark.parametrize("value", ["1", "yes", "enabled", "", " true-ish "])
def test_shared_feature_rejects_ambiguous_flag_values(value):
    with pytest.raises(ValueError, match="exactly true or false"):
        normalize_shared_server_feature_gate(
            "desktop_shared", environ={SHARED_SERVER_FEATURE_ENV: value}
        )


@pytest.mark.parametrize(
    ("raw", "expected_host"),
    [
        ({"host": "LOCALHOST", "port": 2036}, "localhost"),
        ({"host": "127.0.0.1", "port": 2036}, "127.0.0.1"),
        ({"host": "127.25.3.9", "port": 1}, "127.25.3.9"),
        ({"host": "0:0:0:0:0:0:0:1", "port": 65535}, "::1"),
    ],
)
def test_loopback_endpoint_is_normalized_without_dns(raw, expected_host):
    endpoint = normalize_shared_server_endpoint(raw)

    assert endpoint.host == expected_host
    assert endpoint.port == raw["port"]
    assert endpoint.scope == "loopback"


@pytest.mark.parametrize(
    "raw",
    [
        {"host": "192.168.1.2", "port": 2036},
        {"host": "comsol.internal", "port": 2036},
        {"host": "127.0.0.1", "port": 0},
        {"host": "127.0.0.1", "port": 65536},
        {"host": "127.0.0.1", "port": True},
        {"host": "127.0.0.1", "port": "2036"},
        {"host": "127.0.0.1"},
        {"host": "127.0.0.1", "port": 2036, "token": "secret"},
    ],
)
def test_endpoint_rejects_remote_malformed_and_unknown_inputs(raw):
    with pytest.raises(ValueError):
        normalize_shared_server_endpoint(raw)


def test_contract_import_does_not_import_mph_or_construct_a_client():
    code = """
import sys
from src.shared_session.contracts import normalize_shared_server_endpoint
assert 'mph' not in sys.modules
assert normalize_shared_server_endpoint({'host': '127.0.0.1', 'port': 2036}).port == 2036
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
