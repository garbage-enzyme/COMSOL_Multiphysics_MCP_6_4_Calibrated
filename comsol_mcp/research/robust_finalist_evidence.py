"""Independent finalist robustness evidence assessment."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .derivative_support import _bounded_json, _finite, _identifier, _object, _sha256
from .external_validation import normalize_external_validation_receipt
from .robust_conditions import normalize_optimization_condition_table
from .robust_finalist_validation import normalize_robust_finalist_validation_policy
from .shape_support import normalize_shape_support_policy

ROBUST_FINALIST_VALIDATION_RECEIPT_SCHEMA_NAME = "comsol_mcp.robust_finalist_validation_receipt"
ROBUST_FINALIST_VALIDATION_RECEIPT_SCHEMA_VERSION = "1.0.0"

_CHECK_NAMES = (
    "manufacturability",
    "fresh_remesh",
    "mesh_convergence",
    "branch_guard",
    "off_design",
    "external_fidelity",
)


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer in the allowed range")
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be boolean")
    return value


def _optional_finite(value: object, name: str) -> float | None:
    return None if value is None else _finite(value, name)


def _optional_identifier(value: object, name: str) -> str | None:
    return None if value is None else _identifier(value, name)


def _manufacturability(value: object, shape: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    raw = _object(
        value,
        {
            "shape_policy_fingerprint",
            "minimum_gap_m",
            "minimum_thickness_m",
            "minimum_radius_m",
            "topology_preserved",
            "selections_preserved",
            "positive_dimensions",
            "self_intersection_absent",
            "evidence_sha256",
        },
        "manufacturability evidence",
    )
    if raw["shape_policy_fingerprint"] != shape["policy_fingerprint"]:
        raise ValueError("manufacturability shape policy identity changed")
    gap = _optional_finite(raw["minimum_gap_m"], "minimum_gap_m")
    thickness = _optional_finite(raw["minimum_thickness_m"], "minimum_thickness_m")
    radius = _optional_finite(raw["minimum_radius_m"], "minimum_radius_m")
    required_gap = shape["minimum_gap"]["effective_value_m"]
    guards = shape["geometry_guards"]
    booleans = {
        "topology_preserved": _boolean(raw["topology_preserved"], "topology_preserved"),
        "selections_preserved": _boolean(raw["selections_preserved"], "selections_preserved"),
        "positive_dimensions": _boolean(raw["positive_dimensions"], "positive_dimensions"),
        "self_intersection_absent": _boolean(
            raw["self_intersection_absent"], "self_intersection_absent"
        ),
    }
    accepted = (
        (
            required_gap is None
            and gap is None
            or required_gap is not None
            and gap is not None
            and gap >= required_gap
        )
        and (
            guards["minimum_thickness_m"] is None
            or thickness is not None
            and thickness >= guards["minimum_thickness_m"]
        )
        and (
            guards["minimum_radius_m"] is None
            or radius is not None
            and radius >= guards["minimum_radius_m"]
        )
        and all(booleans.values())
    )
    return {
        "shape_policy_fingerprint": shape["policy_fingerprint"],
        "minimum_gap_m": gap,
        "minimum_thickness_m": thickness,
        "minimum_radius_m": radius,
        **booleans,
        "evidence_sha256": _sha256(raw["evidence_sha256"], "manufacturability evidence"),
    }, accepted


def _fresh_remesh(
    value: object,
    *,
    candidate: str,
    mesh_policy: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    raw = _object(
        value,
        {
            "candidate_fingerprint",
            "explicit_rebuild",
            "optimizer_state_reused",
            "model_sha256",
            "mesh_sha256",
            "element_count",
            "minimum_element_quality",
            "quality_measure",
            "objective_evidence_sha256",
            "evidence_sha256",
        },
        "fresh remesh evidence",
    )
    if raw["candidate_fingerprint"] != candidate:
        raise ValueError("fresh remesh candidate identity changed")
    count = _integer(raw["element_count"], "fresh remesh element_count", minimum=1)
    quality = _finite(raw["minimum_element_quality"], "fresh remesh quality", positive=True)
    measure = _identifier(raw["quality_measure"], "fresh remesh quality_measure")
    explicit = _boolean(raw["explicit_rebuild"], "fresh remesh explicit_rebuild")
    reused = _boolean(raw["optimizer_state_reused"], "fresh remesh optimizer_state_reused")
    accepted = (
        explicit
        and not reused
        and count <= mesh_policy["max_elements_per_model"]
        and quality >= mesh_policy["minimum_element_quality"]
        and measure == mesh_policy["quality_measure"]
    )
    return {
        "candidate_fingerprint": candidate,
        "explicit_rebuild": explicit,
        "optimizer_state_reused": reused,
        "model_sha256": _sha256(raw["model_sha256"], "fresh remesh model_sha256"),
        "mesh_sha256": _sha256(raw["mesh_sha256"], "fresh remesh mesh_sha256"),
        "element_count": count,
        "minimum_element_quality": quality,
        "quality_measure": measure,
        "objective_evidence_sha256": _sha256(
            raw["objective_evidence_sha256"], "fresh remesh objective evidence"
        ),
        "evidence_sha256": _sha256(raw["evidence_sha256"], "fresh remesh evidence"),
    }, accepted


def _mesh_level(
    value: object,
    *,
    expected_id: str,
    candidate: str,
    mesh_policy: Mapping[str, Any],
) -> dict[str, Any]:
    raw = _object(
        value,
        {
            "level_id",
            "candidate_fingerprint",
            "model_sha256",
            "mesh_sha256",
            "element_count",
            "minimum_element_quality",
            "quality_measure",
            "objective_configuration_fingerprint",
            "objective_value",
            "objective_evidence_sha256",
        },
        f"mesh convergence level {expected_id}",
    )
    if raw["level_id"] != expected_id or raw["candidate_fingerprint"] != candidate:
        raise ValueError("mesh convergence level identity changed")
    return {
        "level_id": expected_id,
        "candidate_fingerprint": candidate,
        "model_sha256": _sha256(raw["model_sha256"], "mesh convergence model_sha256"),
        "mesh_sha256": _sha256(raw["mesh_sha256"], "mesh convergence mesh_sha256"),
        "element_count": _integer(raw["element_count"], "mesh element_count", minimum=1),
        "minimum_element_quality": _finite(
            raw["minimum_element_quality"], "mesh minimum quality", positive=True
        ),
        "quality_measure": _identifier(raw["quality_measure"], "mesh quality_measure"),
        "objective_configuration_fingerprint": _sha256(
            raw["objective_configuration_fingerprint"], "objective configuration"
        ),
        "objective_value": _finite(raw["objective_value"], "mesh objective_value"),
        "objective_evidence_sha256": _sha256(
            raw["objective_evidence_sha256"], "mesh objective evidence"
        ),
        "admitted": (
            raw["element_count"] <= mesh_policy["max_elements_per_model"]
            and raw["minimum_element_quality"] >= mesh_policy["minimum_element_quality"]
            and raw["quality_measure"] == mesh_policy["quality_measure"]
        ),
    }


def _mesh_convergence(
    value: object,
    *,
    candidate: str,
    mesh_policy: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    raw = _object(value, {"levels", "evidence_sha256"}, "mesh convergence evidence")
    levels = raw["levels"]
    if not isinstance(levels, list) or len(levels) != 2:
        raise ValueError("mesh convergence requires exactly baseline and finer levels")
    baseline = _mesh_level(
        levels[0],
        expected_id=mesh_policy["baseline_level_id"],
        candidate=candidate,
        mesh_policy=mesh_policy,
    )
    finer = _mesh_level(
        levels[1],
        expected_id=mesh_policy["finer_level_id"],
        candidate=candidate,
        mesh_policy=mesh_policy,
    )
    if baseline["mesh_sha256"] == finer["mesh_sha256"]:
        raise ValueError("mesh convergence levels must use distinct mesh identities")
    same_objective = (
        baseline["objective_configuration_fingerprint"]
        == finer["objective_configuration_fingerprint"]
    )
    baseline_value = baseline["objective_value"]
    finer_value = finer["objective_value"]
    if baseline_value == 0.0:
        relative_change = 0.0 if finer_value == 0.0 else None
    else:
        relative_change = abs(finer_value - baseline_value) / abs(baseline_value)
    accepted = (
        baseline["admitted"]
        and finer["admitted"]
        and finer["element_count"] > baseline["element_count"]
        and same_objective
        and relative_change is not None
        and relative_change <= mesh_policy["max_relative_objective_change"]
    )
    return {
        "levels": [baseline, finer],
        "same_objective_configuration": same_objective,
        "relative_objective_change": relative_change,
        "evidence_sha256": _sha256(raw["evidence_sha256"], "mesh convergence evidence"),
    }, accepted


def _branch_guard(value: object, policy: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    raw = _object(
        value,
        {
            "mode",
            "observable_id",
            "baseline_branch_id",
            "finer_branch_id",
            "baseline_mode_order",
            "finer_mode_order",
            "ambiguous",
            "disappeared",
            "evidence_sha256",
        },
        "branch guard evidence",
    )
    if raw["mode"] != policy["mode"] or raw["observable_id"] != policy["observable_id"]:
        raise ValueError("branch guard policy identity changed")
    baseline_branch = _optional_identifier(raw["baseline_branch_id"], "baseline_branch_id")
    finer_branch = _optional_identifier(raw["finer_branch_id"], "finer_branch_id")
    baseline_order = raw["baseline_mode_order"]
    finer_order = raw["finer_mode_order"]
    ambiguous = _boolean(raw["ambiguous"], "branch ambiguous")
    disappeared = _boolean(raw["disappeared"], "branch disappeared")
    evidence_sha = raw["evidence_sha256"]
    if policy["mode"] == "not_applicable":
        if any(
            item is not None
            for item in (baseline_branch, finer_branch, baseline_order, finer_order, evidence_sha)
        ):
            raise ValueError("not-applicable branch evidence must not claim observations")
        accepted = not ambiguous and not disappeared
        normalized_evidence = None
    else:
        if baseline_branch is None or finer_branch is None or evidence_sha is None:
            raise ValueError("required branch evidence is incomplete")
        baseline_order = _integer(baseline_order, "baseline_mode_order")
        finer_order = _integer(finer_order, "finer_mode_order")
        normalized_evidence = _sha256(evidence_sha, "branch evidence")
        accepted = (
            baseline_branch == finer_branch
            and baseline_order == finer_order
            and not ambiguous
            and not disappeared
        )
    return {
        "mode": policy["mode"],
        "observable_id": policy["observable_id"],
        "baseline_branch_id": baseline_branch,
        "finer_branch_id": finer_branch,
        "baseline_mode_order": baseline_order,
        "finer_mode_order": finer_order,
        "ambiguous": ambiguous,
        "disappeared": disappeared,
        "evidence_sha256": normalized_evidence,
    }, accepted


def _off_design(
    value: object,
    *,
    policy: Mapping[str, Any],
    condition_table: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    if not isinstance(value, list) or len(value) > 100_000:
        raise ValueError("off-design evidence rows must be a bounded list")
    active_ids = [
        row["condition_id"]
        for row in condition_table["conditions"]
        if row["active"] and row["objective_role"] == "objective"
    ]
    expected = {
        (condition_id, axis, offset)
        for condition_id in active_ids
        for axis, offsets in (
            ("wavelength_relative", policy["wavelength_relative_offsets"]),
            ("angle_deg", policy["angle_offsets_deg"]),
        )
        for offset in offsets
    }
    normalized = []
    observed = set()
    all_measured = True
    for index, item in enumerate(value):
        raw = _object(
            item,
            {
                "base_condition_id",
                "axis",
                "offset",
                "status",
                "objective_value",
                "evidence_sha256",
            },
            f"off-design row {index}",
        )
        condition_id = _identifier(raw["base_condition_id"], "base_condition_id")
        axis = raw["axis"]
        if axis not in {"wavelength_relative", "angle_deg"}:
            raise ValueError("off-design axis is unsupported")
        offset = _finite(raw["offset"], "off-design offset")
        key = (condition_id, axis, offset)
        if key in observed:
            raise ValueError("off-design evidence rows must be unique")
        observed.add(key)
        status = raw["status"]
        if status not in {"measured", "failed"}:
            raise ValueError("off-design status is unsupported")
        objective = _optional_finite(raw["objective_value"], "off-design objective_value")
        evidence_sha = _sha256(raw["evidence_sha256"], "off-design evidence")
        if status == "measured" and objective is None:
            raise ValueError("measured off-design rows require an objective value")
        if status == "failed" and objective is not None:
            raise ValueError("failed off-design rows must not claim an objective value")
        all_measured = all_measured and status == "measured"
        normalized.append(
            {
                "base_condition_id": condition_id,
                "axis": axis,
                "offset": offset,
                "status": status,
                "objective_value": objective,
                "evidence_sha256": evidence_sha,
            }
        )
    if policy["mode"] == "not_requested":
        accepted = not normalized
    else:
        accepted = observed == expected and all_measured
    normalized.sort(key=lambda row: (row["base_condition_id"], row["axis"], row["offset"]))
    return {
        "expected_row_count": len(expected),
        "observed_row_count": len(normalized),
        "all_measured": all_measured,
        "rows_fingerprint": domain_sha256_v2("comsol_mcp.robust_off_design_rows", normalized),
    }, accepted


def _external_fidelity(value: object, policy: Mapping[str, Any]) -> tuple[str | None, bool]:
    if policy["mode"] == "not_requested":
        if value is not None:
            raise ValueError("not-requested external fidelity must not supply a receipt")
        return None, True
    if value is None:
        return None, False
    receipt = normalize_external_validation_receipt(value)
    kind = receipt["backend"]["kind"]
    if kind == "independent_comsol":
        mode_ok = receipt["fallback"]["mode"] == "primary"
    else:
        mode_ok = receipt["fallback"]["mode"] == "explicit_manual_fallback"
    return receipt["receipt_fingerprint"], mode_ok and receipt["disposition"] == "validated"


def assess_robust_finalist_validation(
    policy: object,
    condition_table: object,
    shape_policy: object,
    evidence: object,
) -> dict[str, Any]:
    """Independently assess whether one finalist has complete promotion evidence."""
    normalized_policy = normalize_robust_finalist_validation_policy(policy)
    conditions = normalize_optimization_condition_table(condition_table)
    shape = normalize_shape_support_policy(shape_policy)
    if (
        normalized_policy["condition_table_fingerprint"]
        != conditions["condition_table_fingerprint"]
    ):
        raise ValueError("finalist evidence condition table identity changed")
    if normalized_policy["shape_policy_fingerprint"] != shape["policy_fingerprint"]:
        raise ValueError("finalist evidence shape policy identity changed")
    bounded = _bounded_json(evidence, "robust finalist evidence", 8 * 1024 * 1024)
    raw = _object(
        bounded,
        {
            "candidate_fingerprint",
            "optimizer_execution_fingerprint",
            "manufacturability",
            "fresh_remesh",
            "mesh_convergence",
            "branch_guard",
            "off_design_rows",
            "external_validation_receipt",
        },
        "robust finalist evidence",
    )
    candidate = _sha256(raw["candidate_fingerprint"], "candidate_fingerprint")
    optimizer = _sha256(raw["optimizer_execution_fingerprint"], "optimizer execution")
    manufacturability, manufacturability_ok = _manufacturability(raw["manufacturability"], shape)
    mesh_policy = normalized_policy["mesh_convergence"]
    fresh_remesh, fresh_remesh_ok = _fresh_remesh(
        raw["fresh_remesh"], candidate=candidate, mesh_policy=mesh_policy
    )
    convergence, convergence_ok = _mesh_convergence(
        raw["mesh_convergence"], candidate=candidate, mesh_policy=mesh_policy
    )
    branch, branch_ok = _branch_guard(raw["branch_guard"], normalized_policy["branch_guard"])
    off_design, off_design_ok = _off_design(
        raw["off_design_rows"],
        policy=normalized_policy["off_design"],
        condition_table=conditions,
    )
    external_fingerprint, external_ok = _external_fidelity(
        raw["external_validation_receipt"], normalized_policy["external_fidelity"]
    )
    checks = {
        "manufacturability": manufacturability_ok,
        "fresh_remesh": fresh_remesh_ok,
        "mesh_convergence": convergence_ok,
        "branch_guard": branch_ok,
        "off_design": off_design_ok,
        "external_fidelity": external_ok,
    }
    reason_codes = [f"{name}_failed" for name in _CHECK_NAMES if not checks[name]]
    body = {
        "schema_name": ROBUST_FINALIST_VALIDATION_RECEIPT_SCHEMA_NAME,
        "schema_version": ROBUST_FINALIST_VALIDATION_RECEIPT_SCHEMA_VERSION,
        "policy_fingerprint": normalized_policy["policy_fingerprint"],
        "condition_table_fingerprint": conditions["condition_table_fingerprint"],
        "shape_policy_fingerprint": shape["policy_fingerprint"],
        "candidate_fingerprint": candidate,
        "optimizer_execution_fingerprint": optimizer,
        "manufacturability": manufacturability,
        "fresh_remesh": fresh_remesh,
        "mesh_convergence": convergence,
        "branch_guard": branch,
        "off_design": off_design,
        "external_validation_receipt_fingerprint": external_fingerprint,
        "checks": checks,
        "accepted": not reason_codes,
        "disposition": "validated" if not reason_codes else "rejected",
        "reason_codes": reason_codes,
    }
    body["receipt_fingerprint"] = domain_sha256_v2(
        ROBUST_FINALIST_VALIDATION_RECEIPT_SCHEMA_NAME, body
    )
    return body


def normalize_robust_finalist_validation_receipt(value: object) -> dict[str, Any]:
    """Validate the exact fingerprint of a produced finalist receipt."""
    bounded = _bounded_json(value, "robust finalist validation receipt", 2 * 1024 * 1024)
    if not isinstance(bounded, dict):
        raise ValueError("robust finalist validation receipt must be an object")
    raw = dict(bounded)
    supplied = raw.pop("receipt_fingerprint", None)
    expected_fields = {
        "schema_name",
        "schema_version",
        "policy_fingerprint",
        "condition_table_fingerprint",
        "shape_policy_fingerprint",
        "candidate_fingerprint",
        "optimizer_execution_fingerprint",
        "manufacturability",
        "fresh_remesh",
        "mesh_convergence",
        "branch_guard",
        "off_design",
        "external_validation_receipt_fingerprint",
        "checks",
        "accepted",
        "disposition",
        "reason_codes",
    }
    if set(raw) != expected_fields:
        raise ValueError("robust finalist validation receipt fields are invalid")
    if (
        raw["schema_name"] != ROBUST_FINALIST_VALIDATION_RECEIPT_SCHEMA_NAME
        or raw["schema_version"] != ROBUST_FINALIST_VALIDATION_RECEIPT_SCHEMA_VERSION
    ):
        raise ValueError("robust finalist validation receipt schema identity is unsupported")
    checks = raw["checks"]
    if (
        not isinstance(checks, dict)
        or set(checks) != set(_CHECK_NAMES)
        or any(not isinstance(checks[name], bool) for name in _CHECK_NAMES)
    ):
        raise ValueError("robust finalist validation receipt checks are invalid")
    accepted = _boolean(raw["accepted"], "accepted")
    reasons = raw["reason_codes"]
    if not isinstance(reasons, list) or reasons != [
        f"{name}_failed" for name in _CHECK_NAMES if not checks[name]
    ]:
        raise ValueError("robust finalist validation receipt reason codes are invalid")
    if accepted != all(checks.values()) or raw["disposition"] != (
        "validated" if accepted else "rejected"
    ):
        raise ValueError("robust finalist validation receipt disposition is invalid")
    for field in (
        "policy_fingerprint",
        "condition_table_fingerprint",
        "shape_policy_fingerprint",
        "candidate_fingerprint",
        "optimizer_execution_fingerprint",
    ):
        raw[field] = _sha256(raw[field], field)
    external = raw["external_validation_receipt_fingerprint"]
    raw["external_validation_receipt_fingerprint"] = (
        None if external is None else _sha256(external, "external validation receipt")
    )
    expected = domain_sha256_v2(ROBUST_FINALIST_VALIDATION_RECEIPT_SCHEMA_NAME, raw)
    if supplied != expected:
        raise ValueError("robust finalist validation receipt fingerprint is invalid")
    return {**raw, "receipt_fingerprint": expected}


__all__ = [
    "ROBUST_FINALIST_VALIDATION_RECEIPT_SCHEMA_NAME",
    "ROBUST_FINALIST_VALIDATION_RECEIPT_SCHEMA_VERSION",
    "assess_robust_finalist_validation",
    "normalize_robust_finalist_validation_receipt",
]
