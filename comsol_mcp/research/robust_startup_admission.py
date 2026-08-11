"""Startup-only absolute RAM and runtime-volume admission contracts."""

from __future__ import annotations

from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .derivative_support import _bounded_json, _object, _sha256

ROBUST_STARTUP_POLICY_SCHEMA_NAME = "comsol_mcp.robust_startup_admission_policy"
ROBUST_STARTUP_POLICY_SCHEMA_VERSION = "1.0.0"
ROBUST_STARTUP_RECEIPT_SCHEMA_NAME = "comsol_mcp.robust_startup_admission_receipt"
ROBUST_STARTUP_RECEIPT_SCHEMA_VERSION = "1.0.0"


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def normalize_robust_startup_policy(value: object) -> dict[str, Any]:
    """Normalize caller-configurable absolute thresholds checked once at startup."""
    bounded = _bounded_json(value, "robust startup admission policy", 64 * 1024)
    supplied = None
    if isinstance(bounded, dict) and "policy_fingerprint" in bounded:
        supplied = bounded.pop("policy_fingerprint")
    raw = _object(
        bounded,
        {
            "schema_name",
            "schema_version",
            "minimum_available_memory_bytes",
            "minimum_runtime_free_bytes",
            "check_frequency",
        },
        "robust startup admission policy",
    )
    if (
        raw["schema_name"] != ROBUST_STARTUP_POLICY_SCHEMA_NAME
        or raw["schema_version"] != ROBUST_STARTUP_POLICY_SCHEMA_VERSION
    ):
        raise ValueError("robust startup admission policy schema identity is unsupported")
    if raw["check_frequency"] != "startup_only":
        raise ValueError("robust RAM/disk admission must use startup_only frequency")
    body = {
        "schema_name": ROBUST_STARTUP_POLICY_SCHEMA_NAME,
        "schema_version": ROBUST_STARTUP_POLICY_SCHEMA_VERSION,
        "minimum_available_memory_bytes": _positive_integer(
            raw["minimum_available_memory_bytes"], "minimum_available_memory_bytes"
        ),
        "minimum_runtime_free_bytes": _positive_integer(
            raw["minimum_runtime_free_bytes"], "minimum_runtime_free_bytes"
        ),
        "check_frequency": "startup_only",
    }
    body["policy_fingerprint"] = domain_sha256_v2(ROBUST_STARTUP_POLICY_SCHEMA_NAME, body)
    if supplied is not None and supplied != body["policy_fingerprint"]:
        raise ValueError("robust startup admission policy fingerprint is invalid")
    return body


def evaluate_robust_startup_admission(policy: object, telemetry: object) -> dict[str, Any]:
    """Evaluate one pre-mesh sample without creating portable host defaults."""
    normalized_policy = normalize_robust_startup_policy(policy)
    sample = _bounded_json(telemetry, "robust startup telemetry", 128 * 1024)
    if not isinstance(sample, dict):
        raise ValueError("robust startup telemetry must be an object")
    if sample.get("schema_name") == "comsol_mcp.resource_telemetry_sample":
        values = sample.get("values")
        if not isinstance(values, dict):
            raise ValueError("normalized robust startup telemetry values are invalid")
        telemetry_sha256 = _sha256(sample.get("sample_sha256"), "telemetry sample_sha256")
    else:
        values = sample
        telemetry_sha256 = domain_sha256_v2("comsol_mcp.robust_startup_telemetry", sample)
    if values.get("stage") != "pre_mesh":
        raise ValueError("robust startup admission requires a pre_mesh telemetry sample")
    memory = values.get("available_memory_bytes")
    runtime_free = values.get("runtime_free_bytes")
    checks = {
        "available_memory_present": isinstance(memory, int),
        "runtime_free_space_present": isinstance(runtime_free, int),
        "available_memory_meets_minimum": (
            isinstance(memory, int)
            and memory >= normalized_policy["minimum_available_memory_bytes"]
        ),
        "runtime_free_space_meets_minimum": (
            isinstance(runtime_free, int)
            and runtime_free >= normalized_policy["minimum_runtime_free_bytes"]
        ),
    }
    allowed = all(checks.values())
    body = {
        "schema_name": ROBUST_STARTUP_RECEIPT_SCHEMA_NAME,
        "schema_version": ROBUST_STARTUP_RECEIPT_SCHEMA_VERSION,
        "policy_fingerprint": normalized_policy["policy_fingerprint"],
        "telemetry_sha256": telemetry_sha256,
        "check_frequency": "startup_only",
        "checks": checks,
        "observed": {
            "available_memory_bytes": memory,
            "runtime_free_bytes": runtime_free,
        },
        "decision": "allow" if allowed else "refuse",
        "ready": allowed,
        "recheck_required_before_condition_batches": False,
        "recheck_required_before_remesh_or_solve": False,
    }
    return {
        **body,
        "receipt_fingerprint": domain_sha256_v2(ROBUST_STARTUP_RECEIPT_SCHEMA_NAME, body),
    }


__all__ = [
    "ROBUST_STARTUP_POLICY_SCHEMA_NAME",
    "ROBUST_STARTUP_POLICY_SCHEMA_VERSION",
    "ROBUST_STARTUP_RECEIPT_SCHEMA_NAME",
    "ROBUST_STARTUP_RECEIPT_SCHEMA_VERSION",
    "evaluate_robust_startup_admission",
    "normalize_robust_startup_policy",
]
