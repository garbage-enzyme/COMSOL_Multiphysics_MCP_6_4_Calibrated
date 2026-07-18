"""Opt-in contracts for non-owning shared COMSOL Server sessions."""

from .contracts import (
    SHARED_SERVER_FEATURE_ENV,
    SHARED_SERVER_PROFILE,
    SharedServerFeatureGate,
    SharedServerEndpoint,
    normalize_shared_server_endpoint,
    normalize_shared_server_feature_gate,
)
from .identity import (
    AttachedServerIdentity,
    SharedModelSelector,
    normalize_attached_server_identity,
    normalize_shared_model_selector,
)
from .locking import (
    SharedModelIdentity,
    SharedModelLock,
    SharedModelRevision,
    build_shared_model_lock,
    build_shared_model_revision,
    normalize_shared_model_identity,
)
from .cleanup import (
    CleanupOutcome,
    evaluate_attached_detach,
    evaluate_owned_cleanup,
)

__all__ = [
    "SHARED_SERVER_FEATURE_ENV",
    "SHARED_SERVER_PROFILE",
    "AttachedServerIdentity",
    "CleanupOutcome",
    "SharedModelIdentity",
    "SharedModelLock",
    "SharedModelRevision",
    "SharedModelSelector",
    "SharedServerEndpoint",
    "SharedServerFeatureGate",
    "build_shared_model_lock",
    "build_shared_model_revision",
    "evaluate_attached_detach",
    "evaluate_owned_cleanup",
    "normalize_attached_server_identity",
    "normalize_shared_model_identity",
    "normalize_shared_model_selector",
    "normalize_shared_server_endpoint",
    "normalize_shared_server_feature_gate",
]
