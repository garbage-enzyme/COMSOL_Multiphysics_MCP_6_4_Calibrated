"""Fake-client tests for non-owning shared attach and detach lifecycle."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.shared_session.contracts import SHARED_SERVER_FEATURE_ENV
from src.shared_session.lifecycle import (
    SharedSessionManager,
    _default_model_inventory_reader,
    _default_save_copy_writer,
)
from src.tools.ownership import _command_signature


def _process(pid, kind, command, *, windows=0, created=None):
    return {
        "pid": pid,
        "parent_pid": 0,
        "kind": kind,
        "create_time": float(pid if created is None else created),
        "command_signature": _command_signature(command),
        "file_version": "6.4.0.293",
        "window_count": windows,
        "responding": True,
    }


def _snapshot(server_created=20.0):
    return {
        "inventory_complete": True,
        "observed_at_epoch": 1000.0,
        "processes": [
            _process(10, "comsol_desktop", ["comsol.exe"], windows=1),
            _process(
                20,
                "comsol_server",
                ["comsolmphserver.exe", "-port", "2036"],
                created=server_created,
            ),
        ],
        "listeners": [{"host": "127.0.0.1", "port": 2036, "pid": 20}],
    }


def _request():
    return {
        "endpoint": {"host": "127.0.0.1", "port": 2036},
        "user_confirmed": True,
    }


def _selector():
    return {
        "tag": "Model_1",
        "expected_label": "Shared",
        "expected_unsaved": True,
    }


class FakeOwnership:
    def __init__(self, root):
        self.lease_path = root / "solver_owner.json"
        self.releases = 0

    def acquire_attached(self, identity):
        payload = {
            "acquisition_id": "a" * 32,
            "attached_server": {"server_pid": identity.server_pid, "owned": False},
        }
        self.lease_path.write_text(json.dumps(payload), encoding="utf-8")
        return {"success": True, "lease": payload}

    def release(self):
        self.releases += 1
        self.lease_path.unlink(missing_ok=True)
        return {"success": True, "released": True}


class FakeClient:
    def __init__(self):
        self.calls = []
        self.disconnected = False

    def disconnect(self):
        self.calls.append("disconnect")
        self.disconnected = True

    def clear(self):
        self.calls.append("clear")
        raise AssertionError("attached lifecycle must never clear models")


class FakeJavaModel:
    def __init__(self, tag, label, path):
        self._tag = tag
        self._label = label
        self._path = path
        self.save_calls = []

    def tag(self):
        return self._tag

    def label(self):
        return self._label

    def getFilePath(self):
        return self._path

    def save(self, *args):
        self.save_calls.append(args)


class FakeMphModel:
    def __init__(self, tag, label, path):
        self.java = FakeJavaModel(tag, label, path)


class InventoryClient:
    def __init__(self, models):
        self._models = models

    def models(self):
        return self._models


def _inventory(models=None):
    return models if models is not None else [
        {"tag": "Model_1", "label": "Shared", "file_path": None, "unsaved": True}
    ]


def _manager(
    tmp_path,
    *,
    client=None,
    snapshots=None,
    models=None,
    client_factory=None,
    revision_state=None,
    snapshot_writer=None,
):
    values = iter(snapshots or [_snapshot() for _ in range(10)])
    ownership = FakeOwnership(tmp_path)
    client = client or FakeClient()
    revision_state = revision_state or {
        "structural": {"components": ["comp1"], "studies": ["std1"]},
        "state": {"parameters": {"gap": "10[nm]"}},
    }
    return (
        SharedSessionManager(
            snapshot_provider=lambda: next(values),
            ownership_factory=lambda: ownership,
            client_factory=client_factory or (lambda host, port: client),
            model_inventory_reader=lambda value: _inventory(models),
            model_revision_reader=lambda value, tag: (
                revision_state["structural"], revision_state["state"]
            ),
            mcp_process_identity_provider=lambda: {
                "pid": 5000,
                "process_create_time": 900.0,
                "command_signature": "f" * 64,
            },
            snapshot_target_factory=lambda tag: tmp_path / f"{tag}-snapshot.mph",
            save_copy_writer=snapshot_writer or (
                lambda value, tag, target: target.write_bytes(b"snapshot fixture")
            ),
            clock=lambda: 1100.0,
        ),
        ownership,
        client,
    )


def test_default_inventory_uses_raw_java_path_for_unsaved_models():
    inventory = _default_model_inventory_reader(
        InventoryClient(
            [
                FakeMphModel("Model_1", "Blank", ""),
                FakeMphModel("Model_2", "共享", "C:/研究/共享.mph"),
            ]
        )
    )

    assert inventory == [
        {"tag": "Model_1", "label": "Blank", "file_path": None, "unsaved": True},
        {
            "tag": "Model_2",
            "label": "共享",
            "file_path": "C:/研究/共享.mph",
            "unsaved": False,
        },
    ]


def test_default_snapshot_writer_uses_clientapi_save_copy_overload(tmp_path):
    model = FakeMphModel("Model_1", "Shared", "")
    target = tmp_path / "copy.mph"

    _default_save_copy_writer(
        InventoryClient([model]), "Model_1", target
    )

    assert model.java.save_calls == [(str(target), True)]


def test_attached_inventory_is_bounded_sorted_and_keeps_duplicate_metadata(tmp_path):
    models = [
        {"tag": "Model_2", "label": "Shared", "file_path": None, "unsaved": True},
        {"tag": "Model_1", "label": "Shared", "file_path": None, "unsaved": True},
    ]
    manager, _ownership, _client = _manager(tmp_path, models=models)
    assert manager.attach(
        _request(),
        profile="desktop_shared",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )["success"] is True

    result = manager.models()

    assert result["success"] is True
    assert result["model_count"] == 2
    assert [item["tag"] for item in result["models"]] == ["Model_1", "Model_2"]
    assert [item["label"] for item in result["models"]] == ["Shared", "Shared"]
    assert result["model_inventory_sha256"] == result["attached_inventory_sha256"]


def test_exact_tag_adoption_allows_duplicate_unicode_labels_and_paths(tmp_path):
    shared_path = "C:/研究/共享.mph"
    models = [
        {
            "tag": "Model_2",
            "label": "共享",
            "file_path": shared_path,
            "unsaved": False,
        },
        {
            "tag": "Model_1",
            "label": "共享",
            "file_path": shared_path,
            "unsaved": False,
        },
    ]
    selector = {
        "tag": "Model_1",
        "expected_label": "共享",
        "expected_file_path": shared_path,
    }
    manager, _ownership, _client = _manager(tmp_path, models=models)

    attached = manager.attach(
        _request(),
        profile="desktop_shared",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )
    adopted = manager.adopt_model(selector)

    assert attached["success"] is True
    assert adopted["selected_model"]["tag"] == "Model_1"
    assert adopted["selected_model"]["file_path"] == "C:\\研究\\共享.mph"
    assert attached["model_count"] == 2


def test_duplicate_server_model_tags_fail_attach_closed(tmp_path):
    models = [
        {"tag": "Model_1", "label": "First", "file_path": None, "unsaved": True},
        {"tag": "Model_1", "label": "Second", "file_path": None, "unsaved": True},
    ]
    manager, ownership, client = _manager(tmp_path, models=models)

    result = manager.attach(
        _request(),
        profile="desktop_shared",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )

    assert result["success"] is False
    assert result["state"] == "attach_failed"
    assert "duplicate tags" in result["error"]
    assert client.calls == ["disconnect"]
    assert ownership.releases == 1


def test_model_inventory_requires_an_attached_client(tmp_path):
    manager, _ownership, _client = _manager(tmp_path)

    assert manager.models() == {
        "success": False,
        "state": "detached",
        "models": [],
        "model_count": 0,
    }


def _attach_and_lock(manager):
    assert manager.attach(
        _request(),
        profile="desktop_shared",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )["success"] is True
    assert manager.adopt_model(_selector())["success"] is True
    locked = manager.lock_model(collaboration_mode="interactive_inspection")
    assert locked["success"] is True
    return locked["model_lock"]


def test_model_lock_binds_fresh_server_model_revision_and_process(tmp_path):
    manager, _ownership, _client = _manager(tmp_path)

    lock = _attach_and_lock(manager)
    status = manager.status()

    assert lock["attached_server"]["server_pid"] == 20
    assert lock["model"]["tag"] == "Model_1"
    assert lock["revision"]["sequence"] == 0
    assert lock["mcp_process"]["pid"] == 5000
    assert status["state"] == "attached_model_locked"
    assert status["model_lock"]["lock_sha256"] == lock["lock_sha256"]


def test_model_lock_verifies_immutable_source_bytes(tmp_path):
    manager, _ownership, _client = _manager(tmp_path)
    assert manager.attach(
        _request(),
        profile="desktop_shared",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )["success"] is True
    assert manager.adopt_model(_selector())["success"] is True
    source = tmp_path / "source.mph"
    source.write_bytes(b"immutable model fixture")
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()

    rejected = manager.lock_model(
        collaboration_mode="interactive_inspection",
        immutable_source={"path": str(source), "sha256": "0" * 64},
    )
    accepted = manager.lock_model(
        collaboration_mode="interactive_inspection",
        immutable_source={"path": str(source), "sha256": source_sha256},
    )

    assert rejected["success"] is False
    assert "does not match" in rejected["error"]
    assert accepted["success"] is True
    assert accepted["model_lock"]["immutable_source"]["sha256"] == source_sha256


def test_model_lock_verify_detects_desktop_readback_change(tmp_path):
    revision_state = {
        "structural": {"components": ["comp1"], "studies": ["std1"]},
        "state": {"parameters": {"gap": "10[nm]"}},
    }
    manager, _ownership, _client = _manager(
        tmp_path, revision_state=revision_state
    )
    lock = _attach_and_lock(manager)
    revision_state["state"] = {"parameters": {"gap": "11[nm]"}}

    result = manager.verify_model_lock(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
    )

    assert result["success"] is False
    assert result["state"] == "model_guard_mismatch"
    assert result["changed_fields"] == ["state_readback"]


def test_model_lock_verify_detects_changed_model_identity(tmp_path):
    models = [
        {"tag": "Model_1", "label": "Shared", "file_path": None, "unsaved": True}
    ]
    manager, _ownership, _client = _manager(tmp_path, models=models)
    lock = _attach_and_lock(manager)
    models[0] = {
        "tag": "Model_1",
        "label": "Changed in Desktop",
        "file_path": None,
        "unsaved": True,
    }

    result = manager.verify_model_lock(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
    )

    assert result["success"] is False
    assert result["changed_fields"] == ["model_identity"]


def test_model_lock_verify_detects_changed_server_identity(tmp_path):
    snapshots = [
        _snapshot(),
        _snapshot(),
        _snapshot(),
        _snapshot(server_created=999.0),
    ]
    manager, _ownership, _client = _manager(tmp_path, snapshots=snapshots)
    lock = _attach_and_lock(manager)

    result = manager.verify_model_lock(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
    )

    assert result["success"] is False
    assert result["changed_fields"] == ["attached_server"]


def test_model_lock_verify_rejects_stale_caller_identities(tmp_path):
    manager, _ownership, _client = _manager(tmp_path)
    lock = _attach_and_lock(manager)

    result = manager.verify_model_lock(
        expected_lock_sha256="0" * 64,
        expected_revision_sha256="1" * 64,
    )

    assert result["success"] is False
    assert result["changed_fields"] == [
        "expected_lock_sha256",
        "expected_revision_sha256",
    ]


def test_unlock_requires_reason_and_leaves_bounded_audit(tmp_path):
    manager, _ownership, _client = _manager(tmp_path)
    lock = _attach_and_lock(manager)

    missing = manager.unlock_model(
        expected_lock_sha256=lock["lock_sha256"], reason="  "
    )
    unlocked = manager.unlock_model(
        expected_lock_sha256=lock["lock_sha256"], reason="Return control to Desktop"
    )

    assert missing == {"success": False, "state": "unlock_reason_required"}
    assert unlocked["success"] is True
    assert unlocked["unlock_audit"]["reason"] == "Return control to Desktop"
    assert len(unlocked["unlock_audit"]["audit_sha256"]) == 64
    assert manager.status()["last_unlock_audit"] == unlocked["unlock_audit"]


def test_detach_refuses_while_model_lock_is_active(tmp_path):
    manager, ownership, client = _manager(tmp_path)
    lock = _attach_and_lock(manager)

    result = manager.detach()

    assert result["success"] is False
    assert result["state"] == "model_lock_active"
    assert client.calls == []
    assert ownership.releases == 0
    assert manager.unlock_model(
        expected_lock_sha256=lock["lock_sha256"], reason="Detach"
    )["success"] is True


def test_save_copy_snapshot_commits_manifest_after_identity_verification(tmp_path):
    manager, _ownership, _client = _manager(tmp_path)
    lock = _attach_and_lock(manager)

    result = manager.snapshot_model(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
        max_snapshot_bytes=1024,
    )
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))

    assert result["success"] is True
    assert result["state"] == "snapshot_complete"
    assert result["identity_preserved"] is True
    assert manifest["complete"] is True
    assert manifest["save_copy_api"] == "Model.java.save(path, True)"
    assert manifest["snapshot"]["sha256"] == result["snapshot_sha256"]
    assert manifest["model"]["file_path"] is None


def test_snapshot_rehashes_and_preserves_declared_immutable_source(tmp_path):
    manager, _ownership, _client = _manager(tmp_path)
    assert manager.attach(
        _request(),
        profile="desktop_shared",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )["success"] is True
    assert manager.adopt_model(_selector())["success"] is True
    source = tmp_path / "source.mph"
    source.write_bytes(b"immutable source bytes")
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    locked = manager.lock_model(
        collaboration_mode="interactive_inspection",
        immutable_source={"path": str(source), "sha256": source_sha256},
    )["model_lock"]

    result = manager.snapshot_model(
        expected_lock_sha256=locked["lock_sha256"],
        expected_revision_sha256=locked["revision"]["revision_sha256"],
        max_snapshot_bytes=1024,
    )

    assert result["success"] is True
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_sha256


def test_snapshot_writer_failure_never_commits_complete_manifest(tmp_path):
    def partial_failure(_client, _tag, target):
        target.write_bytes(b"partial")
        raise RuntimeError("simulated Save Copy failure")

    manager, _ownership, _client = _manager(
        tmp_path, snapshot_writer=partial_failure
    )
    lock = _attach_and_lock(manager)

    result = manager.snapshot_model(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
        max_snapshot_bytes=1024,
    )

    assert result["success"] is False
    assert result["state"] == "snapshot_incomplete"
    assert result["partial_snapshot_exists"] is True
    assert result["complete_manifest_exists"] is False


def test_snapshot_rejects_size_overrun_without_complete_manifest(tmp_path):
    manager, _ownership, _client = _manager(tmp_path)
    lock = _attach_and_lock(manager)

    result = manager.snapshot_model(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
        max_snapshot_bytes=4,
    )

    assert result["success"] is False
    assert "byte limit" in result["error"]
    assert result["partial_snapshot_exists"] is True
    assert result["complete_manifest_exists"] is False


def test_snapshot_detects_model_identity_change_during_save_copy(tmp_path):
    models = [
        {"tag": "Model_1", "label": "Shared", "file_path": None, "unsaved": True}
    ]

    def change_identity(_client, _tag, target):
        target.write_bytes(b"snapshot fixture")
        models[0] = {
            "tag": "Model_1",
            "label": "Changed during Save Copy",
            "file_path": None,
            "unsaved": True,
        }

    manager, _ownership, _client = _manager(
        tmp_path, models=models, snapshot_writer=change_identity
    )
    lock = _attach_and_lock(manager)

    result = manager.snapshot_model(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
        max_snapshot_bytes=1024,
    )

    assert result["success"] is False
    assert "identity or revision changed" in result["error"]
    assert result["complete_manifest_exists"] is False


def test_attach_and_detach_preserve_server_listener_and_model_inventory(tmp_path):
    manager, ownership, client = _manager(tmp_path)

    attached = manager.attach(
        _request(),
        profile="desktop_shared",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )
    status = manager.status()
    detached = manager.detach()

    assert attached["success"] is True
    assert attached["state"] == "attached_model_pending_adoption"
    assert attached["ownership"] == "external_user_owned_server"
    assert attached["can_start_comsol"] is False
    assert status["attached"] is True
    assert detached["success"] is True
    assert detached["external_resources_preserved"] is True
    assert detached["violations"] == []
    assert client.calls == ["disconnect"]
    assert ownership.releases == 1
    assert not ownership.lease_path.exists()


def test_client_construction_failure_releases_only_mcp_lease(tmp_path):
    def fail_client(host, port):
        raise RuntimeError("connection refused")

    manager, ownership, _client = _manager(tmp_path, client_factory=fail_client)

    result = manager.attach(
        _request(),
        profile="desktop_shared",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )

    assert result["success"] is False
    assert result["state"] == "attach_failed"
    assert result["client_disconnected"] is True
    assert ownership.releases == 1
    assert not ownership.lease_path.exists()


def test_zero_models_attach_for_inventory_then_reject_adoption_without_clear(tmp_path):
    manager, ownership, client = _manager(tmp_path, models=[])

    attached = manager.attach(
        _request(),
        profile="desktop_shared",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )

    adoption = manager.adopt_model(_selector())
    result = manager.detach()

    assert attached["success"] is True
    assert attached["model_count"] == 0
    assert adoption["success"] is False
    assert adoption["state"] == "no_server_models"
    assert result["success"] is True
    assert client.calls == ["disconnect"]
    assert ownership.releases == 1


def test_disconnect_failure_keeps_lease_and_reports_uncertain(tmp_path):
    class FailingDisconnect(FakeClient):
        def disconnect(self):
            self.calls.append("disconnect")
            raise RuntimeError("disconnect uncertain")

    client = FailingDisconnect()
    manager, ownership, _ = _manager(tmp_path, client=client)
    attached = manager.attach(
        _request(),
        profile="desktop_shared",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )
    assert manager.adopt_model(_selector())["success"] is True

    result = manager.detach()

    assert attached["success"] is True
    assert result["success"] is False
    assert result["state"] == "detach_uncertain"
    assert result["lease_released"] is False
    assert ownership.releases == 0
    assert ownership.lease_path.exists()


def test_changed_server_identity_after_disconnect_fails_preservation(tmp_path):
    snapshots = [_snapshot(), _snapshot(), _snapshot(server_created=999.0)]
    manager, ownership, client = _manager(tmp_path, snapshots=snapshots)
    assert manager.attach(
        _request(),
        profile="desktop_shared",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )["success"] is True

    result = manager.detach()

    assert result["success"] is False
    assert "external_server_identity_changed" in result["violations"]
    assert client.calls == ["disconnect"]
    assert ownership.releases == 1
