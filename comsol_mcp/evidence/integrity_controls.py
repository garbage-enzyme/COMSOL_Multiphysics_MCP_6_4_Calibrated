"""Default-on evidence-integrity settings and warning propagation contracts."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from comsol_mcp.settings import load_settings, settings_fingerprint, settings_status


EVIDENCE_SETTINGS_ENV = "COMSOL_MCP_EVIDENCE_SETTINGS_PATH"
EVIDENCE_SETTINGS_SCHEMA = "comsol_mcp.evidence_integrity_settings"
EVIDENCE_STATUS_SCHEMA = "comsol_mcp.evidence_integrity_status"
EVIDENCE_VERIFICATION_SCHEMA = "comsol_mcp.evidence_integrity_verification"
EVIDENCE_INTEGRITY_VERSION = "1.0.0"
MAX_EVIDENCE_SETTINGS_BYTES = 64 * 1024

EVIDENCE_CHECKS = (
    "outcome_contract_validation",
    "artifact_chain_verification",
    "summary_claim_verification",
    "producer_driver_compatibility",
)

DISABLED_CHECK_WARNING_CODE = "strict_evidence_checks_disabled"
INVALID_SETTINGS_WARNING_CODE = "evidence_integrity_settings_invalid"
DISABLED_CHECK_WARNING = (
    "Strict evidence checks are disabled; these results were not fully verified "
    "and may contain AI-generated or hallucinated content."
)
INVALID_SETTINGS_WARNING = (
    "Evidence-integrity settings are invalid; formal verification is blocked "
    "until the settings are corrected."
)


class _DuplicateJsonKey(ValueError):
    pass


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("evidence-integrity settings must contain finite JSON") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _default_status() -> dict[str, Any]:
    checks = {
        name: {"enabled": True, "source": "default"}
        for name in EVIDENCE_CHECKS
    }
    effective = {
        "schema_name": EVIDENCE_SETTINGS_SCHEMA,
        "schema_version": EVIDENCE_INTEGRITY_VERSION,
        "checks": {name: True for name in EVIDENCE_CHECKS},
    }
    return {
        "success": True,
        "schema_name": EVIDENCE_STATUS_SCHEMA,
        "schema_version": EVIDENCE_INTEGRITY_VERSION,
        "configuration_state": "valid",
        "settings_source": "default",
        "settings_environment_variable": EVIDENCE_SETTINGS_ENV,
        "settings_fingerprint_sha256": _sha256(_canonical_bytes(effective)),
        "settings_path_included": False,
        "default_enabled": True,
        "strict_verification_active": True,
        "checks": checks,
        "disabled_checks": [],
        "warning_codes": [],
        "warning_messages": [],
    }


def _invalid_status(reason_code: str, error: Exception, *, raw_sha256: str | None) -> dict[str, Any]:
    status = _default_status()
    return {
        **status,
        "success": False,
        "configuration_state": "invalid",
        "settings_source": "explicit_settings",
        "settings_fingerprint_sha256": raw_sha256,
        "strict_verification_active": False,
        "reason_code": reason_code,
        "error_type": type(error).__name__,
        "error": str(error)[:1024],
        "warning_codes": [INVALID_SETTINGS_WARNING_CODE],
        "warning_messages": [INVALID_SETTINGS_WARNING],
    }


def _valid_status(
    effective: Mapping[str, Any],
    reported_checks: Mapping[str, Any],
    *,
    source: str,
    fingerprint: str,
) -> dict[str, Any]:
    disabled = [name for name in EVIDENCE_CHECKS if not effective["checks"][name]]
    warning_codes = [DISABLED_CHECK_WARNING_CODE] if disabled else []
    warning_messages = [DISABLED_CHECK_WARNING] if disabled else []
    return {
        "success": True,
        "schema_name": EVIDENCE_STATUS_SCHEMA,
        "schema_version": EVIDENCE_INTEGRITY_VERSION,
        "configuration_state": "valid",
        "settings_source": source,
        "settings_environment_variable": EVIDENCE_SETTINGS_ENV,
        "settings_fingerprint_sha256": fingerprint,
        "settings_path_included": False,
        "default_enabled": True,
        "strict_verification_active": not disabled,
        "checks": dict(reported_checks),
        "disabled_checks": disabled,
        "warning_codes": warning_codes,
        "warning_messages": warning_messages,
    }


def _settings_path(value: Any) -> Path:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError(f"{EVIDENCE_SETTINGS_ENV} must be a bounded absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{EVIDENCE_SETTINGS_ENV} must be an absolute path")
    resolved = path.resolve(strict=True)
    is_junction = getattr(path, "is_junction", lambda: False)
    if path.is_symlink() or is_junction() or not resolved.is_file():
        raise ValueError("evidence-integrity settings must be a regular non-link file")
    return resolved


def _normalize_settings(value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(value, dict):
        raise ValueError("evidence-integrity settings must be a JSON object")
    expected = {"schema_name", "schema_version", "checks"}
    unknown = sorted(set(value) - expected)
    if unknown:
        raise ValueError(f"evidence-integrity settings contain unknown fields: {unknown}")
    if set(value) != expected:
        raise ValueError("evidence-integrity settings fields are incomplete")
    if value["schema_name"] != EVIDENCE_SETTINGS_SCHEMA:
        raise ValueError("evidence-integrity settings schema_name is unsupported")
    if value["schema_version"] != EVIDENCE_INTEGRITY_VERSION:
        raise ValueError("evidence-integrity settings schema_version is unsupported")
    supplied_checks = value["checks"]
    if not isinstance(supplied_checks, dict):
        raise ValueError("evidence-integrity settings checks must be a JSON object")
    unknown_checks = sorted(set(supplied_checks) - set(EVIDENCE_CHECKS))
    if unknown_checks:
        raise ValueError(f"evidence-integrity settings contain unknown checks: {unknown_checks}")

    effective_checks: dict[str, bool] = {}
    reported_checks: dict[str, dict[str, Any]] = {}
    for name in EVIDENCE_CHECKS:
        if name in supplied_checks:
            enabled = supplied_checks[name]
            if not isinstance(enabled, bool):
                raise ValueError(f"evidence-integrity check {name} must be a JSON boolean")
            source = "explicit_settings"
        else:
            enabled = True
            source = "default"
        effective_checks[name] = enabled
        reported_checks[name] = {"enabled": enabled, "source": source}
    effective = {
        "schema_name": EVIDENCE_SETTINGS_SCHEMA,
        "schema_version": EVIDENCE_INTEGRITY_VERSION,
        "checks": effective_checks,
    }
    return effective, reported_checks


def load_evidence_integrity_status(
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Load project settings, with the old external file as a compatibility override."""
    if environ is None and EVIDENCE_SETTINGS_ENV in os.environ:
        environ = os.environ
    if environ is None:
        project_status = settings_status()
        project = load_settings()
        effective = {
            "schema_name": EVIDENCE_SETTINGS_SCHEMA,
            "schema_version": EVIDENCE_INTEGRITY_VERSION,
            "checks": dict(project["evidence_integrity"]["checks"]),
        }
        reported = {
            name: {
                "enabled": effective["checks"][name],
                "source": "project_settings",
            }
            for name in EVIDENCE_CHECKS
        }
        result = _valid_status(
            effective,
            reported,
            source="project_settings",
            fingerprint=settings_fingerprint(project),
        )
        if project_status.get("settings_errors"):
            result["configuration_state"] = "degraded"
            result["reason_code"] = project_status.get("reason_code")
            result["settings_errors"] = project_status["settings_errors"]
        return result

    environment = environ
    configured_path = environment.get(EVIDENCE_SETTINGS_ENV)
    if configured_path is None:
        return _default_status()

    raw: bytes | None = None
    try:
        path = _settings_path(configured_path)
        raw = path.read_bytes()
        if not raw or len(raw) > MAX_EVIDENCE_SETTINGS_BYTES:
            raise ValueError(
                f"evidence-integrity settings must contain 1..{MAX_EVIDENCE_SETTINGS_BYTES} bytes"
            )
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        effective, checks = _normalize_settings(document)
    except FileNotFoundError as exc:
        return _invalid_status("settings_file_missing", exc, raw_sha256=None)
    except UnicodeDecodeError as exc:
        return _invalid_status(
            "settings_not_utf8", exc, raw_sha256=_sha256(raw) if raw is not None else None
        )
    except (json.JSONDecodeError, _DuplicateJsonKey) as exc:
        return _invalid_status(
            "settings_json_invalid", exc, raw_sha256=_sha256(raw) if raw is not None else None
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _invalid_status(
            "settings_rejected", exc, raw_sha256=_sha256(raw) if raw is not None else None
        )

    return _valid_status(
        effective,
        checks,
        source="explicit_settings",
        fingerprint=_sha256(_canonical_bytes(effective)),
    )


def warning_fields(status: Mapping[str, Any]) -> dict[str, Any]:
    """Return stable unverified fields for an affected tool response."""
    if status.get("strict_verification_active") is True:
        return {}
    return {
        "strictly_verified": False,
        "disabled_evidence_checks": list(status.get("disabled_checks", [])),
        "evidence_integrity_warning_codes": list(status.get("warning_codes", [])),
        "evidence_integrity_warnings": list(status.get("warning_messages", [])),
        "evidence_settings_fingerprint_sha256": status.get(
            "settings_fingerprint_sha256"
        ),
    }


def annotate_tool_response(tool_name: str, result: Any) -> Any:
    """Propagate opt-out or invalid-settings warnings without upgrading results."""
    if not isinstance(result, dict) or tool_name in {
        "capabilities",
        "evidence_integrity_status",
    }:
        return result
    status = load_evidence_integrity_status()
    fields = warning_fields(status)
    if not fields:
        return result
    annotated = dict(result)
    annotated.update(fields)
    return annotated


def evidence_integrity_capability(
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return path-redacted default/opt-out discovery for capabilities."""
    status = load_evidence_integrity_status(environ)
    return {
        **deepcopy(status),
        "solver_free": True,
        "tools": ["evidence_integrity_status", "evidence_integrity_verify"],
        "formal_verification_scope": (
            "outcome contract, artifact chain, exact summary claims, and resume compatibility"
        ),
        "hashes_prove_physical_correctness": False,
    }


__all__ = [
    "DISABLED_CHECK_WARNING",
    "DISABLED_CHECK_WARNING_CODE",
    "EVIDENCE_CHECKS",
    "EVIDENCE_INTEGRITY_VERSION",
    "EVIDENCE_SETTINGS_ENV",
    "EVIDENCE_SETTINGS_SCHEMA",
    "EVIDENCE_STATUS_SCHEMA",
    "EVIDENCE_VERIFICATION_SCHEMA",
    "INVALID_SETTINGS_WARNING_CODE",
    "annotate_tool_response",
    "evidence_integrity_capability",
    "load_evidence_integrity_status",
    "warning_fields",
]
