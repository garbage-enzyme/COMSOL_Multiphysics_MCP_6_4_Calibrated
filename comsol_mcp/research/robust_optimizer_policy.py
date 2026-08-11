"""Manual robust native optimizer selection and evidence policy."""

from __future__ import annotations

import math
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .derivative_support import _bounded_json, _finite, _identifier, _object, _sha256
from .gradient_contracts import normalize_native_optimizer_configuration

ROBUST_OPTIMIZER_POLICY_SCHEMA_NAME = "comsol_mcp.robust_optimizer_policy"
ROBUST_OPTIMIZER_POLICY_SCHEMA_VERSION = "1.0.0"
ROBUST_OPTIMIZER_EXECUTION_RECEIPT_SCHEMA_NAME = "comsol_mcp.robust_optimizer_execution_receipt"
ROBUST_OPTIMIZER_EXECUTION_RECEIPT_SCHEMA_VERSION = "1.0.0"

_METHODS = {"gcmma", "mma"}
_SUPPORT_STATES = {"validated", "structural_only", "restricted", "rejected"}


def _optional_sha256(value: object, name: str) -> str | None:
    return None if value is None else _sha256(value, name)


def normalize_robust_optimizer_policy(value: object) -> dict[str, Any]:
    """Normalize a method-bound policy with no automatic fallback."""
    bounded = _bounded_json(value, "robust optimizer policy", 128 * 1024)
    supplied = None
    if isinstance(bounded, dict) and "policy_fingerprint" in bounded:
        supplied = bounded.pop("policy_fingerprint")
    derived = None
    if isinstance(bounded, dict) and {
        "execution_allowed",
        "selection_disposition",
        "run_identity_mode",
    }.issubset(bounded):
        derived = (
            bounded.pop("execution_allowed"),
            bounded.pop("selection_disposition"),
            bounded.pop("run_identity_mode"),
        )
    raw = _object(
        bounded,
        {
            "schema_name",
            "schema_version",
            "policy_id",
            "allowed_methods",
            "selected_method",
            "automatic_fallback",
            "method_evidence",
        },
        "robust optimizer policy",
    )
    if (
        raw["schema_name"] != ROBUST_OPTIMIZER_POLICY_SCHEMA_NAME
        or raw["schema_version"] != ROBUST_OPTIMIZER_POLICY_SCHEMA_VERSION
    ):
        raise ValueError("robust optimizer policy schema identity is unsupported")
    allowed = raw["allowed_methods"]
    if not isinstance(allowed, list) or not allowed:
        raise ValueError("allowed_methods must be a nonempty list")
    normalized_allowed = sorted({_identifier(item, "allowed_methods") for item in allowed})
    if len(normalized_allowed) != len(allowed) or not set(normalized_allowed).issubset(_METHODS):
        raise ValueError("allowed_methods must contain unique reviewed GCMMA/MMA methods")
    selected = _identifier(raw["selected_method"], "selected_method")
    if selected not in normalized_allowed:
        raise ValueError("selected_method must be explicitly allowed")
    if raw["automatic_fallback"] is not False:
        raise ValueError("robust optimizer automatic fallback is forbidden")
    evidence = raw["method_evidence"]
    if not isinstance(evidence, list) or len(evidence) != len(normalized_allowed):
        raise ValueError("method_evidence must exactly cover allowed_methods")
    normalized_evidence = []
    for index, item in enumerate(evidence):
        name = f"method_evidence[{index}]"
        entry = _object(item, {"method", "support_state", "evidence_sha256"}, name)
        method = _identifier(entry["method"], f"{name}.method")
        state = entry["support_state"]
        if state not in _SUPPORT_STATES:
            raise ValueError(f"{name}.support_state is unsupported")
        evidence_sha256 = _optional_sha256(entry["evidence_sha256"], f"{name}.evidence_sha256")
        if state == "validated" and evidence_sha256 is None:
            raise ValueError("validated optimizer methods require evidence_sha256")
        normalized_evidence.append(
            {"method": method, "support_state": state, "evidence_sha256": evidence_sha256}
        )
    normalized_evidence.sort(key=lambda item: item["method"])
    if [item["method"] for item in normalized_evidence] != normalized_allowed:
        raise ValueError("method_evidence must exactly cover allowed_methods")
    selected_evidence = next(item for item in normalized_evidence if item["method"] == selected)
    execution_allowed = selected_evidence["support_state"] == "validated"
    disposition = "validated_manual_selection" if execution_allowed else "validation_required"
    expected_derived = (execution_allowed, disposition, "optimizer_method_bound")
    if derived is not None and derived != expected_derived:
        raise ValueError("robust optimizer derived selection fields are invalid")
    body = {
        "schema_name": ROBUST_OPTIMIZER_POLICY_SCHEMA_NAME,
        "schema_version": ROBUST_OPTIMIZER_POLICY_SCHEMA_VERSION,
        "policy_id": _identifier(raw["policy_id"], "policy_id"),
        "allowed_methods": normalized_allowed,
        "selected_method": selected,
        "automatic_fallback": False,
        "method_evidence": normalized_evidence,
        "execution_allowed": execution_allowed,
        "selection_disposition": disposition,
        "run_identity_mode": "optimizer_method_bound",
    }
    body["policy_fingerprint"] = domain_sha256_v2(ROBUST_OPTIMIZER_POLICY_SCHEMA_NAME, body)
    if supplied is not None and supplied != body["policy_fingerprint"]:
        raise ValueError("robust optimizer policy fingerprint is invalid")
    return body


def _mesh_statistics(value: object, name: str) -> dict[str, Any]:
    raw = _object(
        value,
        {"element_count", "minimum_quality", "mean_quality", "quality_measure"},
        name,
    )
    count = raw["element_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError(f"{name}.element_count must be a positive integer")
    minimum = _finite(raw["minimum_quality"], f"{name}.minimum_quality")
    mean = _finite(raw["mean_quality"], f"{name}.mean_quality")
    if not 0.0 <= minimum <= 1.0 or not 0.0 <= mean <= 1.0:
        raise ValueError(f"{name} quality values must be within [0, 1]")
    if not isinstance(raw["quality_measure"], str) or not raw["quality_measure"]:
        raise ValueError(f"{name}.quality_measure must be nonempty")
    return {
        "element_count": count,
        "minimum_quality": minimum,
        "mean_quality": mean,
        "quality_measure": raw["quality_measure"],
    }


def assess_robust_optimizer_execution(
    configuration: object,
    native_receipt: object,
    *,
    native_receipt_sha256: object,
    max_elements_per_model: object,
    minimum_element_quality: object,
    deformation_jacobian_expression: object,
    minimum_relative_jacobian: object,
) -> dict[str, Any]:
    """Bind one native method attempt to caller budgets and fresh-forward evidence."""
    optimizer = normalize_native_optimizer_configuration(configuration)
    receipt = _bounded_json(native_receipt, "native optimizer receipt", 2 * 1024 * 1024)
    if not isinstance(receipt, dict):
        raise ValueError("native optimizer receipt must be an object")
    revision = receipt.get("source_revision")
    if (
        not isinstance(revision, str)
        or len(revision) != 40
        or any(character not in "0123456789abcdef" for character in revision.casefold())
    ):
        raise ValueError("native optimizer source revision is invalid")
    source_sha256 = _sha256(receipt.get("source_sha256"), "source_sha256")
    receipt_sha256 = _sha256(native_receipt_sha256, "native_receipt_sha256")
    if receipt.get("optimizer_method") != optimizer["method"]:
        raise ValueError("native optimizer receipt method differs from its configuration")
    if receipt.get("budget") != optimizer["budget"]:
        raise ValueError("native optimizer receipt budget differs from its configuration")
    maximum = max_elements_per_model
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
        raise ValueError("max_elements_per_model must be a caller-supplied positive integer")
    minimum_quality = _finite(minimum_element_quality, "minimum_element_quality", positive=True)
    if minimum_quality > 1.0:
        raise ValueError("minimum_element_quality must not exceed one")
    if (
        not isinstance(deformation_jacobian_expression, str)
        or not deformation_jacobian_expression.strip()
        or len(deformation_jacobian_expression) > 256
        or any(ord(character) < 32 for character in deformation_jacobian_expression)
    ):
        raise ValueError("deformation_jacobian_expression must be bounded printable text")
    jacobian_expression = deformation_jacobian_expression.strip()
    jacobian_threshold = _finite(minimum_relative_jacobian, "minimum_relative_jacobian")
    if jacobian_threshold < 0.0:
        raise ValueError("minimum_relative_jacobian must be nonnegative")
    cleanup = receipt.get("cleanup")
    cleanup_complete = bool(
        isinstance(cleanup, dict)
        and cleanup.get("client_clear") is True
        and cleanup.get("source_unchanged") is True
    )
    execution_success = receipt.get("success") is True and cleanup_complete
    failure_code = None
    if not execution_success:
        error = receipt.get("error")
        if isinstance(error, dict):
            candidate = error.get("code")
            if isinstance(candidate, str) and 0 < len(candidate) <= 128:
                failure_code = candidate
    baseline = None
    final = None
    fresh = None
    delta = None
    mesh_checks = {
        "policy_matches": False,
        "baseline_admitted": False,
        "finalist_remesh_admitted": False,
        "quality_measure_matches": False,
    }
    fresh_forward_improvement = False
    deformation_checks = {
        "policy_matches": False,
        "finite_evidence": False,
        "above_caller_threshold": False,
    }
    if execution_success:
        baseline = _finite(receipt.get("baseline_objective"), "baseline_objective")
        final = _finite(receipt.get("final_objective"), "final_objective")
        fresh = _finite(receipt.get("fresh_forward_objective"), "fresh_forward_objective")
        delta = _finite(receipt.get("fresh_forward_delta"), "fresh_forward_delta")
        if not math.isclose(delta, fresh - baseline, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("fresh-forward delta differs from objective evidence")
        if not math.isclose(fresh, final, rel_tol=1e-6, abs_tol=1e-9):
            raise ValueError("fresh-forward objective differs from native optimizer result")
        mesh_policy = receipt.get("mesh_admission_policy")
        if not isinstance(mesh_policy, dict):
            raise ValueError("native optimizer mesh admission policy is missing")
        mesh_checks["policy_matches"] = mesh_policy == {
            "max_elements_per_model": maximum,
            "minimum_element_quality": minimum_quality,
            "scope": "baseline_and_explicit_finalist_remesh",
            "internal_optimizer_remesh_callback": False,
        }
        baseline_mesh = _mesh_statistics(receipt.get("baseline_mesh"), "baseline_mesh")
        remesh = _object(receipt.get("remesh"), {"explicit_rebuild", "before", "after"}, "remesh")
        if remesh["explicit_rebuild"] is not True:
            raise ValueError("finalist remesh must be an explicit rebuild")
        remesh_before = _mesh_statistics(remesh["before"], "remesh.before")
        finalist_mesh = _mesh_statistics(remesh["after"], "remesh.after")
        mesh_checks["baseline_admitted"] = (
            baseline_mesh["element_count"] <= maximum
            and baseline_mesh["minimum_quality"] >= minimum_quality
        )
        mesh_checks["finalist_remesh_admitted"] = (
            finalist_mesh["element_count"] <= maximum
            and finalist_mesh["minimum_quality"] >= minimum_quality
        )
        mesh_checks["quality_measure_matches"] = (
            len(
                {
                    baseline_mesh["quality_measure"],
                    remesh_before["quality_measure"],
                    finalist_mesh["quality_measure"],
                }
            )
            == 1
        )
        fresh_forward_improvement = delta > 0.0
        expected_deformation_policy = {
            "jacobian_expression": jacobian_expression,
            "minimum_relative_jacobian": jacobian_threshold,
            "comparison": "strictly_greater_than",
            "scope": "fresh_forward_finalist_deformed_geometry",
        }
        deformation_checks["policy_matches"] = (
            receipt.get("deformation_feasibility_policy") == expected_deformation_policy
        )
        evidence = receipt.get("deformation_feasibility")
        if not isinstance(evidence, dict):
            raise ValueError("native optimizer deformation feasibility evidence is missing")
        sample_count = evidence.get("sample_count")
        minimum_jacobian = _finite(
            evidence.get("minimum_relative_jacobian"),
            "deformation_feasibility.minimum_relative_jacobian",
        )
        maximum_jacobian = _finite(
            evidence.get("maximum_relative_jacobian"),
            "deformation_feasibility.maximum_relative_jacobian",
        )
        deformation_checks["finite_evidence"] = (
            isinstance(sample_count, int)
            and not isinstance(sample_count, bool)
            and sample_count >= 1
            and minimum_jacobian <= maximum_jacobian
            and evidence.get("threshold") == jacobian_threshold
        )
        deformation_checks["above_caller_threshold"] = (
            minimum_jacobian > jacobian_threshold and evidence.get("passed") is True
        )
    accepted = (
        execution_success
        and fresh_forward_improvement
        and all(mesh_checks.values())
        and all(deformation_checks.values())
    )
    body = {
        "schema_name": ROBUST_OPTIMIZER_EXECUTION_RECEIPT_SCHEMA_NAME,
        "schema_version": ROBUST_OPTIMIZER_EXECUTION_RECEIPT_SCHEMA_VERSION,
        "source_revision": revision.casefold(),
        "source_sha256": source_sha256,
        "optimizer_fingerprint": optimizer["optimizer_fingerprint"],
        "method": optimizer["method"],
        "native_receipt_sha256": receipt_sha256,
        "execution_success": execution_success,
        "failure_code": failure_code,
        "cleanup_complete": cleanup_complete,
        "baseline_objective": baseline,
        "native_final_objective": final,
        "fresh_forward_objective": fresh,
        "fresh_forward_delta": delta,
        "fresh_forward_improvement": fresh_forward_improvement,
        "mesh_checks": mesh_checks,
        "deformation_checks": deformation_checks,
        "disposition": "accepted" if accepted else "rejected",
        "automatic_fallback_used": False,
    }
    return {
        **body,
        "receipt_fingerprint": domain_sha256_v2(
            ROBUST_OPTIMIZER_EXECUTION_RECEIPT_SCHEMA_NAME, body
        ),
    }


__all__ = [
    "ROBUST_OPTIMIZER_EXECUTION_RECEIPT_SCHEMA_NAME",
    "ROBUST_OPTIMIZER_EXECUTION_RECEIPT_SCHEMA_VERSION",
    "ROBUST_OPTIMIZER_POLICY_SCHEMA_NAME",
    "ROBUST_OPTIMIZER_POLICY_SCHEMA_VERSION",
    "assess_robust_optimizer_execution",
    "normalize_robust_optimizer_policy",
]
