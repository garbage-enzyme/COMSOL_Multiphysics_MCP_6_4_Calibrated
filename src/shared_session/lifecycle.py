"""Attached client lifecycle that never starts, clears, or owns COMSOL Server."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Callable, Mapping

from .attach_request import normalize_shared_server_attach_request
from .cleanup import evaluate_attached_detach
from .identity import normalize_attached_server_identity
from .locking import normalize_shared_model_identity
from .preflight import (
    classify_shared_server_preflight,
    normalize_shared_preflight_snapshot,
)
from .process_probe import collect_shared_preflight_snapshot


MAX_SERVER_MODELS = 32


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
        path = model.file()
        inventory.append({
            "tag": tag,
            "label": label,
            "file_path": None if path is None else str(path),
            "unsaved": path is None,
        })
    return inventory


class SharedSessionManager:
    """One process-local facade for an exact non-owned server connection."""

    def __init__(
        self,
        *,
        snapshot_provider: Callable[[], Mapping[str, Any]] = collect_shared_preflight_snapshot,
        ownership_factory: Callable[[], Any] = _default_ownership_factory,
        client_factory: Callable[[str, int], Any] = _default_client_factory,
        model_inventory_reader: Callable[[Any], list[dict[str, Any]]] = _default_model_inventory_reader,
        clock: Callable[[], float] = time.time,
    ):
        self._snapshot_provider = snapshot_provider
        self._ownership_factory = ownership_factory
        self._client_factory = client_factory
        self._model_inventory_reader = model_inventory_reader
        self._clock = clock
        self._lock = threading.RLock()
        self._client = None
        self._ownership = None
        self._server_identity = None
        self._selector = None
        self._selected_model = None
        self._inventory_sha256 = None
        self._session_acquisition_id = None

    @staticmethod
    def _inventory(reader: Callable[[Any], list[dict[str, Any]]], client: Any):
        raw = reader(client)
        if not isinstance(raw, list) or len(raw) > MAX_SERVER_MODELS:
            raise ValueError("server model inventory is not a bounded list")
        normalized = [normalize_shared_model_identity(item) for item in raw]
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
                    "attached_model_pending_lock"
                    if self._client is not None
                    else "detached"
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
            }

    def detach(self) -> dict[str, Any]:
        """Disconnect only the MCP client and prove external preservation."""
        with self._lock:
            if self._client is None:
                return {"success": True, "state": "detached", "detached": False}
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
            return {
                **outcome.to_dict(),
                "state": "detached" if outcome.success else "detached_preservation_failed",
                "detach_release": release,
            }


__all__ = ["SharedSessionManager"]
