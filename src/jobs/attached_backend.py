"""Pure immutable execution identity for durable attached-server jobs."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from src.shared_session.identity import normalize_attached_server_identity
from src.shared_session.locking import normalize_shared_model_identity


ATTACHED_EXECUTION_BACKEND_KIND = "attached_shared_server"

_BACKEND_FIELDS = frozenset(
    {
        "kind",
        "user_confirmed_automation_exclusive",
        "source_model_lock_sha256",
        "attached_server",
        "model",
        "expected_revision",
    }
)
_SERIALIZED_SERVER_FIELDS = frozenset(
    {
        "endpoint",
        "server_pid",
        "server_process_create_time",
        "server_command_signature",
        "listener_bind_scope",
        "listener_observed_at_epoch",
        "identity_sha256",
        "ownership",
    }
)
_SERIALIZED_ENDPOINT_FIELDS = frozenset({"host", "port", "scope"})
_SERIALIZED_MODEL_FIELDS = frozenset(
    {"tag", "label", "file_path", "unsaved", "identity_sha256"}
)
_SERIALIZED_REVISION_FIELDS = frozenset(
    {
        "sequence",
        "model_identity_sha256",
        "structural_sha256",
        "readback_sha256",
        "revision_sha256",
    }
)
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _exact_mapping(
    value: Any,
    fields: frozenset[str],
    label: str,
    *,
    optional: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise ValueError(f"{label} must be an object with string keys")
    actual = set(value)
    missing = sorted(fields - actual)
    unknown = sorted(actual - fields - optional)
    if missing or unknown:
        raise ValueError(
            f"{label} fields are invalid; missing={missing}, unknown={unknown}"
        )
    return dict(value)


def _hex64(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ValueError(f"{label} must be exactly 64 hexadecimal characters")
    return value.casefold()


def _normalize_serialized_server(value: Any) -> dict[str, Any]:
    raw = _exact_mapping(
        value, _SERIALIZED_SERVER_FIELDS, "attached execution server identity"
    )
    endpoint = _exact_mapping(
        raw["endpoint"],
        _SERIALIZED_ENDPOINT_FIELDS,
        "attached execution server endpoint",
    )
    if endpoint["scope"] != "loopback":
        raise ValueError("attached execution server endpoint scope must be loopback")
    if raw["ownership"] != "external_user_owned":
        raise ValueError("attached execution server must remain external user owned")
    normalized = normalize_attached_server_identity(
        {
            "endpoint": {"host": endpoint["host"], "port": endpoint["port"]},
            "server_pid": raw["server_pid"],
            "server_process_create_time": raw["server_process_create_time"],
            "server_command_signature": raw["server_command_signature"],
            "listener_bind_scope": raw["listener_bind_scope"],
            "listener_observed_at_epoch": raw["listener_observed_at_epoch"],
        }
    )
    if _hex64(raw["identity_sha256"], "attached server identity SHA-256") != (
        normalized.identity_sha256
    ):
        raise ValueError("attached server identity SHA-256 does not match its fields")
    return normalized.to_dict()


def _normalize_serialized_model(value: Any) -> dict[str, Any]:
    raw = _exact_mapping(
        value, _SERIALIZED_MODEL_FIELDS, "attached execution model identity"
    )
    normalized = normalize_shared_model_identity(
        {
            "tag": raw["tag"],
            "label": raw["label"],
            "file_path": raw["file_path"],
            "unsaved": raw["unsaved"],
        }
    )
    if _hex64(raw["identity_sha256"], "shared model identity SHA-256") != (
        normalized.identity_sha256
    ):
        raise ValueError("shared model identity SHA-256 does not match its fields")
    return normalized.to_dict()


def _normalize_serialized_revision(
    value: Any, *, model_identity_sha256: str
) -> dict[str, Any]:
    raw = _exact_mapping(
        value, _SERIALIZED_REVISION_FIELDS, "attached execution model revision"
    )
    sequence = raw["sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise ValueError("attached execution revision sequence must be nonnegative")
    body = {
        "sequence": sequence,
        "model_identity_sha256": _hex64(
            raw["model_identity_sha256"], "revision model identity SHA-256"
        ),
        "structural_sha256": _hex64(
            raw["structural_sha256"], "revision structural SHA-256"
        ),
        "readback_sha256": _hex64(
            raw["readback_sha256"], "revision readback SHA-256"
        ),
    }
    if body["model_identity_sha256"] != model_identity_sha256:
        raise ValueError("attached execution revision belongs to a different model")
    expected_sha256 = _canonical_sha256(body)
    if _hex64(raw["revision_sha256"], "model revision SHA-256") != expected_sha256:
        raise ValueError("model revision SHA-256 does not match its fields")
    return {**body, "revision_sha256": expected_sha256}


def normalize_attached_execution_backend(value: Any) -> dict[str, Any]:
    """Verify one exact automation-exclusive target without importing MPh."""
    raw = _exact_mapping(
        value,
        _BACKEND_FIELDS,
        "attached execution backend",
        optional=frozenset({"backend_identity_sha256"}),
    )
    if raw["kind"] != ATTACHED_EXECUTION_BACKEND_KIND:
        raise ValueError("attached execution backend kind is unsupported")
    if raw["user_confirmed_automation_exclusive"] is not True:
        raise ValueError(
            "attached execution requires explicit automation-exclusive confirmation"
        )
    server = _normalize_serialized_server(raw["attached_server"])
    model = _normalize_serialized_model(raw["model"])
    revision = _normalize_serialized_revision(
        raw["expected_revision"],
        model_identity_sha256=model["identity_sha256"],
    )
    body = {
        "kind": ATTACHED_EXECUTION_BACKEND_KIND,
        "user_confirmed_automation_exclusive": True,
        "source_model_lock_sha256": _hex64(
            raw["source_model_lock_sha256"], "source model lock SHA-256"
        ),
        "attached_server": server,
        "model": model,
        "expected_revision": revision,
    }
    identity_sha256 = _canonical_sha256(body)
    supplied_identity = raw.get("backend_identity_sha256")
    if supplied_identity is not None and _hex64(
        supplied_identity, "attached execution backend identity SHA-256"
    ) != identity_sha256:
        raise ValueError("attached execution backend identity SHA-256 is inconsistent")
    return {**body, "backend_identity_sha256": identity_sha256}


__all__ = [
    "ATTACHED_EXECUTION_BACKEND_KIND",
    "normalize_attached_execution_backend",
]
