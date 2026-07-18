"""Pure evaluation and artifact accounting for the reference-power licensed gate."""

from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
from typing import Any, Mapping

from .contracts import (
    build_physical_evidence,
    build_validation_policy,
    evaluate_physical_evidence_policy,
    example_validation_policies,
)
from .reference_power_acceptance import validate_reference_power_acceptance_contract


def build_reference_power_policies(contract: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    strict = validate_reference_power_acceptance_contract(contract)
    acceptance = strict["acceptance"]
    reference_template = example_validation_policies()["reference_air_polarization_ratio"]
    reference_rule = deepcopy(reference_template["rules"][0])
    reference_rule["tolerances"] = {
        "minimum_ratio": acceptance["reference_air"]["target_to_transverse_ratio_min"]
    }
    reference = build_validation_policy(
        {
            "schema_name": reference_template["schema_name"],
            "schema_version": reference_template["schema_version"],
            "policy_id": "reference_power.reference_air",
            "rules": [reference_rule],
        }
    )
    flux_template = example_validation_policies()["declared_flux_closure"]
    flux_rule = deepcopy(flux_template["rules"][0])
    flux_rule["tolerances"] = {
        "closure_abs": acceptance["declared_flux"]["closure_abs_max"],
        "margin": acceptance["declared_flux"]["margin"],
    }
    flux = build_validation_policy(
        {
            "schema_name": flux_template["schema_name"],
            "schema_version": flux_template["schema_version"],
            "policy_id": "reference_power.declared_flux",
            "rules": [flux_rule],
        }
    )
    return {"reference_air": reference, "declared_flux": flux}


def _rebuilt_with_record_value(
    physical_evidence: Mapping[str, Any], key: str, value: Any
) -> dict[str, Any]:
    payload = deepcopy(dict(physical_evidence))
    payload.pop("contract_sha256", None)
    record = payload["evidence"].get(key)
    if not isinstance(record, dict) or "value" not in record:
        raise ValueError(f"required negative-control evidence is unavailable: {key}")
    record["value"] = value
    return build_physical_evidence(payload)


def evaluate_reference_power_negative_controls(
    physical_evidence: Mapping[str, Any], flux_policy: Mapping[str, Any]
) -> dict[str, Any]:
    sign_record = physical_evidence["evidence"].get("flux.reflected_positive_power_sign")
    sign = sign_record.get("value") if isinstance(sign_record, dict) else None
    if sign not in {-1, 1}:
        raise ValueError("reflected positive-power sign is unavailable for the negative control")
    reversed_evidence = _rebuilt_with_record_value(
        physical_evidence,
        "flux.reflected_positive_power_sign",
        -int(sign),
    )
    ineligible_evidence = _rebuilt_with_record_value(
        physical_evidence,
        "flux.physical_flux_closure_eligible",
        False,
    )
    reversed_result = evaluate_physical_evidence_policy(reversed_evidence, flux_policy)
    ineligible_result = evaluate_physical_evidence_policy(ineligible_evidence, flux_policy)
    return {
        "reversed_sign": {
            "overall": reversed_result["overall"],
            "passed_rejection_gate": reversed_result["overall"] == "fail",
            "policy_evaluation": reversed_result,
        },
        "internal_consistency_substitution": {
            "overall": ineligible_result["overall"],
            "passed_rejection_gate": ineligible_result["overall"] == "fail",
            "policy_evaluation": ineligible_result,
        },
    }


def evaluate_reference_power_results(
    contract: Mapping[str, Any],
    reference_result: Mapping[str, Any],
    point_result: Mapping[str, Any],
) -> dict[str, Any]:
    strict = validate_reference_power_acceptance_contract(contract)
    policies = build_reference_power_policies(strict)
    acceptance = strict["acceptance"]
    reference = reference_result.get("reference") or {}
    point_measurement = point_result.get("measurement") or {}
    wavelength = point_measurement.get("wavelength") or {}
    negative = evaluate_reference_power_negative_controls(
        point_result["physical_evidence"], policies["declared_flux"]
    )
    reference_checks = {
        "audit_complete": reference_result.get("audit_status") == "measurement_complete",
        "policy_pass": reference_result.get("assessment", {}).get("overall") == "pass",
        "reflection": float(reference.get("R", float("inf")))
        <= acceptance["reference_air"]["reflection_max"],
        "r_plus_t_residual": float(reference.get("R_plus_T_residual_abs", float("inf")))
        <= acceptance["reference_air"]["r_plus_t_residual_max"],
        "target_ratio": float(reference.get("target_to_transverse_ratio", -1.0))
        >= acceptance["reference_air"]["target_to_transverse_ratio_min"],
        "clone_cleanup": reference_result.get("cleanup", {}).get("removed") is True,
    }
    point_checks = {
        "audit_complete": point_result.get("audit_status") == "policy_evaluated",
        "policy_pass": point_result.get("assessment", {}).get("project_verdict") == "pass",
        "wavelength_absolute": float(wavelength.get("absolute_difference_m", float("inf")))
        <= acceptance["wavelength"]["absolute_m_max"],
        "wavelength_relative": float(wavelength.get("relative_difference", float("inf")))
        <= acceptance["wavelength"]["relative_max"],
        "source_unchanged": point_measurement.get("integrity", {}).get("source_unchanged") is True,
    }
    negative_checks = {
        "reversed_sign_rejected": negative["reversed_sign"]["passed_rejection_gate"],
        "internal_substitution_rejected": negative["internal_consistency_substitution"]["passed_rejection_gate"],
    }
    checks = {
        "reference_air": reference_checks,
        "physical_point": point_checks,
        "negative_controls": negative_checks,
    }
    passed = all(value for group in checks.values() for value in group.values())
    return {
        "passed": passed,
        "checks": checks,
        "negative_controls": negative,
        "raw": {
            "reference": {
                "R": reference.get("R"),
                "T": reference.get("T"),
                "R_plus_T_residual_abs": reference.get("R_plus_T_residual_abs"),
                "target_to_transverse_ratio": reference.get("target_to_transverse_ratio"),
            },
            "physical_flux": point_measurement.get("declared_plane_flux"),
            "wavelength": wavelength,
        },
    }


def inventory_reference_power_artifacts(root: Path, limits: Mapping[str, Any]) -> dict[str, Any]:
    resolved = root.resolve()
    if not resolved.is_dir():
        raise ValueError("reference-power artifact root does not exist")
    files = sorted(path for path in resolved.rglob("*") if path.is_file())
    if len(files) > int(limits["max_artifact_files"]):
        raise ValueError("reference-power artifact file count exceeds the contract")
    total = sum(path.stat().st_size for path in files)
    if total > int(limits["max_artifact_bytes"]):
        raise ValueError("reference-power artifact bytes exceed the contract")
    entries = []
    for path in files:
        relative = path.resolve().relative_to(resolved).as_posix()
        entries.append(
            {
                "relative_path": relative,
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return {"file_count": len(entries), "total_bytes": total, "files": entries}


__all__ = [
    "build_reference_power_policies",
    "evaluate_reference_power_negative_controls",
    "evaluate_reference_power_results",
    "inventory_reference_power_artifacts",
]
