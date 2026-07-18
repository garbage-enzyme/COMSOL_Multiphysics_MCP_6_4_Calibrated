"""Deterministic revision and lock identities for one shared server model."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import math
import re
from typing import Any, Mapping

from comsol_mcp.durable import canonical_json_v1, canonical_sha256_v1

from .identity import (
    AttachedServerIdentity,
    MAX_MODEL_LABEL_CHARACTERS,
    MAX_MODEL_TAG_CHARACTERS,
    _normalize_confirmed_model_path,
)


SHARED_MODEL_LOCK_SCHEMA = "comsol_mcp.shared_model_lock"
SHARED_MODEL_LOCK_VERSION = "1.0.0"
MAX_REVISION_READBACK_BYTES = 64 * 1024
MAX_REVISION_COLLECTION_ITEMS = 256
MAX_REVISION_DEPTH = 8
MAX_REVISION_TEXT_CHARACTERS = 4096

_MODEL_IDENTITY_FIELDS = frozenset({"tag", "label", "file_path", "unsaved"})
_PROCESS_IDENTITY_FIELDS = frozenset(
    {"pid", "process_create_time", "command_signature"}
)
_SOURCE_IDENTITY_FIELDS = frozenset({"path", "sha256"})
_COLLABORATION_MODES = frozenset(
    {"interactive_inspection", "automation_exclusive"}
)
_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_MODEL_TAG = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _canonical_bytes(value: Any) -> bytes:
    return canonical_json_v1(value)


def _sha256(value: Any) -> str:
    return canonical_sha256_v1(value)


def _exact_mapping(value: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise ValueError(f"{label} must be an object with string keys")
    actual = set(value)
    if actual != fields:
        missing = sorted(fields - actual)
        unknown = sorted(actual - fields)
        raise ValueError(
            f"{label} fields are invalid; missing={missing}, unknown={unknown}"
        )
    return dict(value)


def _bounded_text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    if len(value) > maximum:
        raise ValueError(f"{label} exceeds {maximum} characters")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{label} contains control characters")
    return value


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _positive_finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be positive and finite")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be positive and finite")
    return number


def _hex64(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ValueError(f"{label} must be exactly 64 hexadecimal characters")
    return value.casefold()


def _normalize_json_value(value: Any, label: str, depth: int = 0) -> Any:
    if depth > MAX_REVISION_DEPTH:
        raise ValueError(f"{label} exceeds the maximum nesting depth")
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} contains a non-finite number")
        return value
    if isinstance(value, str):
        if len(value) > MAX_REVISION_TEXT_CHARACTERS:
            raise ValueError(f"{label} contains an oversized string")
        if "\x00" in value:
            raise ValueError(f"{label} contains a NUL character")
        return value
    if isinstance(value, list):
        if len(value) > MAX_REVISION_COLLECTION_ITEMS:
            raise ValueError(f"{label} contains an oversized list")
        return [
            _normalize_json_value(item, f"{label}[{index}]", depth + 1)
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping) and all(isinstance(key, str) for key in value):
        if len(value) > MAX_REVISION_COLLECTION_ITEMS:
            raise ValueError(f"{label} contains an oversized object")
        return {
            key: _normalize_json_value(item, f"{label}.{key}", depth + 1)
            for key, item in sorted(value.items())
        }
    raise ValueError(f"{label} must contain only bounded JSON values")


def _normalize_readback(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{label} must be a non-empty JSON object")
    normalized = _normalize_json_value(value, label)
    if len(_canonical_bytes(normalized)) > MAX_REVISION_READBACK_BYTES:
        raise ValueError(f"{label} exceeds {MAX_REVISION_READBACK_BYTES} bytes")
    return normalized


@dataclass(frozen=True)
class SharedModelIdentity:
    """Exact visible tag, label, and saved/unsaved state."""

    tag: str
    label: str
    file_path: str | None
    unsaved: bool
    identity_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SharedModelRevision:
    """Bounded structural and mutable-state readback fingerprint."""

    sequence: int
    model_identity_sha256: str
    structural_sha256: str
    readback_sha256: str
    revision_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SharedModelLock:
    """One MCP enforcement guard; this is not a COMSOL database lock."""

    schema_name: str
    schema_version: str
    lock_id: str
    attached_server: dict[str, Any]
    session_acquisition_id: str
    model: dict[str, Any]
    revision: dict[str, Any]
    collaboration_mode: str
    immutable_source: dict[str, str] | None
    lock_created_at_epoch: float
    mcp_process: dict[str, Any]
    lock_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_shared_model_identity(value: Any) -> SharedModelIdentity:
    """Normalize an observed model identity with explicit saved state."""
    raw = _exact_mapping(value, _MODEL_IDENTITY_FIELDS, "shared model identity")
    tag = _bounded_text(raw["tag"], "model tag", MAX_MODEL_TAG_CHARACTERS)
    if not _MODEL_TAG.fullmatch(tag):
        raise ValueError("model tag must be a bounded clientapi tag")
    label = _bounded_text(raw["label"], "model label", MAX_MODEL_LABEL_CHARACTERS)
    if not isinstance(raw["unsaved"], bool):
        raise ValueError("model unsaved state must be boolean")
    unsaved = raw["unsaved"]
    file_path = raw["file_path"]
    if unsaved:
        if file_path is not None:
            raise ValueError("an unsaved model cannot have a file path")
        normalized_path = None
    else:
        if file_path is None:
            raise ValueError("a saved model requires an exact file path")
        normalized_path = _normalize_confirmed_model_path(file_path)
    body = {
        "tag": tag,
        "label": label,
        "file_path": normalized_path,
        "unsaved": unsaved,
    }
    return SharedModelIdentity(**body, identity_sha256=_sha256(body))


def build_shared_model_revision(
    model: SharedModelIdentity,
    *,
    sequence: int,
    structural_readback: Mapping[str, Any],
    state_readback: Mapping[str, Any],
) -> SharedModelRevision:
    """Hash bounded readbacks into one optimistic-concurrency revision."""
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise ValueError("shared model revision sequence must be a nonnegative integer")
    structural = _normalize_readback(structural_readback, "structural readback")
    state = _normalize_readback(state_readback, "state readback")
    body = {
        "sequence": sequence,
        "model_identity_sha256": model.identity_sha256,
        "structural_sha256": _sha256(structural),
        "readback_sha256": _sha256(state),
    }
    return SharedModelRevision(**body, revision_sha256=_sha256(body))


def _normalize_process_identity(value: Any) -> dict[str, Any]:
    raw = _exact_mapping(value, _PROCESS_IDENTITY_FIELDS, "MCP process identity")
    return {
        "pid": _positive_integer(raw["pid"], "MCP PID"),
        "process_create_time": _positive_finite(
            raw["process_create_time"], "MCP process creation time"
        ),
        "command_signature": _hex64(
            raw["command_signature"], "MCP command signature"
        ),
    }


def _normalize_immutable_source(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    raw = _exact_mapping(value, _SOURCE_IDENTITY_FIELDS, "immutable source")
    return {
        "path": _normalize_confirmed_model_path(raw["path"]),
        "sha256": _hex64(raw["sha256"], "immutable source SHA-256"),
    }


def build_shared_model_lock(
    *,
    attached_server: AttachedServerIdentity,
    session_acquisition_id: str,
    model: SharedModelIdentity,
    revision: SharedModelRevision,
    collaboration_mode: str,
    lock_created_at_epoch: float,
    mcp_process: Mapping[str, Any],
    immutable_source: Mapping[str, Any] | None = None,
) -> SharedModelLock:
    """Bind one shared model guard to exact server, session, and process state."""
    if not isinstance(session_acquisition_id, str) or not _HEX32.fullmatch(
        session_acquisition_id
    ):
        raise ValueError("session acquisition ID must be exactly 32 hexadecimal characters")
    if revision.model_identity_sha256 != model.identity_sha256:
        raise ValueError("shared model revision belongs to a different model identity")
    if collaboration_mode not in _COLLABORATION_MODES:
        raise ValueError("shared model collaboration mode is unsupported")
    process = _normalize_process_identity(mcp_process)
    source = _normalize_immutable_source(immutable_source)
    body = {
        "schema_name": SHARED_MODEL_LOCK_SCHEMA,
        "schema_version": SHARED_MODEL_LOCK_VERSION,
        "lock_id": hashlib.sha256(
            f"{session_acquisition_id}:{model.identity_sha256}".encode("ascii")
        ).hexdigest()[:32],
        "attached_server": attached_server.to_dict(),
        "session_acquisition_id": session_acquisition_id.casefold(),
        "model": model.to_dict(),
        "revision": revision.to_dict(),
        "collaboration_mode": collaboration_mode,
        "immutable_source": source,
        "lock_created_at_epoch": _positive_finite(
            lock_created_at_epoch, "lock creation time"
        ),
        "mcp_process": process,
    }
    return SharedModelLock(**body, lock_sha256=_sha256(body))


__all__ = [
    "MAX_REVISION_COLLECTION_ITEMS",
    "MAX_REVISION_DEPTH",
    "MAX_REVISION_READBACK_BYTES",
    "SHARED_MODEL_LOCK_SCHEMA",
    "SHARED_MODEL_LOCK_VERSION",
    "SharedModelIdentity",
    "SharedModelLock",
    "SharedModelRevision",
    "build_shared_model_lock",
    "build_shared_model_revision",
    "normalize_shared_model_identity",
]
