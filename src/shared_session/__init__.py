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
from .attach_request import (
    SharedServerAttachRequest,
    normalize_shared_server_attach_request,
)
from .preflight import (
    classify_shared_server_preflight,
    normalize_shared_preflight_snapshot,
)
from .process_probe import collect_shared_preflight_snapshot
from .lifecycle import SharedSessionManager

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
    "SharedServerAttachRequest",
    "SharedServerFeatureGate",
    "SharedSessionManager",
    "build_shared_model_lock",
    "build_shared_model_revision",
    "classify_shared_server_preflight",
    "collect_shared_preflight_snapshot",
    "evaluate_attached_detach",
    "evaluate_owned_cleanup",
    "normalize_attached_server_identity",
    "normalize_shared_model_identity",
    "normalize_shared_model_selector",
    "normalize_shared_preflight_snapshot",
    "normalize_shared_server_endpoint",
    "normalize_shared_server_attach_request",
    "normalize_shared_server_feature_gate",
]
