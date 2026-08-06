"""Solver-free tests for shared-model revisions and enforcement locks."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest
import src.shared_session.locking as locking_module
from src.durable import canonical_sha256_v1
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


def test_revision_accepts_exact_collection_item_limit():
    revision = build_shared_model_revision(
        _model(),
        sequence=0,
        structural_readback={
            "items": list(range(locking_module.MAX_REVISION_COLLECTION_ITEMS))
        },
        state_readback={"state": "ready"},
    )

    assert len(revision.revision_sha256) == 64


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


def test_lock_hash_binds_every_serialized_leaf_independently():
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
    body = lock.to_dict()
    observed = body.pop("lock_sha256")

    assert canonical_sha256_v1(body) == observed

    def scalar_paths(value, path=()):
        if isinstance(value, dict):
            for key, child in value.items():
                yield from scalar_paths(child, (*path, key))
        else:
            yield path

    for path in scalar_paths(body):
        changed = deepcopy(body)
        parent = changed
        for key in path[:-1]:
            parent = parent[key]
        value = parent[path[-1]]
        if isinstance(value, bool):
            parent[path[-1]] = not value
        elif isinstance(value, (int, float)):
            parent[path[-1]] = value + 1
        elif value is None:
            parent[path[-1]] = "present"
        else:
            parent[path[-1]] = f"{value}:changed"
        assert canonical_sha256_v1(changed) != observed, path


def test_lock_identity_normalizes_acquisition_id_case_before_derivation():
    model = _model()
    revision = _revision(model)
    common = {
        "attached_server": _server(),
        "model": model,
        "revision": revision,
        "collaboration_mode": "interactive_inspection",
        "lock_created_at_epoch": 3456.7,
        "mcp_process": {
            "pid": 5000,
            "process_create_time": 3000.0,
            "command_signature": "d" * 64,
        },
    }

    lower = build_shared_model_lock(
        session_acquisition_id="abcdef0123456789abcdef0123456789",
        **common,
    )
    upper = build_shared_model_lock(
        session_acquisition_id="ABCDEF0123456789ABCDEF0123456789",
        **common,
    )

    assert lower == upper
    assert lower.lock_sha256 == upper.lock_sha256
    assert lower.lock_id == upper.lock_id


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


@pytest.mark.parametrize("forged_identity", ["server", "model", "revision"])
def test_lock_builder_rejects_directly_fabricated_identity_dataclasses(
    forged_identity,
):
    server = _server()
    model = _model()
    revision = _revision(model)
    if forged_identity == "server":
        server = replace(server, server_pid=server.server_pid + 1)
        match = "server identity"
    elif forged_identity == "model":
        model = replace(model, label="Forged label")
        match = "model identity"
    else:
        revision = replace(revision, sequence=revision.sequence + 1)
        match = "revision identity"

    with pytest.raises(ValueError, match=match):
        build_shared_model_lock(
            attached_server=server,
            session_acquisition_id="b" * 32,
            model=model,
            revision=revision,
            collaboration_mode="interactive_inspection",
            lock_created_at_epoch=3456.7,
            mcp_process={
                "pid": 5000,
                "process_create_time": 3000.0,
                "command_signature": "d" * 64,
            },
        )


@pytest.mark.parametrize("mode", ["interactive", "exclusive", "", [], {}])
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


def test_lock_rejects_integer_timestamp_overflow_as_validation_error():
    with pytest.raises(ValueError, match="positive and finite"):
        build_shared_model_lock(
            attached_server=_server(),
            session_acquisition_id="b" * 32,
            model=_model(),
            revision=_revision(),
            collaboration_mode="interactive_inspection",
            lock_created_at_epoch=10**400,
            mcp_process={
                "pid": 5000,
                "process_create_time": 3000.0,
                "command_signature": "d" * 64,
            },
        )
