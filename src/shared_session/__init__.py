"""Opt-in contracts for non-owning shared COMSOL Server sessions."""

from .contracts import (
    SHARED_SERVER_FEATURE_ENV,
    SHARED_SERVER_PROFILE,
    SharedServerFeatureGate,
    SharedServerEndpoint,
    normalize_shared_server_endpoint,
    normalize_shared_server_feature_gate,
)

__all__ = [
    "SHARED_SERVER_FEATURE_ENV",
    "SHARED_SERVER_PROFILE",
    "SharedServerEndpoint",
    "SharedServerFeatureGate",
    "normalize_shared_server_endpoint",
    "normalize_shared_server_feature_gate",
]
