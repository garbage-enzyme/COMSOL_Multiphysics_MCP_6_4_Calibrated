"""Pure identity contracts for attached servers and shared model selection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import ntpath
import re
from typing import Any, Mapping

from src.durable import canonical_sha256_v1

from .contracts import (
    LISTENER_BIND_SCOPE_LOOPBACK,
    LISTENER_BIND_SCOPE_WILDCARD,
    SharedServerEndpoint,
    normalize_shared_server_endpoint,
)


MAX_MODEL_LABEL_CHARACTERS = 512
MAX_MODEL_PATH_CHARACTERS = 4096
MAX_MODEL_TAG_CHARACTERS = 128

_ATTACHED_IDENTITY_FIELDS = frozenset(
    {
        "endpoint",
        "server_pid",
        "server_process_create_time",
        "server_command_signature",
        "listener_bind_scope",
        "listener_observed_at_epoch",
    }
)
_MODEL_SELECTOR_FIELDS = frozenset(
    {"tag", "expected_label", "expected_file_path", "expected_unsaved"}
)
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_MODEL_TAG = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_WINDOWS_DEVICE_PATH = re.compile(r"^(?:\\\\[?.]\\|//[?.]/)")


def _mapping(value: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise ValueError(f"{label} must be an object with string keys")
    unknown = sorted(set(value) - fields)
    if unknown:
        raise ValueError(f"{label} contains unknown fields: {unknown}")
    return dict(value)


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _positive_finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be positive and finite")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{label} must be positive and finite")
    return normalized


def _bounded_text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    if len(value) > maximum:
        raise ValueError(f"{label} exceeds {maximum} characters")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{label} contains control characters")
    return value


@dataclass(frozen=True)
class AttachedServerIdentity:
    """Exact non-owned listener/process identity observed before attach."""

    endpoint: SharedServerEndpoint
    server_pid: int
    server_process_create_time: float
    server_command_signature: str
    listener_bind_scope: str
    listener_observed_at_epoch: float
    identity_sha256: str
    ownership: str = "external_user_owned"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SharedModelSelector:
    """Exact tag selector with optional label and saved-state confirmations."""

    tag: str
    expected_label: str | None
    expected_file_path: str | None
    expected_unsaved: bool | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_attached_server_identity(value: Any) -> AttachedServerIdentity:
    """Normalize fresh process evidence without treating it as owned state."""
    raw = _mapping(value, _ATTACHED_IDENTITY_FIELDS, "attached server identity")
    missing = sorted(_ATTACHED_IDENTITY_FIELDS - set(raw))
    if missing:
        raise ValueError(f"attached server identity is missing required fields: {missing}")
    endpoint = normalize_shared_server_endpoint(raw["endpoint"])
    signature = raw["server_command_signature"]
    if not isinstance(signature, str) or not _HEX64.fullmatch(signature):
        raise ValueError("server command signature must be exactly 64 hexadecimal characters")
    listener_bind_scope = raw["listener_bind_scope"]
    if listener_bind_scope not in {
        LISTENER_BIND_SCOPE_LOOPBACK,
        LISTENER_BIND_SCOPE_WILDCARD,
    }:
        raise ValueError("listener bind scope must be loopback or wildcard")
    identity_body = {
        "endpoint": endpoint.to_dict(),
        "server_pid": _positive_integer(raw["server_pid"], "server PID"),
        "server_process_create_time": _positive_finite(
            raw["server_process_create_time"], "server process creation time"
        ),
        "server_command_signature": signature.casefold(),
        "listener_bind_scope": listener_bind_scope,
        "ownership": "external_user_owned",
    }
    observed_at = _positive_finite(
        raw["listener_observed_at_epoch"], "listener observation time"
    )
    return AttachedServerIdentity(
        endpoint=endpoint,
        server_pid=identity_body["server_pid"],
        server_process_create_time=identity_body["server_process_create_time"],
        server_command_signature=identity_body["server_command_signature"],
        listener_bind_scope=identity_body["listener_bind_scope"],
        listener_observed_at_epoch=observed_at,
        identity_sha256=canonical_sha256_v1(identity_body),
    )


def _normalize_confirmed_model_path(value: Any) -> str:
    path = _bounded_text(
        value, "expected model file path", MAX_MODEL_PATH_CHARACTERS
    )
    if _WINDOWS_DEVICE_PATH.match(path):
        raise ValueError("expected model file path cannot be a device path")
    if not ntpath.isabs(path):
        raise ValueError("expected model file path must be absolute")
    normalized = ntpath.normpath(path)
    if normalized.endswith((" ", ".")):
        raise ValueError("expected model file path has an ambiguous trailing character")
    return normalized


def normalize_shared_model_selector(value: Any) -> SharedModelSelector:
    """Normalize an exact model tag plus optional identity confirmations."""
    raw = _mapping(value, _MODEL_SELECTOR_FIELDS, "shared model selector")
    if "tag" not in raw:
        raise ValueError("shared model selector requires an exact model tag")
    tag = _bounded_text(raw["tag"], "model tag", MAX_MODEL_TAG_CHARACTERS)
    if not _MODEL_TAG.fullmatch(tag):
        raise ValueError("model tag must be a bounded clientapi tag")

    expected_label = raw.get("expected_label")
    if expected_label is not None:
        expected_label = _bounded_text(
            expected_label, "expected model label", MAX_MODEL_LABEL_CHARACTERS
        )
    expected_file_path = raw.get("expected_file_path")
    if expected_file_path is not None:
        expected_file_path = _normalize_confirmed_model_path(expected_file_path)
    expected_unsaved = raw.get("expected_unsaved")
    if expected_unsaved is not None and not isinstance(expected_unsaved, bool):
        raise ValueError("expected_unsaved must be boolean when provided")
    if expected_file_path is not None and expected_unsaved is not None:
        raise ValueError(
            "expected_file_path and expected_unsaved are mutually exclusive confirmations"
        )
    if expected_unsaved is False:
        raise ValueError(
            "expected_unsaved=false is ambiguous; provide expected_file_path or omit it"
        )
    return SharedModelSelector(
        tag=tag,
        expected_label=expected_label,
        expected_file_path=expected_file_path,
        expected_unsaved=expected_unsaved,
    )


__all__ = [
    "AttachedServerIdentity",
    "MAX_MODEL_LABEL_CHARACTERS",
    "MAX_MODEL_PATH_CHARACTERS",
    "MAX_MODEL_TAG_CHARACTERS",
    "SharedModelSelector",
    "normalize_attached_server_identity",
    "normalize_shared_model_selector",
]
