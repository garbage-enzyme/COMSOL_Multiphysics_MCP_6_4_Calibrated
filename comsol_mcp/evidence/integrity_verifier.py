"""Settings-aware solver-free verification of formal evidence portfolios."""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from comsol_mcp.evidence.contracts import canonical_sha256
from comsol_mcp.evidence.integrity_controls import (
    EVIDENCE_CHECKS,
    EVIDENCE_INTEGRITY_VERSION,
    EVIDENCE_STATUS_SCHEMA,
    EVIDENCE_VERIFICATION_SCHEMA,
    load_evidence_integrity_status,
    warning_fields,
)
from comsol_mcp.evidence.portfolio_verifier import verify_portfolio_evidence_checks

_HASH = re.compile(r"^[0-9a-f]{64}$")
_COMPATIBILITY_FIELDS = {
    "producer",
    "producer_version",
    "driver_sha256",
    "schema_version",
}


def _validated_settings_status(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("settings_status must be an object")
    status = deepcopy(dict(value))
    if status.get("schema_name") != EVIDENCE_STATUS_SCHEMA:
        raise ValueError("settings_status schema_name is invalid")
    if status.get("schema_version") != EVIDENCE_INTEGRITY_VERSION:
        raise ValueError("settings_status schema_version is invalid")
    state = status.get("configuration_state")
    if state not in {"valid", "degraded", "invalid"}:
        raise ValueError("settings_status configuration_state is invalid")
    strict = status.get("strict_verification_active")
    if not isinstance(strict, bool):
        raise ValueError("settings_status strict_verification_active must be boolean")
    checks = status.get("checks")
    if not isinstance(checks, Mapping) or set(checks) != set(EVIDENCE_CHECKS):
        raise ValueError("settings_status checks are incomplete")
    enabled: dict[str, bool] = {}
    for name in EVIDENCE_CHECKS:
        check = checks[name]
        if not isinstance(check, Mapping) or set(check) != {"enabled", "source"}:
            raise ValueError(f"settings_status checks.{name} is invalid")
        if not isinstance(check["enabled"], bool):
            raise ValueError(f"settings_status checks.{name}.enabled must be boolean")
        if (
            not isinstance(check["source"], str)
            or not check["source"]
            or len(check["source"]) > 64
        ):
            raise ValueError(f"settings_status checks.{name}.source is invalid")
        enabled[name] = check["enabled"]
    disabled = [name for name in EVIDENCE_CHECKS if not enabled[name]]
    supplied_disabled = status.get("disabled_checks")
    if (
        not isinstance(supplied_disabled, list)
        or supplied_disabled != disabled
        or not all(isinstance(name, str) for name in supplied_disabled)
    ):
        raise ValueError("settings_status disabled_checks is inconsistent")
    for field in ("warning_codes", "warning_messages"):
        items = status.get(field)
        if not isinstance(items, list) or not all(
            isinstance(item, str) and len(item) <= 2048 for item in items
        ):
            raise ValueError(f"settings_status {field} is invalid")
    if state == "valid":
        if status.get("success") is not True:
            raise ValueError("settings_status valid state must be successful")
        fingerprint = status.get("settings_fingerprint_sha256")
        if not isinstance(fingerprint, str) or not _HASH.fullmatch(fingerprint):
            raise ValueError("settings_status fingerprint is invalid")
        if strict is not all(enabled.values()):
            raise ValueError("settings_status strict verification state is inconsistent")
    elif state == "invalid" and strict:
        raise ValueError("settings_status invalid configuration cannot be strictly verified")
    return status


def _bounded_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(f"{label} must be a bounded nonempty string")
    return value


def _compatibility_identity(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != _COMPATIBILITY_FIELDS:
        raise ValueError(f"{label} fields are invalid")
    identity = {
        name: _bounded_text(value[name], f"{label}.{name}")
        for name in sorted(_COMPATIBILITY_FIELDS)
    }
    if not _HASH.fullmatch(identity["driver_sha256"]):
        raise ValueError(f"{label}.driver_sha256 must be a lowercase SHA-256 digest")
    return identity


def verify_producer_driver_compatibility(value: Any) -> dict[str, Any]:
    """Require exact producer, driver, and schema identity across resume."""
    if not isinstance(value, dict) or set(value) != {"expected", "observed"}:
        raise ValueError("producer_compatibility fields are invalid")
    expected = _compatibility_identity(value["expected"], "producer_compatibility.expected")
    observed = _compatibility_identity(value["observed"], "producer_compatibility.observed")
    mismatches = [name for name in sorted(_COMPATIBILITY_FIELDS) if expected[name] != observed[name]]
    if mismatches:
        raise ValueError(
            f"producer/driver compatibility mismatch: {mismatches}"
        )
    return {
        "state": "passed",
        "matched_fields": sorted(_COMPATIBILITY_FIELDS),
        "driver_sha256": expected["driver_sha256"],
    }


def _finalize(body: dict[str, Any]) -> dict[str, Any]:
    return {**body, "verification_sha256": canonical_sha256(body)}


def verify_evidence_integrity(
    *,
    portfolio_request: Mapping[str, Any],
    artifact_roots: Mapping[str, str],
    resumed: bool = False,
    producer_compatibility: Mapping[str, Any] | None = None,
    settings_status: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run every enabled deterministic check and disclose every skipped check."""
    if not isinstance(resumed, bool):
        raise ValueError("resumed must be a boolean")
    if not isinstance(portfolio_request, Mapping):
        raise ValueError("portfolio_request must be a JSON object")
    if not isinstance(artifact_roots, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in artifact_roots.items()
    ):
        raise ValueError("artifact_roots must map case IDs to absolute directory strings")
    if any(not value or not Path(value).is_absolute() for value in artifact_roots.values()):
        raise ValueError("artifact_roots must map case IDs to absolute directory strings")

    status = _validated_settings_status(
        settings_status
        if settings_status is not None
        else load_evidence_integrity_status()
    )
    base = {
        "schema_name": EVIDENCE_VERIFICATION_SCHEMA,
        "schema_version": EVIDENCE_INTEGRITY_VERSION,
        "settings_fingerprint_sha256": status.get("settings_fingerprint_sha256"),
        "settings_path_included": False,
        "resumed": resumed,
        "paths_included": False,
        "source_mutation_performed": False,
    }
    if status.get("configuration_state") != "valid":
        return _finalize(
            {
                **base,
                "success": False,
                "verification_state": "blocked",
                "strictly_verified": False,
                "reason_code": "evidence_integrity_settings_invalid",
                "check_results": {},
                **warning_fields(status),
            }
        )

    checks = status["checks"]
    check_results: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, str]] = []
    portfolio_check_args = {
        "outcome_contract_validation": "check_outcome_contract",
        "artifact_chain_verification": "check_artifact_chain",
        "summary_claim_verification": "check_summary_claims",
    }
    filesystem_checks_enabled = any(
        checks[name]["enabled"]
        for name in ("artifact_chain_verification", "summary_claim_verification")
    )
    selected_roots = dict(artifact_roots) if filesystem_checks_enabled else {}
    for check_name, argument_name in portfolio_check_args.items():
        if not checks[check_name]["enabled"]:
            check_results[check_name] = {
                "state": "skipped",
                "reason_code": "disabled_by_settings",
            }
            continue
        arguments = {
            "check_outcome_contract": False,
            "check_artifact_chain": False,
            "check_summary_claims": False,
        }
        arguments[argument_name] = True
        try:
            receipt = verify_portfolio_evidence_checks(
                portfolio_request,
                artifact_roots=selected_roots if argument_name != "check_outcome_contract" else {},
                **arguments,
            )
            check_results[check_name] = {
                "state": "passed",
                "verification_sha256": receipt["verification_sha256"],
                "case_count": receipt["case_count"],
            }
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            check_results[check_name] = {
                "state": "failed",
                "reason_code": "deterministic_check_failed",
                "error_type": type(exc).__name__,
                "error": "Evidence check failed.",
            }
            failures.append({"check": check_name, "error": "Evidence check failed."})

    producer_check = "producer_driver_compatibility"
    if not checks[producer_check]["enabled"]:
        check_results[producer_check] = {
            "state": "skipped",
            "reason_code": "disabled_by_settings",
        }
    elif not resumed:
        check_results[producer_check] = {
            "state": "not_applicable",
            "reason_code": "fresh_verification_not_resume",
        }
    elif producer_compatibility is None:
        check_results[producer_check] = {
            "state": "failed",
            "reason_code": "resume_compatibility_missing",
        }
        failures.append(
            {"check": producer_check, "error": "resume compatibility evidence is missing"}
        )
    else:
        try:
            check_results[producer_check] = verify_producer_driver_compatibility(
                producer_compatibility
            )
        except (TypeError, ValueError) as exc:
            check_results[producer_check] = {
                "state": "failed",
                "reason_code": "resume_compatibility_failed",
                "error_type": type(exc).__name__,
                "error": "Producer compatibility verification failed.",
            }
            failures.append(
                {
                    "check": producer_check,
                    "error": "Producer compatibility verification failed.",
                }
            )

    fully_active = status["strict_verification_active"] is True
    strictly_verified = fully_active and not failures and all(
        check_results[name]["state"] in {"passed", "not_applicable"}
        for name in EVIDENCE_CHECKS
    )
    verification_state = (
        "verified"
        if strictly_verified
        else "failed"
        if failures
        else "unverified"
    )
    result = {
        **base,
        "success": not failures,
        "verification_state": verification_state,
        "strictly_verified": strictly_verified,
        "reason_code": (
            "all_enabled_checks_passed"
            if strictly_verified
            else "deterministic_check_failed"
            if failures
            else "checks_disabled_by_settings"
        ),
        "request_sha256": portfolio_request.get("request_sha256"),
        "check_results": check_results,
        "failures": failures,
    }
    if not strictly_verified:
        result.update(warning_fields(status))
    return _finalize(result)


__all__ = [
    "EVIDENCE_VERIFICATION_SCHEMA",
    "verify_evidence_integrity",
    "verify_producer_driver_compatibility",
]
