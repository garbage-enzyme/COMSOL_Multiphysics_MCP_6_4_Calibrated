"""Settings-aware solver-free verification of formal evidence portfolios."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Mapping

from comsol_mcp.evidence.contracts import canonical_sha256
from comsol_mcp.evidence.integrity_controls import (
    EVIDENCE_CHECKS,
    EVIDENCE_INTEGRITY_VERSION,
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

    status = deepcopy(
        dict(settings_status)
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
                "error": str(exc)[:1024],
            }
            failures.append({"check": check_name, "error": str(exc)[:1024]})

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
                "error": str(exc)[:1024],
            }
            failures.append({"check": producer_check, "error": str(exc)[:1024]})

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
