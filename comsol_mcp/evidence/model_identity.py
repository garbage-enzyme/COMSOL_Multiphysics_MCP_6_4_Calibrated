"""Versioned offline model identity and checkpoint readiness.

``comsol_mcp.model_identity`` binds one inspected `.mph` archive to its
declared archive identity, optional caller-declared source and checkpoint
confirmations, passive runtime distribution identities, and — only when the
caller explicitly requests it — passively readable in-process session state.

The builder never starts COMSOL, Java, MPh, or JPype. Runtime versions come
from local package metadata without importing those distributions, and live
session identity is read exclusively from a caller-supplied passive provider.
Missing live identity is a structured ``unavailable`` result; conflicting or
unverifiable declared identity is a ``pause_and_repair`` disposition that
never grants permission to continue modeling.
"""

from __future__ import annotations

import hashlib
import re
import sys
from importlib import metadata as importlib_metadata
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Mapping

from comsol_mcp.contracts.mph_inspection import MphInspectionLimits
from comsol_mcp.durable import canonical_sha256_v1
from comsol_mcp.evidence.inspection.summary import build_mph_inspection_summary

MODEL_IDENTITY_SCHEMA_NAME = "comsol_mcp.model_identity"
MODEL_IDENTITY_SCHEMA_VERSION = "1.0.0"

_HASH_CHUNK_BYTES = 1_048_576
_MAX_WARNING_ROWS = 32
_MAX_REASON_ROWS = 16
_MAX_SESSION_TEXT = 512

SessionProvider = Callable[[], Mapping[str, Any] | None]

_MODEL_TAG = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")

_SESSION_PROVIDER_FIELDS = frozenset(
    {
        "available",
        "reason_codes",
        "active_model_tag",
        "bound_model_tag",
        "label",
        "revision",
        "comsol_version",
        "shared_session",
    }
)


class ModelIdentityError(Exception):
    """Typed refusal with a stable public reason code."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _redact(path: str | Path) -> str:
    return "**/" + PureWindowsPath(str(path)).name


def _bounded_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    return text[:_MAX_SESSION_TEXT]


def _bounded_rows(values: Any, label: str, maximum: int) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
        raise ModelIdentityError(
            "identity_session_provider_invalid",
            f"session provider {label} must be a list of strings",
        )
    rows = [item[:64] for item in values]
    if len(rows) > maximum:
        raise ModelIdentityError(
            "identity_session_provider_invalid",
            f"session provider {label} exceeds {maximum} rows",
        )
    return rows


def _optional_tag(value: Any, field: str) -> str | None:
    tag = _bounded_text(value)
    if tag is None:
        return None
    if _MODEL_TAG.fullmatch(tag) is None:
        raise ModelIdentityError(
            "identity_session_provider_invalid",
            f"session provider {field} must be one bounded clientapi-style tag",
        )
    return tag


def _distribution_version(distribution: str) -> str | None:
    """Read installed distribution metadata without importing the package."""
    try:
        version = importlib_metadata.version(distribution)
    except importlib_metadata.PackageNotFoundError:
        return None
    return version or None


def _bounded_file_sha256(
    path: Path,
    *,
    max_bytes: int,
    missing_code: str,
    over_limit_code: str,
) -> tuple[str, int]:
    try:
        stat_result = path.stat()
    except OSError as exc:
        raise ModelIdentityError(missing_code, f"{path.name} is not readable") from exc
    if not path.is_file():
        raise ModelIdentityError(missing_code, f"{path.name} is not a regular file")
    if stat_result.st_size > max_bytes:
        raise ModelIdentityError(over_limit_code, f"{path.name} exceeds the declared byte limit")
    digest_bytes = 0
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                block = handle.read(_HASH_CHUNK_BYTES)
                if not block:
                    break
                digest.update(block)
                digest_bytes += len(block)
    except OSError as exc:
        raise ModelIdentityError(missing_code, f"{path.name} could not be read") from exc
    return digest.hexdigest(), digest_bytes


def _normalize_live_session(provider_result: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate one passive provider snapshot without ever touching COMSOL."""
    if provider_result is None:
        return {
            "availability": "unavailable",
            "reason_codes": ["identity_session_provider_unavailable"],
            "active_model_tag": None,
            "bound_model_tag": None,
            "label": None,
            "revision": None,
            "comsol_version": None,
            "shared_session": False,
        }
    if (
        not isinstance(provider_result, Mapping)
        or not set(provider_result) <= _SESSION_PROVIDER_FIELDS
    ):
        raise ModelIdentityError(
            "identity_session_provider_invalid",
            "session provider returned an unknown or non-mapping shape",
        )
    available = provider_result.get("available")
    if not isinstance(available, bool):
        raise ModelIdentityError(
            "identity_session_provider_invalid",
            "session provider availability must be boolean",
        )
    shared_session = provider_result.get("shared_session", False)
    if not isinstance(shared_session, bool):
        raise ModelIdentityError(
            "identity_session_provider_invalid",
            "session provider shared_session must be boolean",
        )
    reason_codes = _bounded_rows(provider_result.get("reason_codes"), "reason_codes", 8)
    active_tag = _optional_tag(provider_result.get("active_model_tag"), "active_model_tag")
    bound_tag = _optional_tag(provider_result.get("bound_model_tag"), "bound_model_tag")
    if available:
        if active_tag is None:
            raise ModelIdentityError(
                "identity_session_provider_invalid",
                "an available session provider must declare an active model tag",
            )
        return {
            "availability": "available",
            "reason_codes": [],
            "active_model_tag": active_tag,
            "bound_model_tag": bound_tag,
            "label": _bounded_text(provider_result.get("label")),
            "revision": _bounded_text(provider_result.get("revision")),
            "comsol_version": _bounded_text(provider_result.get("comsol_version")),
            "shared_session": shared_session,
        }
    return {
        "availability": "unavailable",
        "reason_codes": reason_codes or ["no_active_session"],
        "active_model_tag": None,
        "bound_model_tag": None,
        "label": None,
        "revision": None,
        "comsol_version": None,
        "shared_session": False,
    }


def build_model_identity(
    file_path: str | Path,
    *,
    source_path: str | Path | None = None,
    checkpoint_path: str | Path | None = None,
    expected_file_sha256: str | None = None,
    expected_source_sha256: str | None = None,
    expected_checkpoint_sha256: str | None = None,
    limits: MphInspectionLimits | None = None,
    session_provider: SessionProvider | None = None,
) -> dict[str, Any]:
    """Build one bounded read-only model-identity contract offline.

    The primary archive must be a valid bounded `.mph` archive; refusals use
    the inspection reader's typed reason codes. Optional source/checkpoint
    confirmations and session identity never raise: their problems are
    recorded as failure reasons and drive the pause-and-repair disposition.
    """
    bounds = limits or MphInspectionLimits()
    path = Path(file_path)
    summary = build_mph_inspection_summary(path, bounds)

    warnings: list[str] = []
    failure_reasons: list[str] = []

    file_sha256 = summary["sha256"]
    if expected_file_sha256 is not None and expected_file_sha256.casefold() != file_sha256:
        failure_reasons.append("declared_file_hash_mismatch")

    derived_redacted: str | None = None
    source_redacted = _redact(path)
    source_sha256: str | None = file_sha256
    if source_path is not None:
        try:
            computed_source_hash, _source_bytes = _bounded_file_sha256(
                Path(source_path),
                max_bytes=bounds.max_archive_bytes,
                missing_code="source_unavailable",
                over_limit_code="source_over_declared_byte_limit",
            )
        except ModelIdentityError as exc:
            failure_reasons.append(exc.reason_code)
            source_sha256 = None
        else:
            source_redacted = _redact(source_path)
            source_sha256 = computed_source_hash
            if (
                expected_source_sha256 is not None
                and expected_source_sha256.casefold() != computed_source_hash
            ):
                failure_reasons.append("declared_source_hash_mismatch")
            if computed_source_hash != file_sha256:
                derived_redacted = _redact(path)
            else:
                warnings.append("source_and_derived_bytes_identical")
    else:
        warnings.append("source_provenance_undeclared")

    checkpoint_ready = False
    checkpoint_redacted: str | None = None
    checkpoint_sha256: str | None = None
    if checkpoint_path is not None:
        checkpoint_candidate = Path(checkpoint_path)
        checkpoint_redacted = _redact(checkpoint_candidate)
        try:
            computed_checkpoint_hash, checkpoint_bytes = _bounded_file_sha256(
                checkpoint_candidate,
                max_bytes=bounds.max_archive_bytes,
                missing_code="checkpoint_unavailable",
                over_limit_code="checkpoint_over_declared_byte_limit",
            )
        except ModelIdentityError as exc:
            failure_reasons.append(exc.reason_code)
        else:
            if checkpoint_bytes == 0:
                failure_reasons.append("checkpoint_empty")
            else:
                checkpoint_sha256 = computed_checkpoint_hash
                checkpoint_ready = True
                if (
                    expected_checkpoint_sha256 is not None
                    and expected_checkpoint_sha256.casefold() != computed_checkpoint_hash
                ):
                    failure_reasons.append("declared_checkpoint_hash_mismatch")
                    checkpoint_ready = False
    elif expected_checkpoint_sha256 is not None:
        # The caller declared a checkpoint expectation, so its absence is a
        # conflicting-identity result rather than an offline-only reading.
        failure_reasons.append("checkpoint_missing")
    else:
        warnings.append("checkpoint_not_declared")

    session_identity: dict[str, Any]
    if session_provider is None:
        session_identity = {
            "availability": "not_requested",
            "reason_codes": [],
            "active_model_tag": None,
            "bound_model_tag": None,
            "label": None,
            "revision": None,
            "comsol_version": None,
            "shared_session": False,
        }
    else:
        try:
            raw_session = session_provider()
        except ModelIdentityError:
            raise
        except Exception:
            session_identity = _normalize_live_session(None)
            session_identity["reason_codes"] = ["identity_session_provider_failed"]
        else:
            session_identity = _normalize_live_session(raw_session)
        if session_identity["availability"] == "unavailable":
            warnings.append("live_session_unavailable")

    mph_version = _distribution_version("MPh")
    if mph_version is None:
        warnings.append("mph_distribution_metadata_unavailable")
    jpype_version = _distribution_version("JPype1")
    if jpype_version is None:
        warnings.append("jpype_distribution_metadata_unavailable")

    revision = session_identity["revision"]
    comsol_version = summary["comsol_version"]
    declared_session_version = session_identity["comsol_version"]
    if (
        session_identity["availability"] == "available"
        and declared_session_version
        and declared_session_version != comsol_version
    ):
        failure_reasons.append("declared_session_version_mismatch")

    if len(failure_reasons) > _MAX_REASON_ROWS:
        del failure_reasons[_MAX_REASON_ROWS:]
        warnings.append("failure_reason_list_truncated")
    warnings_unique = sorted(set(warnings))
    if len(warnings_unique) > _MAX_WARNING_ROWS:
        del warnings_unique[_MAX_WARNING_ROWS:]

    identity: dict[str, Any] = {
        "schema_name": MODEL_IDENTITY_SCHEMA_NAME,
        "schema_version": MODEL_IDENTITY_SCHEMA_VERSION,
        "active_model_tag": session_identity["active_model_tag"],
        "bound_model_tag": session_identity["bound_model_tag"],
        "title": summary["title"],
        "label": summary["title"],
        "source_path_redacted": source_redacted,
        "derived_path_redacted": derived_redacted,
        "model_path": _redact(path),
        "comsol_version": comsol_version,
        "mph_version": mph_version,
        "jpype_version": jpype_version,
        "python_version": "{0}.{1}.{2}".format(*sys.version_info[:3]),
        "source_sha256": source_sha256,
        "file_sha256": file_sha256,
        "byte_size": summary["byte_size"],
        "node_type": summary["node_type"],
        "runnable_state": summary["runnable_state"],
        "solved_state": summary["solved_state"],
        "preview_state": summary["preview_state"],
        "revision": revision,
        "read_only": True,
        "checkpoint_ready": checkpoint_ready,
        "checkpoint_path_redacted": checkpoint_redacted,
        "checkpoint_sha256": checkpoint_sha256,
        "shared_session": bool(session_identity["shared_session"]),
        "session_identity": session_identity,
        "identity_disposition": "pause_and_repair" if failure_reasons else "ready",
        "failure_reasons": failure_reasons,
        "warnings": warnings_unique,
        "inspection_fingerprint": summary["inspection_fingerprint"],
        "identity_fingerprint": "",
    }

    body = {key: value for key, value in identity.items() if key != "identity_fingerprint"}
    identity["identity_fingerprint"] = canonical_sha256_v1(body)
    return identity


__all__ = [
    "MODEL_IDENTITY_SCHEMA_NAME",
    "MODEL_IDENTITY_SCHEMA_VERSION",
    "ModelIdentityError",
    "build_model_identity",
]
