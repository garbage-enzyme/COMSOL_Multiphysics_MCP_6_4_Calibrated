"""Solver-free tests for shared-session input contracts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from src.shared_session.contracts import (
    SHARED_SERVER_FEATURE_ENV,
    SharedServerEndpoint,
    normalize_shared_listener_bind_host,
    normalize_shared_server_endpoint,
    normalize_shared_server_feature_gate,
    shared_listener_matches_endpoint,
)


@pytest.mark.parametrize("profile", ["core", "wave_optics", "full"])
def test_shared_feature_is_default_off_for_existing_profiles(profile):
    gate = normalize_shared_server_feature_gate(profile, environ={})

    assert gate.feature_enabled is False
    assert gate.profile_independent is True
    assert gate.gate_open is False
    assert gate.restart_required_after_change is True


def test_shared_feature_is_profile_independent_and_requires_strict_true_flag():
    enabled = {SHARED_SERVER_FEATURE_ENV: " TRUE "}

    wave = normalize_shared_server_feature_gate("wave_optics", environ=enabled)
    core = normalize_shared_server_feature_gate(" core ", environ=enabled)

    assert wave.feature_enabled is True
    assert wave.gate_open is True
    assert core.to_dict() == {
        "profile": "core",
        "feature_enabled": True,
        "profile_independent": True,
        "gate_open": True,
        "environment_variable": SHARED_SERVER_FEATURE_ENV,
        "restart_required_after_change": True,
    }


@pytest.mark.parametrize("value", ["1", "yes", "enabled", "", " true-ish "])
def test_shared_feature_rejects_ambiguous_flag_values(value):
    with pytest.raises(ValueError, match="exactly true or false"):
        normalize_shared_server_feature_gate(
            "core", environ={SHARED_SERVER_FEATURE_ENV: value}
        )


@pytest.mark.parametrize(
    ("raw", "expected_host"),
    [
        ({"host": "LOCALHOST", "port": 2036}, "127.0.0.1"),
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
        {"host": "127.0.0.1", "port": -1},
        {"host": "127.0.0.1", "port": 65536},
        {"host": "127.0.0.1", "port": True},
        {"host": "127.0.0.1", "port": 2036.0},
        {"host": "127.0.0.1", "port": None},
        {"host": "127.0.0.1", "port": "2036"},
        {"host": "127.0.0.1"},
        {"host": "127.0.0.1", "port": 2036, "token": "secret"},
    ],
)
def test_endpoint_rejects_remote_malformed_and_unknown_inputs(raw):
    with pytest.raises(ValueError):
        normalize_shared_server_endpoint(raw)


def test_endpoint_public_constructor_enforces_and_canonicalizes_loopback_contract():
    endpoint = SharedServerEndpoint(host=" LOCALHOST ", port=2036)

    assert endpoint.to_dict() == {
        "host": "127.0.0.1",
        "port": 2036,
        "scope": "loopback",
    }
    for kwargs in (
        {"host": "0.0.0.0", "port": 2036},
        {"host": "127.0.0.1", "port": 0},
        {"host": "127.0.0.1", "port": True},
        {"host": "127.0.0.1", "port": 2036, "scope": "wildcard"},
    ):
        with pytest.raises(ValueError):
            SharedServerEndpoint(**kwargs)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", ("127.0.0.1", "loopback")),
        ("::1", ("::1", "loopback")),
        ("0.0.0.0", ("0.0.0.0", "wildcard")),
        ("::", ("::", "wildcard")),
        ("::0", ("::", "wildcard")),
        ("0:0:0:0:0:0:0:0", ("::", "wildcard")),
    ],
)
def test_listener_bind_host_preserves_scope(host, expected):
    assert normalize_shared_listener_bind_host(host) == expected


def test_wildcard_listener_matches_only_same_address_family_and_endpoint_port():
    ipv4_endpoint = normalize_shared_server_endpoint({"host": "127.0.0.1", "port": 2036})
    ipv6_endpoint = normalize_shared_server_endpoint({"host": "::1", "port": 2036})

    assert shared_listener_matches_endpoint(
        listener_host="0.0.0.0", listener_port=2036, endpoint=ipv4_endpoint
    )
    assert not shared_listener_matches_endpoint(
        listener_host="::", listener_port=2036, endpoint=ipv4_endpoint
    )
    assert shared_listener_matches_endpoint(
        listener_host="::", listener_port=2036, endpoint=ipv6_endpoint
    )
    assert not shared_listener_matches_endpoint(
        listener_host="0.0.0.0", listener_port=2036, endpoint=ipv6_endpoint
    )
    assert not shared_listener_matches_endpoint(
        listener_host="0.0.0.0", listener_port=2037, endpoint=ipv4_endpoint
    )
    assert not shared_listener_matches_endpoint(
        listener_host="192.168.1.2", listener_port=2036, endpoint=ipv4_endpoint
    )


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
