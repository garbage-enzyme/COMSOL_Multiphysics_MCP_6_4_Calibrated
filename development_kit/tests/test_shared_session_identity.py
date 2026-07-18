"""Tests for exact attached-server and model-selector identities."""

from __future__ import annotations

from copy import deepcopy

import pytest

from src.shared_session.identity import (
    normalize_attached_server_identity,
    normalize_shared_model_selector,
)


def _server_identity():
    return {
        "endpoint": {"host": "127.0.0.1", "port": 2036},
        "server_pid": 4200,
        "server_process_create_time": 1234.5,
        "server_command_signature": "A" * 64,
        "listener_observed_at_epoch": 2345.6,
    }


def test_attached_server_identity_is_exact_stable_and_non_owned():
    first = normalize_attached_server_identity(_server_identity())
    second = normalize_attached_server_identity(deepcopy(_server_identity()))

    assert first == second
    assert first.server_command_signature == "a" * 64
    assert first.ownership == "external_user_owned"
    assert len(first.identity_sha256) == 64
    assert first.to_dict()["endpoint"]["scope"] == "loopback"


@pytest.mark.parametrize(
    "field,value",
    [
        ("server_pid", 0),
        ("server_pid", True),
        ("server_process_create_time", float("nan")),
        ("server_process_create_time", 0),
        ("server_command_signature", "short"),
        ("listener_observed_at_epoch", float("inf")),
    ],
)
def test_attached_server_identity_rejects_incomplete_process_evidence(field, value):
    raw = _server_identity()
    raw[field] = value

    with pytest.raises(ValueError):
        normalize_attached_server_identity(raw)


def test_attached_server_identity_rejects_missing_and_unknown_fields():
    missing = _server_identity()
    missing.pop("server_pid")
    unknown = {**_server_identity(), "owned": True}

    with pytest.raises(ValueError, match="missing required fields"):
        normalize_attached_server_identity(missing)
    with pytest.raises(ValueError, match="unknown fields"):
        normalize_attached_server_identity(unknown)


def test_model_selector_normalizes_unicode_saved_path():
    selector = normalize_shared_model_selector(
        {
            "tag": "Model_1",
            "expected_label": "共享模型",
            "expected_file_path": "C:/研究/模型.mph",
        }
    )

    assert selector.to_dict() == {
        "tag": "Model_1",
        "expected_label": "共享模型",
        "expected_file_path": "C:\\研究\\模型.mph",
        "expected_unsaved": None,
    }


def test_model_selector_supports_explicit_unsaved_confirmation():
    selector = normalize_shared_model_selector(
        {"tag": "model1", "expected_unsaved": True}
    )

    assert selector.expected_file_path is None
    assert selector.expected_unsaved is True


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"tag": "1bad"},
        {"tag": "model-1"},
        {"tag": "model1", "expected_label": ""},
        {"tag": "model1", "expected_file_path": "relative.mph"},
        {"tag": "model1", "expected_file_path": "C:\\x.mph", "expected_unsaved": True},
        {"tag": "model1", "expected_unsaved": False},
        {"tag": "model1", "name": "ambiguous"},
    ],
)
def test_model_selector_rejects_ambiguous_or_unbounded_identity(raw):
    with pytest.raises(ValueError):
        normalize_shared_model_selector(raw)
