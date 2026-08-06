"""Fail-closed normalization for one explicit shared-server attach request."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from comsol_mcp.utils.immutability import deep_freeze, deep_thaw

from .contracts import (
    SharedServerEndpoint,
    normalize_shared_server_endpoint,
    normalize_shared_server_feature_gate,
)
_ATTACH_REQUEST_FIELDS = frozenset({"endpoint", "user_confirmed"})


@dataclass(frozen=True)
class SharedServerAttachRequest:
    """One normalized request that is eligible for process preflight."""

    endpoint: SharedServerEndpoint
    user_confirmed: bool
    feature_gate: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "feature_gate", deep_freeze(self.feature_gate))

    def to_dict(self) -> dict[str, Any]:
        return deep_thaw(asdict(self))


def normalize_shared_server_attach_request(
    value: Any,
    *,
    profile: str,
    environ: Mapping[str, str] | None = None,
    feature_enabled: bool | None = None,
) -> SharedServerAttachRequest:
    """Require every static and per-call gate before lease acquisition."""
    gate = normalize_shared_server_feature_gate(
        profile,
        environ=environ,
        feature_enabled=feature_enabled,
    )
    if not gate.gate_open:
        raise ValueError("shared server attach requires the static feature flag")
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise ValueError("shared server attach request must be an object with string keys")
    actual = set(value)
    if actual != _ATTACH_REQUEST_FIELDS:
        missing = sorted(_ATTACH_REQUEST_FIELDS - actual)
        unknown = sorted(actual - _ATTACH_REQUEST_FIELDS)
        raise ValueError(
            f"shared server attach request fields are invalid; missing={missing}, unknown={unknown}"
        )
    if not isinstance(value["user_confirmed"], bool) or value["user_confirmed"] is not True:
        raise ValueError("shared server attach requires user_confirmed=true")
    endpoint = normalize_shared_server_endpoint(value["endpoint"])
    return SharedServerAttachRequest(
        endpoint=endpoint,
        user_confirmed=True,
        feature_gate=gate.to_dict(),
    )


__all__ = [
    "SharedServerAttachRequest",
    "normalize_shared_server_attach_request",
]
