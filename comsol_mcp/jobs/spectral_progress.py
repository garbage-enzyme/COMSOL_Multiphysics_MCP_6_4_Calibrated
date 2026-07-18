"""Solver-free adaptive progress decisions for durable spectral jobs."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from typing import Any, Mapping

from comsol_mcp.evidence.spectral_characterization import (
    build_spectral_analysis_decision,
    build_spectral_characterization,
    build_spectral_point_bundle,
)

from .spectral_rows import spectral_point_identity
from .spectral_stages import (
    build_initial_spectral_stage,
    build_spectral_stage_plan,
    inclusive_wavelength_grid,
)


SPECTRAL_PROGRESS_SCHEMA_NAME = "comsol_mcp.spectral_progress"
SPECTRAL_PROGRESS_SCHEMA_VERSION = "1.0.0"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object with string keys")
    return dict(value)


def _analysis_artifacts(spec: Mapping[str, Any], rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: float(row["requested_wavelength_m"]))
    inputs = _mapping(_mapping(spec.get("collector"), "collector").get("inputs"), "collector.inputs")
    bundle = build_spectral_point_bundle(
        bundle_id=f"spectrum-{str(spec['spec_fingerprint'])[:20]}",
        source_model={
            "relative_identity": spec["source_model_relative_identity"],
            "sha256": spec["source_model_sha256"],
        },
        configuration_sha256=spec["configuration_sha256"],
        parameter_state=spec["parameter_state"],
        wavelength_convention={
            "unit": "m",
            "requested_field": "requested_wavelength_m",
            "evaluated_field": "evaluated_wavelength_m",
            "frequency_derived_field": "frequency_wavelength_m",
            "frequency_relation": "c_const/frequency",
        },
        expressions={
            "R": inputs["r_expression"],
            "T": inputs["t_expression"],
            "A": inputs["a_expression"],
        },
        rows=[
            {
                "row_id": row["point_id"],
                "raw_row_sha256": row["row_sha256"],
                "configuration_sha256": row["configuration_sha256"],
                "requested_wavelength_m": row["requested_wavelength_m"],
                "evaluated_wavelength_m": row["evaluated_wavelength_m"],
                "frequency_wavelength_m": row["frequency_wavelength_m"],
                "R": row["R"],
                "T": row["T"],
                "A": row["A"],
            }
            for row in ordered
        ],
    )
    decision = build_spectral_analysis_decision(bundle, spec["analysis_policy"])
    characterization = build_spectral_characterization(
        bundle,
        decision,
        spec["measurement_configuration"],
    )
    return {
        "bundle": bundle,
        "decision": decision,
        "characterization": characterization,
    }


def _validate_stage_row_membership(
    plans: list[Mapping[str, Any]], rows: list[Mapping[str, Any]]
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    planned: dict[str, Mapping[str, Any]] = {}
    for plan in plans:
        points = plan.get("requested_points")
        if not isinstance(points, list):
            raise ValueError("stage requested_points are unavailable")
        for point in points:
            item = _mapping(point, "stage point")
            fingerprint = item.get("point_fingerprint")
            if not isinstance(fingerprint, str) or fingerprint in planned:
                raise ValueError("stage point identities are invalid or duplicated")
            planned[fingerprint] = plan
    observed: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        fingerprint = row.get("point_fingerprint")
        if fingerprint not in planned:
            raise ValueError("spectral row was not requested by a frozen stage")
        plan = planned[fingerprint]
        if row.get("stage_index") != plan.get("stage_index") or row.get("stage_kind") != plan.get("stage_kind"):
            raise ValueError("spectral row stage identity differs from its frozen plan")
        if fingerprint in observed:
            raise ValueError("duplicate complete spectral row identity")
        observed[str(fingerprint)] = row
    for plan in plans[:-1]:
        missing = [
            point["point_fingerprint"]
            for point in plan["requested_points"]
            if point["point_fingerprint"] not in observed
        ]
        if missing:
            raise ValueError("a later spectral stage exists before its predecessor completed")
    return planned, observed


def _pending_points(plan: Mapping[str, Any], observed: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_fingerprint = {
        point["point_fingerprint"]: wavelength
        for point, wavelength in zip(
            plan["requested_points"], plan["requested_wavelengths_m"]
        )
    }
    return [
        {
            "point_id": point["point_id"],
            "point_fingerprint": point["point_fingerprint"],
            "requested_wavelength_m": by_fingerprint[point["point_fingerprint"]],
        }
        for point in plan["requested_points"]
        if point["point_fingerprint"] not in observed
    ]


def _fit_support_is_stable(
    characterization: Mapping[str, Any], policy: Mapping[str, Any]
) -> tuple[bool, str]:
    candidate = characterization.get("candidate")
    if not isinstance(candidate, Mapping):
        return False, "candidate_not_measured"
    sensitivity = candidate.get("fit_support_sensitivity")
    if not isinstance(sensitivity, Mapping):
        return False, "fit_support_sensitivity_missing"
    if sensitivity.get("state") == "not_requested":
        return True, "fit_support_sensitivity_not_requested"
    measurements = sensitivity.get("measurements")
    if not isinstance(measurements, list) or any(
        not isinstance(item, Mapping) or item.get("state") != "measured"
        for item in measurements
    ):
        return False, "fit_support_measurement_failed"
    spans = _mapping(sensitivity.get("spans"), "fit support spans")
    checks = (
        ("peak_wavelength_m", "fit_support_peak_abs_tolerance_m"),
        ("fwhm_m", "fit_support_fwhm_abs_tolerance_m"),
        ("quality_factor", "fit_support_quality_factor_abs_tolerance"),
    )
    for span_name, tolerance_name in checks:
        span = spans.get(span_name)
        if span is not None and float(span) > float(policy[tolerance_name]):
            return False, f"{span_name}_fit_sensitive"
    return True, "fit_support_within_declared_tolerances"


def _completion(
    *,
    reason_code: str,
    disposition: str,
    declared_cap_reached: bool,
) -> dict[str, Any]:
    return {
        "action": "complete",
        "reason_code": reason_code,
        "scientific_disposition": disposition,
        "declared_cap_reached": declared_cap_reached,
        "pending_points": [],
        "next_stage_plan": None,
    }


def _new_targets(
    spec: Mapping[str, Any],
    plans: list[Mapping[str, Any]],
    wavelengths: list[float],
) -> list[float]:
    existing = {
        point["point_fingerprint"]
        for plan in plans
        for point in plan["requested_points"]
    }
    return [
        wavelength
        for wavelength in wavelengths
        if spectral_point_identity(spec, wavelength)["point_fingerprint"] not in existing
    ]


def _expansion_plan(
    spec: Mapping[str, Any],
    plans: list[Mapping[str, Any]],
    rows: list[Mapping[str, Any]],
    artifacts: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    policy = spec["expansion_policy"]
    expansion_count = sum(plan["stage_kind"] == "window_expansion" for plan in plans)
    if expansion_count >= policy["maximum_expansions"]:
        return None, "window_expansion_count_cap_reached"
    lower = min(float(row["requested_wavelength_m"]) for row in rows)
    upper = max(float(row["requested_wavelength_m"]) for row in rows)
    span = upper - lower
    decision = artifacts["decision"]
    bundle_rows = artifacts["bundle"]["rows"]
    boundary_ids = set(decision["boundary_row_ids"])
    response = decision["analysis_policy"]["response_quantity"]
    polarity = decision["analysis_policy"]["candidate_polarity"]
    oriented = {
        row["row_id"]: row[response] if polarity == "maximum" else -row[response]
        for row in bundle_rows
        if row["row_id"] in boundary_ids
    }
    maximum = max(oriented.values())
    sides = {
        "lower" if next(row for row in bundle_rows if row["row_id"] == row_id)["requested_wavelength_m"] == lower else "upper"
        for row_id, value in oriented.items()
        if value == maximum
    }
    extra = span * (float(policy["span_multiplier"]) - 1.0)
    if sides == {"lower", "upper"}:
        lower_extra = upper_extra = extra / 2.0
    else:
        lower_extra = extra if "lower" in sides else 0.0
        upper_extra = extra if "upper" in sides else 0.0
    new_lower = max(float(policy["absolute_lower_m"]), lower - lower_extra)
    new_upper = min(float(policy["absolute_upper_m"]), upper + upper_extra)
    if new_lower == lower and new_upper == upper:
        return None, "window_expansion_absolute_bound_reached"
    grid = inclusive_wavelength_grid(
        new_lower, new_upper, int(policy["points_per_expansion"])
    )
    targets = _new_targets(spec, plans, grid)
    planned_count = sum(len(plan["requested_points"]) for plan in plans)
    if not targets:
        return None, "window_expansion_has_no_new_exact_points"
    if planned_count + len(targets) > int(spec["maximum_points"]):
        return None, "window_expansion_point_cap_reached"
    plan = build_spectral_stage_plan(
        spec,
        stage_index=len(plans),
        stage_kind="window_expansion",
        planning_reason="measured_boundary_requires_bounded_expansion",
        window_lower_m=new_lower,
        window_upper_m=new_upper,
        requested_wavelengths_m=targets,
        previous_stage_sha256=plans[-1]["stage_sha256"],
        evidence_row_sha256=rows[-1]["row_sha256"],
    )
    return plan, "window_expansion_planned"


def _candidate_peak(artifacts: Mapping[str, Any]) -> float | None:
    characterization = artifacts["characterization"]
    candidate = characterization.get("candidate")
    if characterization.get("measurement_state") != "measured" or not isinstance(candidate, Mapping):
        return None
    peak = candidate.get("peak")
    if not isinstance(peak, Mapping):
        return None
    value = peak.get("wavelength_m")
    return float(value) if isinstance(value, (int, float)) and math.isfinite(float(value)) else None


def _refinement_plan(
    spec: Mapping[str, Any],
    plans: list[Mapping[str, Any]],
    rows: list[Mapping[str, Any]],
    artifacts: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    policy = spec["refinement_policy"]
    refinement_count = sum(plan["stage_kind"] == "refinement" for plan in plans)
    if refinement_count >= policy["maximum_stages"]:
        return None, "refinement_stage_cap_reached"
    peak = _candidate_peak(artifacts)
    if peak is None:
        return None, "candidate_measurement_unavailable"
    lower = min(float(row["requested_wavelength_m"]) for row in rows)
    upper = max(float(row["requested_wavelength_m"]) for row in rows)
    width = (upper - lower) / float(policy["span_shrink_factor"])
    absolute = spec["expansion_policy"]
    new_lower = max(float(absolute["absolute_lower_m"]), peak - width / 2.0)
    new_upper = min(float(absolute["absolute_upper_m"]), peak + width / 2.0)
    point_count = int(policy["points_per_stage"])
    spacing = (new_upper - new_lower) / (point_count - 1)
    if spacing < float(policy["minimum_spacing_m"]):
        return None, "refinement_minimum_spacing_reached"
    grid = inclusive_wavelength_grid(new_lower, new_upper, point_count)
    targets = _new_targets(spec, plans, grid)
    planned_count = sum(len(plan["requested_points"]) for plan in plans)
    if not targets:
        return None, "refinement_has_no_new_exact_points"
    if planned_count + len(targets) > int(spec["maximum_points"]):
        return None, "refinement_point_cap_reached"
    return (
        build_spectral_stage_plan(
            spec,
            stage_index=len(plans),
            stage_kind="refinement",
            planning_reason="measured_interior_candidate_refinement",
            window_lower_m=new_lower,
            window_upper_m=new_upper,
            requested_wavelengths_m=targets,
            previous_stage_sha256=plans[-1]["stage_sha256"],
            evidence_row_sha256=rows[-1]["row_sha256"],
        ),
        "refinement_planned",
    )


def _final_candidate_disposition(
    spec: Mapping[str, Any],
    plans: list[Mapping[str, Any]],
    rows: list[Mapping[str, Any]],
    artifacts: Mapping[str, Any],
    cap_reason: str,
) -> dict[str, Any]:
    characterization = artifacts["characterization"]
    candidate = characterization.get("candidate")
    if characterization.get("measurement_state") != "measured" or not isinstance(candidate, Mapping):
        return _completion(
            reason_code=characterization.get("reason_code", "candidate_not_measured"),
            disposition="residual",
            declared_cap_reached=True,
        )
    fwhm = candidate.get("fwhm")
    quality = candidate.get("quality_factor")
    if not isinstance(fwhm, Mapping) or fwhm.get("state") != "bracketed":
        return _completion(
            reason_code="fwhm_unbracketed_at_declared_cap",
            disposition="residual",
            declared_cap_reached=True,
        )
    if not isinstance(quality, Mapping) or quality.get("state") != "computed_from_bracketed_fwhm":
        return _completion(
            reason_code="quality_factor_unavailable",
            disposition="residual",
            declared_cap_reached=True,
        )
    fit_stable, fit_reason = _fit_support_is_stable(
        characterization, spec["refinement_policy"]
    )
    if not fit_stable:
        return _completion(
            reason_code=fit_reason,
            disposition="residual",
            declared_cap_reached=True,
        )
    refinement_count = sum(plan["stage_kind"] == "refinement" for plan in plans)
    if refinement_count:
        previous_rows = [row for row in rows if int(row["stage_index"]) < len(plans) - 1]
        previous_artifacts = _analysis_artifacts(spec, previous_rows)
        previous_peak = _candidate_peak(previous_artifacts)
        current_peak = _candidate_peak(artifacts)
        if previous_peak is None or current_peak is None:
            return _completion(
                reason_code="refinement_peak_comparison_unavailable",
                disposition="residual",
                declared_cap_reached=True,
            )
        if abs(current_peak - previous_peak) > float(
            spec["refinement_policy"]["peak_shift_abs_tolerance_m"]
        ):
            return _completion(
                reason_code="refinement_peak_shift_exceeds_tolerance",
                disposition="residual",
                declared_cap_reached=True,
            )
    return _completion(
        reason_code=(
            "candidate_characterization_accepted"
            if cap_reason == "refinement_converged"
            else "candidate_characterization_accepted_at_declared_refinement_limit"
        ),
        disposition="accepted",
        declared_cap_reached=cap_reason != "refinement_converged",
    )


def _action_after_completed_stage(
    spec: Mapping[str, Any],
    plans: list[Mapping[str, Any]],
    rows: list[Mapping[str, Any]],
    artifacts: Mapping[str, Any],
) -> dict[str, Any]:
    classification = artifacts["decision"]["classification"]
    if classification == "invalid_evidence":
        return _completion(
            reason_code="spectral_point_evidence_invalid",
            disposition="invalid_evidence",
            declared_cap_reached=False,
        )
    if classification == "boundary_high":
        next_plan, reason = _expansion_plan(spec, plans, rows, artifacts)
        if next_plan is not None:
            return {
                "action": "schedule_next_stage",
                "reason_code": reason,
                "scientific_disposition": "not_assessed",
                "declared_cap_reached": False,
                "pending_points": [],
                "next_stage_plan": next_plan,
            }
        return _completion(
            reason_code=reason,
            disposition="unresolved_at_declared_cap",
            declared_cap_reached=True,
        )
    if classification == "interior_candidate":
        refinement_count = sum(plan["stage_kind"] == "refinement" for plan in plans)
        converged = False
        if refinement_count:
            previous_rows = [
                row for row in rows if int(row["stage_index"]) < len(plans) - 1
            ]
            previous_peak = _candidate_peak(_analysis_artifacts(spec, previous_rows))
            current_peak = _candidate_peak(artifacts)
            converged = (
                previous_peak is not None
                and current_peak is not None
                and abs(current_peak - previous_peak)
                <= float(spec["refinement_policy"]["peak_shift_abs_tolerance_m"])
            )
        if converged:
            return _final_candidate_disposition(
                spec,
                plans,
                rows,
                artifacts,
                "refinement_converged",
            )
        next_plan, reason = _refinement_plan(spec, plans, rows, artifacts)
        if next_plan is not None:
            return {
                "action": "schedule_next_stage",
                "reason_code": reason,
                "scientific_disposition": "not_assessed",
                "declared_cap_reached": False,
                "pending_points": [],
                "next_stage_plan": next_plan,
            }
        return _final_candidate_disposition(
            spec,
            plans,
            rows,
            artifacts,
            reason,
        )
    return _completion(
        reason_code=f"classification_{classification}",
        disposition="residual",
        declared_cap_reached=False,
    )


def _validate_adaptive_stage_history(
    spec: Mapping[str, Any],
    plans: list[Mapping[str, Any]],
    rows: list[Mapping[str, Any]],
) -> None:
    if not plans:
        return
    if plans[0] != build_initial_spectral_stage(spec):
        raise ValueError("initial spectral stage differs from the immutable job")
    for index in range(1, len(plans)):
        prior_plans = plans[:index]
        prior_fingerprints = {
            point["point_fingerprint"]
            for plan in prior_plans
            for point in plan["requested_points"]
        }
        prior_rows = [
            row for row in rows if row.get("point_fingerprint") in prior_fingerprints
        ]
        _planned, observed = _validate_stage_row_membership(prior_plans, prior_rows)
        if _pending_points(prior_plans[-1], observed):
            raise ValueError("adaptive spectral stage was frozen before its evidence completed")
        artifacts = _analysis_artifacts(spec, prior_rows)
        expected_action = _action_after_completed_stage(
            spec, prior_plans, prior_rows, artifacts
        )
        if expected_action["action"] != "schedule_next_stage":
            raise ValueError("adaptive spectral stage exists after a terminal scientific decision")
        if expected_action["next_stage_plan"] != plans[index]:
            raise ValueError("adaptive spectral stage differs from deterministic evidence replay")


def build_spectral_progress(
    spec: Mapping[str, Any],
    stage_plans: list[Mapping[str, Any]],
    rows: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return the next bounded action without starting a solver or mutating evidence."""
    if spec.get("job_type") != "spectral_characterization":
        raise ValueError("spectral progress requires a spectral_characterization job")
    plans = [deepcopy(_mapping(plan, "stage plan")) for plan in stage_plans]
    normalized_rows = [deepcopy(_mapping(row, "spectral row")) for row in rows]
    if not plans:
        action = {
            "action": "schedule_next_stage",
            "reason_code": "initial_locator_required",
            "scientific_disposition": "not_assessed",
            "declared_cap_reached": False,
            "pending_points": [],
            "next_stage_plan": build_initial_spectral_stage(spec),
        }
        artifacts = None
    else:
        _planned, observed = _validate_stage_row_membership(plans, normalized_rows)
        _validate_adaptive_stage_history(spec, plans, normalized_rows)
        pending = _pending_points(plans[-1], observed)
        if pending:
            action = {
                "action": "solve_current_stage",
                "reason_code": "frozen_stage_has_pending_points",
                "scientific_disposition": "not_assessed",
                "declared_cap_reached": False,
                "pending_points": pending,
                "next_stage_plan": None,
            }
            artifacts = None
        else:
            artifacts = _analysis_artifacts(spec, normalized_rows)
            action = _action_after_completed_stage(
                spec, plans, normalized_rows, artifacts
            )

    body = {
        "schema_name": SPECTRAL_PROGRESS_SCHEMA_NAME,
        "schema_version": SPECTRAL_PROGRESS_SCHEMA_VERSION,
        "spec_fingerprint": spec["spec_fingerprint"],
        "stage_count": len(plans),
        "row_count": len(normalized_rows),
        "last_stage_sha256": plans[-1]["stage_sha256"] if plans else None,
        "last_row_sha256": normalized_rows[-1]["row_sha256"] if normalized_rows else None,
        **action,
        "analysis": artifacts,
    }
    return {**body, "progress_sha256": _fingerprint(body)}


__all__ = [
    "SPECTRAL_PROGRESS_SCHEMA_NAME",
    "SPECTRAL_PROGRESS_SCHEMA_VERSION",
    "build_spectral_progress",
]
