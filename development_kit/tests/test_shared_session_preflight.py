"""Shared-session preflight version gates and process identity tests."""

from __future__ import annotations

import pytest

from comsol_mcp.shared_session.preflight import (
    _normalize_process,
    normalize_comsol_version_readback,
)


def test_file_version_must_be_the_whole_build_string():
    assert normalize_comsol_version_readback("6.4.0.293")[1] == (6, 4, 0, 293)
    assert normalize_comsol_version_readback(" 6.4.0.293 ")[1] == (6, 4, 0, 293)


def test_version_embedded_in_prose_is_unreadable():
    normalized, parts = normalize_comsol_version_readback(r"C:\apps\comsol\6.4.0.13\bin")
    assert normalized == "unreadable"
    assert parts is None


def test_display_fallback_requires_a_labeled_build_token():
    labeled = normalize_comsol_version_readback(
        "COMSOL Multiphysics 6.4.0 (build 293)",
        expected_file_version="6.4.0.293",
    )
    assert labeled[1] == (6, 4, 0, 293)

    bitness = normalize_comsol_version_readback(
        "COMSOL Multiphysics 6.4.0 (64-bit)",
        expected_file_version="6.4.0.293",
    )
    assert bitness == ("unreadable", None)


def _process(**overrides):
    value = {
        "pid": 7,
        "parent_pid": 1,
        "kind": "mph_client",
        "create_time": 1000.0,
        "command_signature": "a" * 64,
        "file_version": "6.4.0.293",
        "window_count": 2,
        "responding": True,
    }
    value.update(overrides)
    return _normalize_process(value, 0)


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"pid": 8}, "pid"),
        ({"kind": "comsol_desktop"}, "kind"),
        ({"create_time": 1000.000000125}, "create_time"),
        ({"file_version": "6.4.0.294"}, "file_version"),
    ],
)
def test_identity_hash_tracks_every_replacement_component(override, field):
    base = _process()
    variant = _process(**override)

    assert base["identity_sha256"] != variant["identity_sha256"], field


def test_identity_hash_ignores_volatile_window_and_responsiveness_fields():
    base = _process()
    volatile = _process(window_count=5, responding=False)

    assert base["identity_sha256"] == volatile["identity_sha256"]
