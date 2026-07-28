"""Solver-free tests for shared-model revisions and enforcement locks."""

from __future__ import annotations

import pytest
import src.shared_session.locking as locking_module
from src.shared_session.identity import normalize_attached_server_identity
from src.shared_session.locking import (
    build_shared_model_lock,
    build_shared_model_revision,
    normalize_shared_model_identity,
)


def _server():
    return normalize_attached_server_identity(
        {
            "endpoint": {"host": "127.0.0.1", "port": 2036},
            "server_pid": 4200,
            "server_process_create_time": 1234.5,
            "server_command_signature": "a" * 64,
            "listener_bind_scope": "loopback",
            "listener_observed_at_epoch": 2345.6,
        }
    )


def _model(path="C:/models/shared.mph"):
    return normalize_shared_model_identity(
        {
            "tag": "Model_1",
            "label": "Shared model",
            "file_path": path,
            "unsaved": path is None,
        }
    )


def _revision(model=None):
    return build_shared_model_revision(
        model or _model(),
        sequence=0,
        structural_readback={"components": ["comp1"], "studies": ["std1"]},
        state_readback={"parameters": {"gap": "10[nm]"}},
    )


def test_model_identity_covers_tag_label_and_unicode_saved_state():
    model = _model("C:/研究/共享模型.mph")

    assert model.file_path == "C:\\研究\\共享模型.mph"
    assert model.unsaved is False
    assert len(model.identity_sha256) == 64


def test_unsaved_model_identity_is_explicit():
    model = _model(None)

    assert model.file_path is None
    assert model.unsaved is True


@pytest.mark.parametrize(
    "raw",
    [
        {"tag": "model1", "label": "M", "file_path": None, "unsaved": False},
        {"tag": "model1", "label": "M", "file_path": "C:/m.mph", "unsaved": True},
        {"tag": "model1", "label": "", "file_path": "C:/m.mph", "unsaved": False},
        {"tag": "model1", "label": "M", "file_path": "C:/m.mph", "unsaved": False, "name": "x"},
    ],
)
def test_model_identity_rejects_ambiguous_state(raw):
    with pytest.raises(ValueError):
        normalize_shared_model_identity(raw)


def test_revision_changes_for_structural_or_state_readback():
    model = _model()
    baseline = _revision(model)
    structural_change = build_shared_model_revision(
        model,
        sequence=0,
        structural_readback={"components": ["comp1", "comp2"]},
        state_readback={"parameters": {"gap": "10[nm]"}},
    )
    desktop_change = build_shared_model_revision(
        model,
        sequence=0,
        structural_readback={"components": ["comp1"], "studies": ["std1"]},
        state_readback={"parameters": {"gap": "11[nm]"}},
    )

    assert baseline.structural_sha256 != structural_change.structural_sha256
    assert baseline.readback_sha256 != desktop_change.readback_sha256
    assert len({baseline.revision_sha256, structural_change.revision_sha256, desktop_change.revision_sha256}) == 3


def test_revision_is_deterministic_for_mapping_order():
    model = _model()
    first = build_shared_model_revision(
        model,
        sequence=7,
        structural_readback={"b": 2, "a": 1},
        state_readback={"y": [2, 3], "x": True},
    )
    second = build_shared_model_revision(
        model,
        sequence=7,
        structural_readback={"a": 1, "b": 2},
        state_readback={"x": True, "y": [2, 3]},
    )

    assert first == second


@pytest.mark.parametrize(
    "structural,state",
    [
        ({}, {"x": 1}),
        ({"x": 1}, {}),
        ({"x": float("nan")}, {"y": 1}),
        ({"x": object()}, {"y": 1}),
        ({"x": [0] * 257}, {"y": 1}),
    ],
)
def test_revision_rejects_missing_nonfinite_or_unbounded_readback(structural, state):
    with pytest.raises(ValueError):
        build_shared_model_revision(
            _model(),
            sequence=0,
            structural_readback=structural,
            state_readback=state,
        )


def test_revision_rejects_aggregate_node_budget_before_serialization(monkeypatch):
    monkeypatch.setattr(locking_module, "MAX_REVISION_NODES", 4)

    with pytest.raises(ValueError, match="aggregate node limit"):
        build_shared_model_revision(
            _model(),
            sequence=0,
            structural_readback={"branch": [1, 2]},
            state_readback={"state": "ready"},
        )


def test_lock_binds_server_session_model_revision_source_and_mcp_process():
    model = _model()
    revision = _revision(model)
    lock = build_shared_model_lock(
        attached_server=_server(),
        session_acquisition_id="b" * 32,
        model=model,
        revision=revision,
        collaboration_mode="interactive_inspection",
        immutable_source={"path": "C:/models/source.mph", "sha256": "c" * 64},
        lock_created_at_epoch=3456.7,
        mcp_process={
            "pid": 5000,
            "process_create_time": 3000.0,
            "command_signature": "d" * 64,
        },
    )
    payload = lock.to_dict()

    assert payload["attached_server"]["ownership"] == "external_user_owned"
    assert payload["session_acquisition_id"] == "b" * 32
    assert payload["model"]["identity_sha256"] == model.identity_sha256
    assert payload["revision"]["revision_sha256"] == revision.revision_sha256
    assert payload["immutable_source"]["sha256"] == "c" * 64
    assert len(payload["lock_sha256"]) == 64
    with pytest.raises(TypeError, match="frozen"):
        lock.revision["sequence"] = 1
    payload["revision"]["sequence"] = 1
    assert lock.revision["sequence"] == 0


def test_lock_rejects_revision_from_a_different_model():
    first = _model()
    second = normalize_shared_model_identity(
        {"tag": "Model_2", "label": "Other", "file_path": None, "unsaved": True}
    )

    with pytest.raises(ValueError, match="different model identity"):
        build_shared_model_lock(
            attached_server=_server(),
            session_acquisition_id="b" * 32,
            model=second,
            revision=_revision(first),
            collaboration_mode="interactive_inspection",
            lock_created_at_epoch=3456.7,
            mcp_process={
                "pid": 5000,
                "process_create_time": 3000.0,
                "command_signature": "d" * 64,
            },
        )


@pytest.mark.parametrize("mode", ["interactive", "exclusive", ""])
def test_lock_rejects_implicit_collaboration_modes(mode):
    with pytest.raises(ValueError, match="collaboration mode"):
        build_shared_model_lock(
            attached_server=_server(),
            session_acquisition_id="b" * 32,
            model=_model(),
            revision=_revision(),
            collaboration_mode=mode,
            lock_created_at_epoch=3456.7,
            mcp_process={
                "pid": 5000,
                "process_create_time": 3000.0,
                "command_signature": "d" * 64,
            },
        )
