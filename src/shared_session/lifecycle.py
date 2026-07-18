"""Attached client lifecycle that never starts, clears, or owns COMSOL Server."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any, Callable, Mapping

from .attach_request import normalize_shared_server_attach_request
from .cleanup import evaluate_attached_detach
from .identity import normalize_attached_server_identity
from .locking import (
    build_shared_model_lock,
    build_shared_model_revision,
    normalize_shared_model_identity,
)
from .preflight import (
    classify_shared_server_preflight,
    normalize_shared_preflight_snapshot,
)
from .process_probe import collect_shared_preflight_snapshot


MAX_SERVER_MODELS = 32
MAX_UNLOCK_REASON_CHARACTERS = 512


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _default_ownership_factory():
    from src.tools.ownership import SolverOwnership

    return SolverOwnership()


def _default_client_factory(host: str, port: int):
    import mph

    return mph.Client(host=host, port=port)


def _default_model_inventory_reader(client: Any) -> list[dict[str, Any]]:
    models = list(client.models())
    if len(models) > MAX_SERVER_MODELS:
        raise ValueError(f"server model inventory exceeds {MAX_SERVER_MODELS}")
    inventory = []
    for model in models:
        java = model.java
        tag = str(java.tag())
        label = str(java.label())
        raw_path = str(java.getFilePath())
        path = raw_path if raw_path else None
        inventory.append({
            "tag": tag,
            "label": label,
            "file_path": path,
            "unsaved": path is None,
        })
    return inventory


def _default_model_revision_reader(
    client: Any, model_tag: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    matches = [
        model for model in list(client.models())
        if str(model.java.tag()) == model_tag
    ]
    if len(matches) != 1:
        raise ValueError("adopted server model is no longer uniquely available")
    model = matches[0]
    structural = {
        "components": sorted(str(value) for value in model.components()),
        "studies": sorted(str(value) for value in model.studies()),
        "datasets": sorted(str(value) for value in model.datasets()),
    }
    state = {
        "parameters": {
            str(name): str(value)
            for name, value in sorted(model.parameters().items())
        }
    }
    return structural, state


def _default_mcp_process_identity() -> dict[str, Any]:
    import psutil

    process = psutil.Process(os.getpid())
    try:
        command = list(process.cmdline())
    except (psutil.AccessDenied, psutil.ZombieProcess):
        command = []
    signature = hashlib.sha256(
        "\0".join(str(part) for part in command).encode(
            "utf-8", errors="replace"
        )
    ).hexdigest()
    return {
        "pid": process.pid,
        "process_create_time": process.create_time(),
        "command_signature": signature,
    }


class SharedSessionManager:
    """One process-local facade for an exact non-owned server connection."""

    def __init__(
        self,
        *,
        snapshot_provider: Callable[[], Mapping[str, Any]] = collect_shared_preflight_snapshot,
        ownership_factory: Callable[[], Any] = _default_ownership_factory,
        client_factory: Callable[[str, int], Any] = _default_client_factory,
        model_inventory_reader: Callable[[Any], list[dict[str, Any]]] = _default_model_inventory_reader,
        model_revision_reader: Callable[
            [Any, str], tuple[dict[str, Any], dict[str, Any]]
        ] = _default_model_revision_reader,
        mcp_process_identity_provider: Callable[
            [], Mapping[str, Any]
        ] = _default_mcp_process_identity,
        clock: Callable[[], float] = time.time,
    ):
        self._snapshot_provider = snapshot_provider
        self._ownership_factory = ownership_factory
        self._client_factory = client_factory
        self._model_inventory_reader = model_inventory_reader
        self._model_revision_reader = model_revision_reader
        self._mcp_process_identity_provider = mcp_process_identity_provider
        self._clock = clock
        self._lock = threading.RLock()
        self._client = None
        self._ownership = None
        self._server_identity = None
        self._selector = None
        self._selected_model = None
        self._inventory_sha256 = None
        self._session_acquisition_id = None
        self._model_lock = None
        self._unlock_audit: list[dict[str, Any]] = []

    @staticmethod
    def _inventory(reader: Callable[[Any], list[dict[str, Any]]], client: Any):
        raw = reader(client)
        if not isinstance(raw, list) or len(raw) > MAX_SERVER_MODELS:
            raise ValueError("server model inventory is not a bounded list")
        normalized = sorted(
            (normalize_shared_model_identity(item) for item in raw),
            key=lambda item: item.tag,
        )
        tags = [item.tag for item in normalized]
        if len(tags) != len(set(tags)):
            raise ValueError("server model inventory contains duplicate tags")
        public = [item.to_dict() for item in normalized]
        return normalized, _canonical_sha256(public)

    @staticmethod
    def _matches(selector, model) -> bool:
        return (
            model.tag == selector.tag
            and (
                selector.expected_label is None
                or model.label == selector.expected_label
            )
            and (
                selector.expected_file_path is None
                or model.file_path == selector.expected_file_path
            )
            and (
                selector.expected_unsaved is None
                or model.unsaved is selector.expected_unsaved
            )
        )

    @staticmethod
    def _server_identity_from_snapshot(endpoint, snapshot):
        normalized = normalize_shared_preflight_snapshot(snapshot)
        listeners = [
            item
            for item in normalized["listeners"]
            if item["host"] == endpoint.host and item["port"] == endpoint.port
        ]
        if len(listeners) != 1:
            raise ValueError("declared listener is no longer unique")
        pid = listeners[0]["pid"]
        server = next(
            (
                item for item in normalized["processes"]
                if item["pid"] == pid and item["kind"] == "comsol_server"
            ),
            None,
        )
        if server is None:
            raise ValueError("declared listener owner is not the exact COMSOL Server")
        return normalize_attached_server_identity({
            "endpoint": {"host": endpoint.host, "port": endpoint.port},
            "server_pid": pid,
            "server_process_create_time": server["create_time"],
            "server_command_signature": server["command_signature"],
            "listener_observed_at_epoch": normalized["observed_at_epoch"],
        })

    def attach(
        self,
        request: Mapping[str, Any],
        *,
        profile: str,
        environ: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Attach to one exact existing server and resolve one model selector."""
        normalized_request = normalize_shared_server_attach_request(
            request, profile=profile, environ=environ
        )
        with self._lock:
            if self._client is not None:
                return {
                    "success": False,
                    "state": "already_attached",
                    "error": "A shared server is already attached.",
                }
            first = self._snapshot_provider()
            second = self._snapshot_provider()
            preflight = classify_shared_server_preflight(
                endpoint={
                    "host": normalized_request.endpoint.host,
                    "port": normalized_request.endpoint.port,
                },
                first_probe=first,
                second_probe=second,
            )
            if not preflight["success"]:
                return {**preflight, "success": False, "lease_acquired": False}
            server_identity = self._server_identity_from_snapshot(
                normalized_request.endpoint, second
            )
            ownership = self._ownership_factory()
            lease = ownership.acquire_attached(server_identity)
            if not lease.get("success"):
                return {
                    "success": False,
                    "state": "attached_lease_rejected",
                    "error": lease.get("error", "Attached lease was rejected."),
                    "preflight": preflight,
                    "lease_acquired": False,
                }
            client = None
            try:
                client = self._client_factory(
                    normalized_request.endpoint.host,
                    normalized_request.endpoint.port,
                )
                models, inventory_sha256 = self._inventory(
                    self._model_inventory_reader, client
                )
                matches = [
                    model
                    for model in models
                    if self._matches(normalized_request.model_selector, model)
                ]
                if len(matches) != 1:
                    state = (
                        "no_server_models" if not models
                        else "model_selector_not_unique"
                    )
                    client.disconnect()
                    release = ownership.release()
                    return {
                        "success": False,
                        "state": state,
                        "model_count": len(models),
                        "match_count": len(matches),
                        "client_disconnected": True,
                        "lease_release": release,
                    }
            except Exception as exc:
                disconnected = client is None
                if client is not None:
                    try:
                        client.disconnect()
                        disconnected = True
                    except Exception:
                        disconnected = False
                release = ownership.release() if disconnected else {
                    "success": False,
                    "released": False,
                    "error": "Client disconnect could not be verified.",
                }
                return {
                    "success": False,
                    "state": "attach_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "client_disconnected": disconnected,
                    "lease_release": release,
                }

            self._client = client
            self._ownership = ownership
            self._server_identity = server_identity
            self._selector = normalized_request.model_selector
            self._selected_model = matches[0]
            self._inventory_sha256 = inventory_sha256
            self._session_acquisition_id = lease["lease"]["acquisition_id"]
            return {
                "success": True,
                "state": "attached_model_pending_lock",
                "server_identity_sha256": server_identity.identity_sha256,
                "session_acquisition_id": self._session_acquisition_id,
                "selected_model": matches[0].to_dict(),
                "model_count": len(models),
                "model_inventory_sha256": inventory_sha256,
                "ownership": "external_user_owned_server",
                "can_start_comsol": False,
                "preflight": preflight,
            }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "success": True,
                "attached": self._client is not None,
                "state": (
                    "attached_model_locked"
                    if self._model_lock is not None
                    else (
                        "attached_model_pending_lock"
                        if self._client is not None
                        else "detached"
                    )
                ),
                "server_identity_sha256": (
                    None
                    if self._server_identity is None
                    else self._server_identity.identity_sha256
                ),
                "session_acquisition_id": self._session_acquisition_id,
                "model_inventory_sha256": self._inventory_sha256,
                "ownership": (
                    "external_user_owned_server"
                    if self._client is not None
                    else None
                ),
                "can_start_comsol": False,
                "model_lock": (
                    None if self._model_lock is None else {
                        "lock_id": self._model_lock.lock_id,
                        "lock_sha256": self._model_lock.lock_sha256,
                        "revision_sha256": self._model_lock.revision["revision_sha256"],
                        "collaboration_mode": self._model_lock.collaboration_mode,
                    }
                ),
                "last_unlock_audit": (
                    None if not self._unlock_audit else dict(self._unlock_audit[-1])
                ),
            }

    def models(self) -> dict[str, Any]:
        """Return one bounded fresh inventory without changing the baseline."""
        with self._lock:
            if self._client is None:
                return {
                    "success": False,
                    "state": "detached",
                    "models": [],
                    "model_count": 0,
                }
            try:
                models, inventory_sha256 = self._inventory(
                    self._model_inventory_reader, self._client
                )
            except Exception as exc:
                return {
                    "success": False,
                    "state": "model_inventory_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            return {
                "success": True,
                "state": (
                    "attached_model_locked"
                    if self._model_lock is not None
                    else "attached_model_pending_lock"
                ),
                "models": [model.to_dict() for model in models],
                "model_count": len(models),
                "model_inventory_sha256": inventory_sha256,
                "attached_inventory_sha256": self._inventory_sha256,
            }

    def lock_model(
        self,
        *,
        collaboration_mode: str,
        immutable_source: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Lock the exact adopted model against bounded optimistic readback."""
        with self._lock:
            if self._client is None or self._selected_model is None:
                return {"success": False, "state": "detached"}
            if self._model_lock is not None:
                return {
                    "success": False,
                    "state": "model_already_locked",
                    "lock_sha256": self._model_lock.lock_sha256,
                }
            try:
                server_now = self._server_identity_from_snapshot(
                    self._server_identity.endpoint, self._snapshot_provider()
                )
                if server_now.identity_sha256 != self._server_identity.identity_sha256:
                    raise RuntimeError("attached server identity changed before model lock")
                models, _inventory_sha256 = self._inventory(
                    self._model_inventory_reader, self._client
                )
                matches = [
                    model for model in models
                    if model.tag == self._selected_model.tag
                ]
                if len(matches) != 1:
                    raise RuntimeError("adopted model tag is no longer unique")
                current_model = matches[0]
                if current_model.identity_sha256 != self._selected_model.identity_sha256:
                    raise RuntimeError("adopted model identity changed before model lock")
                structural, state = self._model_revision_reader(
                    self._client, current_model.tag
                )
                revision = build_shared_model_revision(
                    current_model,
                    sequence=0,
                    structural_readback=structural,
                    state_readback=state,
                )
                model_lock = build_shared_model_lock(
                    attached_server=self._server_identity,
                    session_acquisition_id=self._session_acquisition_id,
                    model=current_model,
                    revision=revision,
                    collaboration_mode=collaboration_mode,
                    immutable_source=immutable_source,
                    lock_created_at_epoch=self._clock(),
                    mcp_process=self._mcp_process_identity_provider(),
                )
            except Exception as exc:
                return {
                    "success": False,
                    "state": "model_lock_rejected",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            self._model_lock = model_lock
            return {
                "success": True,
                "state": "attached_model_locked",
                "model_lock": model_lock.to_dict(),
            }

    def verify_model_lock(
        self, *, expected_lock_sha256: str, expected_revision_sha256: str
    ) -> dict[str, Any]:
        """Re-read exact server/model/revision identities and fail closed."""
        with self._lock:
            if self._model_lock is None or self._client is None:
                return {"success": False, "state": "model_not_locked"}
            changed_fields = []
            if expected_lock_sha256 != self._model_lock.lock_sha256:
                changed_fields.append("expected_lock_sha256")
            locked_revision_sha256 = self._model_lock.revision["revision_sha256"]
            if expected_revision_sha256 != locked_revision_sha256:
                changed_fields.append("expected_revision_sha256")
            try:
                server_now = self._server_identity_from_snapshot(
                    self._server_identity.endpoint, self._snapshot_provider()
                )
                if server_now.identity_sha256 != self._server_identity.identity_sha256:
                    changed_fields.append("attached_server")
                models, _inventory_sha256 = self._inventory(
                    self._model_inventory_reader, self._client
                )
                matches = [
                    model for model in models
                    if model.tag == self._selected_model.tag
                ]
                if len(matches) != 1:
                    changed_fields.append("model_tag")
                else:
                    current_model = matches[0]
                    if current_model.identity_sha256 != self._selected_model.identity_sha256:
                        changed_fields.append("model_identity")
                    else:
                        structural, state = self._model_revision_reader(
                            self._client, current_model.tag
                        )
                        current_revision = build_shared_model_revision(
                            current_model,
                            sequence=self._model_lock.revision["sequence"],
                            structural_readback=structural,
                            state_readback=state,
                        )
                        if current_revision.structural_sha256 != self._model_lock.revision["structural_sha256"]:
                            changed_fields.append("structural_readback")
                        if current_revision.readback_sha256 != self._model_lock.revision["readback_sha256"]:
                            changed_fields.append("state_readback")
            except Exception as exc:
                return {
                    "success": False,
                    "state": "model_lock_verification_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "changed_fields": sorted(set(changed_fields)),
                }
            changed_fields = sorted(set(changed_fields))
            return {
                "success": not changed_fields,
                "state": "model_lock_verified" if not changed_fields else "model_guard_mismatch",
                "changed_fields": changed_fields,
                "lock_sha256": self._model_lock.lock_sha256,
                "revision_sha256": locked_revision_sha256,
            }

    def unlock_model(
        self, *, expected_lock_sha256: str, reason: str
    ) -> dict[str, Any]:
        """Release only the MCP guard and retain one bounded audit record."""
        with self._lock:
            if self._model_lock is None:
                return {"success": False, "state": "model_not_locked"}
            if expected_lock_sha256 != self._model_lock.lock_sha256:
                return {"success": False, "state": "model_lock_identity_mismatch"}
            if not isinstance(reason, str) or not reason.strip():
                return {"success": False, "state": "unlock_reason_required"}
            normalized_reason = reason.strip()
            if len(normalized_reason) > MAX_UNLOCK_REASON_CHARACTERS:
                return {"success": False, "state": "unlock_reason_too_long"}
            body = {
                "lock_id": self._model_lock.lock_id,
                "lock_sha256": self._model_lock.lock_sha256,
                "reason": normalized_reason,
                "unlocked_at_epoch": self._clock(),
            }
            audit = {**body, "audit_sha256": _canonical_sha256(body)}
            self._unlock_audit.append(audit)
            self._unlock_audit = self._unlock_audit[-32:]
            self._model_lock = None
            return {
                "success": True,
                "state": "attached_model_pending_lock",
                "unlock_audit": audit,
            }

    def detach(self) -> dict[str, Any]:
        """Disconnect only the MCP client and prove external preservation."""
        with self._lock:
            if self._client is None:
                return {"success": True, "state": "detached", "detached": False}
            if self._model_lock is not None:
                return {
                    "success": False,
                    "state": "model_lock_active",
                    "error": "Unlock the shared model before detaching.",
                }
            try:
                _models, inventory_after = self._inventory(
                    self._model_inventory_reader, self._client
                )
                self._client.disconnect()
            except Exception as exc:
                return {
                    "success": False,
                    "state": "detach_uncertain",
                    "error": f"{type(exc).__name__}: {exc}",
                    "lease_released": False,
                }
            release = self._ownership.release()
            snapshot = self._snapshot_provider()
            try:
                server_after = self._server_identity_from_snapshot(
                    self._server_identity.endpoint, snapshot
                )
                listener_active = True
            except ValueError:
                server_after = None
                listener_active = False
            outcome = evaluate_attached_detach(
                server_before=self._server_identity,
                server_after=server_after,
                model_inventory_before_sha256=self._inventory_sha256,
                model_inventory_after_sha256=inventory_after,
                client_disconnected=True,
                lease_released=bool(release.get("success") and not self._ownership.lease_path.exists()),
                listener_active_after=listener_active,
                model_clear_attempted=False,
                external_server_shutdown_attempted=False,
                external_server_termination_attempted=False,
            )
            self._client = None
            self._ownership = None
            self._server_identity = None
            self._selector = None
            self._selected_model = None
            self._inventory_sha256 = None
            self._session_acquisition_id = None
            self._model_lock = None
            return {
                **outcome.to_dict(),
                "state": "detached" if outcome.success else "detached_preservation_failed",
                "detach_release": release,
            }


__all__ = ["SharedSessionManager"]
