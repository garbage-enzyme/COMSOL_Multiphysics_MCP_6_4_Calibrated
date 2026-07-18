"""Solver-free target checks used before an attached durable worker mutates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from src.shared_session.identity import (
    AttachedServerIdentity,
    normalize_attached_server_identity,
)
from src.shared_session.locking import (
    SharedModelIdentity,
    build_shared_model_revision,
    normalize_shared_model_identity,
)

from .attached_backend import normalize_attached_execution_backend


@dataclass(frozen=True)
class AttachedExecutionTarget:
    """Normalized immutable target carried by one attached job attempt."""

    backend: dict[str, Any]
    server: AttachedServerIdentity
    model: SharedModelIdentity
    expected_revision: dict[str, Any]


def normalize_attached_execution_target(value: Any) -> AttachedExecutionTarget:
    """Normalize a persisted backend and restore its exact server/model types."""
    backend = normalize_attached_execution_backend(value)
    server_raw = backend["attached_server"]
    endpoint = server_raw["endpoint"]
    server = normalize_attached_server_identity(
        {
            "endpoint": {"host": endpoint["host"], "port": endpoint["port"]},
            "server_pid": server_raw["server_pid"],
            "server_process_create_time": server_raw["server_process_create_time"],
            "server_command_signature": server_raw["server_command_signature"],
            "listener_bind_scope": server_raw["listener_bind_scope"],
            "listener_observed_at_epoch": server_raw["listener_observed_at_epoch"],
        }
    )
    model = normalize_shared_model_identity(
        {
            "tag": backend["model"]["tag"],
            "label": backend["model"]["label"],
            "file_path": backend["model"]["file_path"],
            "unsaved": backend["model"]["unsaved"],
        }
    )
    return AttachedExecutionTarget(
        backend=backend,
        server=server,
        model=model,
        expected_revision=dict(backend["expected_revision"]),
    )


def verify_attached_model_inventory(
    target: AttachedExecutionTarget,
    inventory: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Require exactly one server model with the persisted tag and identity."""
    normalized = []
    for item in inventory:
        raw = dict(item)
        supplied_identity = raw.pop("identity_sha256", None)
        model = normalize_shared_model_identity(raw)
        if supplied_identity is not None and supplied_identity != model.identity_sha256:
            raise ValueError("server model identity SHA-256 does not match its fields")
        normalized.append(model)
    tag_matches = [model for model in normalized if model.tag == target.model.tag]
    if len(tag_matches) != 1:
        state = "no_matching_server_model" if not tag_matches else "server_model_not_unique"
        description = (
            "server model is unavailable"
            if not tag_matches
            else "server model is not unique"
        )
        raise ValueError(
            f"{description} ({state}): expected tag={target.model.tag!r} "
            "and exact model identity"
        )
    if tag_matches[0].identity_sha256 != target.model.identity_sha256:
        raise ValueError("server model identity changed for the expected tag")
    return tag_matches[0].to_dict()


def verify_attached_model_revision(
    target: AttachedExecutionTarget,
    *,
    structural_readback: Mapping[str, Any],
    state_readback: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare a fresh bounded readback against the persisted initial revision."""
    current = build_shared_model_revision(
        target.model,
        sequence=int(target.expected_revision["sequence"]),
        structural_readback=structural_readback,
        state_readback=state_readback,
    )
    expected = target.expected_revision
    changed = [
        field
        for field in ("model_identity_sha256", "structural_sha256", "readback_sha256")
        if current.to_dict()[field] != expected[field]
    ]
    if changed:
        raise ValueError(
            "attached model revision changed: " + ", ".join(changed)
        )
    return current.to_dict()


__all__ = [
    "AttachedExecutionTarget",
    "normalize_attached_execution_target",
    "verify_attached_model_inventory",
    "verify_attached_model_revision",
]
