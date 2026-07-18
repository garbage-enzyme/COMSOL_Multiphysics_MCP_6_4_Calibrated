"""Fake-client tests for non-owning shared attach and detach lifecycle."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.shared_session.contracts import SHARED_SERVER_FEATURE_ENV
from src.shared_session.lifecycle import SharedSessionManager
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
        "model_selector": {
            "tag": "Model_1",
            "expected_label": "Shared",
            "expected_unsaved": True,
        },
        "user_confirmed": True,
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


def _inventory(models=None):
    return models if models is not None else [
        {"tag": "Model_1", "label": "Shared", "file_path": None, "unsaved": True}
    ]


def _manager(tmp_path, *, client=None, snapshots=None, models=None, client_factory=None):
    values = iter(snapshots or [_snapshot(), _snapshot(), _snapshot()])
    ownership = FakeOwnership(tmp_path)
    client = client or FakeClient()
    return (
        SharedSessionManager(
            snapshot_provider=lambda: next(values),
            ownership_factory=lambda: ownership,
            client_factory=client_factory or (lambda host, port: client),
            model_inventory_reader=lambda value: _inventory(models),
        ),
        ownership,
        client,
    )


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
    assert attached["state"] == "attached_model_pending_lock"
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


def test_zero_or_nonmatching_models_detach_without_clear(tmp_path):
    manager, ownership, client = _manager(tmp_path, models=[])

    result = manager.attach(
        _request(),
        profile="desktop_shared",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )

    assert result["success"] is False
    assert result["state"] == "no_server_models"
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
