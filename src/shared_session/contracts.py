"""Solver-free contracts for the opt-in shared-server surface."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import ipaddress
import os
from typing import Any, Mapping


SHARED_SERVER_FEATURE_ENV = "COMSOL_MCP_ENABLE_SHARED_SERVER"
SHARED_SERVER_PROFILE = "desktop_shared"
MAX_ENDPOINT_HOST_CHARACTERS = 253

_ENDPOINT_FIELDS = frozenset({"host", "port"})
_TRUE = "true"
_FALSE = "false"


@dataclass(frozen=True)
class SharedServerFeatureGate:
    """One immutable startup decision for the shared-server feature."""

    profile: str
    feature_enabled: bool
    profile_selected: bool
    gate_open: bool
    environment_variable: str = SHARED_SERVER_FEATURE_ENV
    restart_required_after_change: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SharedServerEndpoint:
    """One normalized local endpoint that requires no DNS lookup."""

    host: str
    port: int
    scope: str = "loopback"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize_feature_flag(raw_value: Any) -> bool:
    if not isinstance(raw_value, str):
        raise ValueError(f"{SHARED_SERVER_FEATURE_ENV} must be the string 'true' or 'false'")
    normalized = raw_value.strip().casefold()
    if normalized == _TRUE:
        return True
    if normalized == _FALSE:
        return False
    raise ValueError(f"{SHARED_SERVER_FEATURE_ENV} must be exactly true or false")


def normalize_shared_server_feature_gate(
    profile: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> SharedServerFeatureGate:
    """Normalize the two startup-only gates without importing MPh."""
    if not isinstance(profile, str) or not profile.strip():
        raise ValueError("active profile must be a non-empty string")
    normalized_profile = profile.strip().casefold()
    environment = os.environ if environ is None else environ
    raw_flag = environment.get(SHARED_SERVER_FEATURE_ENV, _FALSE)
    enabled = _normalize_feature_flag(raw_flag)
    selected = normalized_profile == SHARED_SERVER_PROFILE
    return SharedServerFeatureGate(
        profile=normalized_profile,
        feature_enabled=enabled,
        profile_selected=selected,
        gate_open=selected and enabled,
    )


def normalize_shared_server_endpoint(value: Any) -> SharedServerEndpoint:
    """Validate one explicit loopback endpoint without DNS resolution."""
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise ValueError("shared server endpoint must be an object with string keys")
    unknown = sorted(set(value) - _ENDPOINT_FIELDS)
    if unknown:
        raise ValueError(f"shared server endpoint contains unknown fields: {unknown}")
    missing = sorted(_ENDPOINT_FIELDS - set(value))
    if missing:
        raise ValueError(f"shared server endpoint is missing required fields: {missing}")

    raw_host = value["host"]
    if not isinstance(raw_host, str) or not raw_host.strip():
        raise ValueError("shared server endpoint host must be a non-empty string")
    host = raw_host.strip().casefold()
    if len(host) > MAX_ENDPOINT_HOST_CHARACTERS:
        raise ValueError("shared server endpoint host is too long")
    if host == "localhost":
        normalized_host = host
    else:
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError(
                "shared server endpoint host must be a literal loopback address or localhost"
            ) from exc
        if not address.is_loopback:
            raise ValueError("shared server endpoint host must be loopback-only")
        normalized_host = address.compressed

    port = value["port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("shared server endpoint port must be an integer from 1 to 65535")
    return SharedServerEndpoint(host=normalized_host, port=port)


__all__ = [
    "MAX_ENDPOINT_HOST_CHARACTERS",
    "SHARED_SERVER_FEATURE_ENV",
    "SHARED_SERVER_PROFILE",
    "SharedServerEndpoint",
    "SharedServerFeatureGate",
    "normalize_shared_server_endpoint",
    "normalize_shared_server_feature_gate",
]
