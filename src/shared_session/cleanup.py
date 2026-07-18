"""Pure owned-cleanup and non-owning detach outcome contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any

from .identity import AttachedServerIdentity


CLEANUP_OUTCOME_SCHEMA = "comsol_mcp.cleanup_outcome"
CLEANUP_OUTCOME_VERSION = "1.0.0"

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ValueError(f"{label} must be exactly 64 hexadecimal characters")
    return value.casefold()


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be boolean")
    return value


@dataclass(frozen=True)
class CleanupOutcome:
    """Machine-readable proof result for one cleanup ownership mode."""

    schema_name: str
    schema_version: str
    resource_mode: str
    success: bool
    client_disconnected: bool
    lease_released: bool
    external_resources_preserved: bool | None
    owned_resources_absent: bool | None
    violations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["violations"] = list(self.violations)
        return value


def evaluate_attached_detach(
    *,
    server_before: AttachedServerIdentity,
    server_after: AttachedServerIdentity | None,
    model_inventory_before_sha256: str,
    model_inventory_after_sha256: str | None,
    client_disconnected: bool,
    lease_released: bool,
    listener_active_after: bool,
    model_clear_attempted: bool,
    external_server_shutdown_attempted: bool,
    external_server_termination_attempted: bool,
) -> CleanupOutcome:
    """Verify detach preserved the exact user-owned server and model inventory."""
    before_inventory = _hash(
        model_inventory_before_sha256, "model inventory before SHA-256"
    )
    after_inventory = (
        None
        if model_inventory_after_sha256 is None
        else _hash(model_inventory_after_sha256, "model inventory after SHA-256")
    )
    disconnected = _boolean(client_disconnected, "client_disconnected")
    released = _boolean(lease_released, "lease_released")
    listener_active = _boolean(listener_active_after, "listener_active_after")
    forbidden_actions = {
        "model_clear_attempted": _boolean(
            model_clear_attempted, "model_clear_attempted"
        ),
        "external_server_shutdown_attempted": _boolean(
            external_server_shutdown_attempted,
            "external_server_shutdown_attempted",
        ),
        "external_server_termination_attempted": _boolean(
            external_server_termination_attempted,
            "external_server_termination_attempted",
        ),
    }

    violations: list[str] = []
    if not disconnected:
        violations.append("mcp_client_still_connected")
    if not released:
        violations.append("attached_lease_not_released")
    if server_after is None:
        violations.append("external_server_identity_unavailable_after_detach")
    elif server_after.identity_sha256 != server_before.identity_sha256:
        violations.append("external_server_identity_changed")
    if not listener_active:
        violations.append("external_listener_not_preserved")
    if after_inventory is None:
        violations.append("model_inventory_unavailable_after_detach")
    elif after_inventory != before_inventory:
        violations.append("server_model_inventory_changed")
    violations.extend(name for name, attempted in forbidden_actions.items() if attempted)

    preserved = not any(
        violation
        in {
            "external_server_identity_unavailable_after_detach",
            "external_server_identity_changed",
            "external_listener_not_preserved",
            "model_inventory_unavailable_after_detach",
            "server_model_inventory_changed",
            *forbidden_actions,
        }
        for violation in violations
    )
    return CleanupOutcome(
        schema_name=CLEANUP_OUTCOME_SCHEMA,
        schema_version=CLEANUP_OUTCOME_VERSION,
        resource_mode="attached_server",
        success=not violations,
        client_disconnected=disconnected,
        lease_released=released,
        external_resources_preserved=preserved,
        owned_resources_absent=None,
        violations=tuple(violations),
    )


def evaluate_owned_cleanup(
    *,
    client_disconnected: bool,
    lease_released: bool,
    owned_server_process_active_after: bool,
    owned_listener_active_after: bool,
    owned_models_present_after: bool,
) -> CleanupOutcome:
    """Verify standalone resources owned by MCP are absent after cleanup."""
    disconnected = _boolean(client_disconnected, "client_disconnected")
    released = _boolean(lease_released, "lease_released")
    process_active = _boolean(
        owned_server_process_active_after, "owned_server_process_active_after"
    )
    listener_active = _boolean(
        owned_listener_active_after, "owned_listener_active_after"
    )
    models_present = _boolean(owned_models_present_after, "owned_models_present_after")
    violations: list[str] = []
    if not disconnected:
        violations.append("mcp_client_still_connected")
    if not released:
        violations.append("owned_lease_not_released")
    if process_active:
        violations.append("owned_server_process_still_active")
    if listener_active:
        violations.append("owned_listener_still_active")
    if models_present:
        violations.append("owned_models_still_present")
    return CleanupOutcome(
        schema_name=CLEANUP_OUTCOME_SCHEMA,
        schema_version=CLEANUP_OUTCOME_VERSION,
        resource_mode="owned_standalone",
        success=not violations,
        client_disconnected=disconnected,
        lease_released=released,
        external_resources_preserved=None,
        owned_resources_absent=not any(
            (process_active, listener_active, models_present)
        ),
        violations=tuple(violations),
    )


__all__ = [
    "CLEANUP_OUTCOME_SCHEMA",
    "CLEANUP_OUTCOME_VERSION",
    "CleanupOutcome",
    "evaluate_attached_detach",
    "evaluate_owned_cleanup",
]
