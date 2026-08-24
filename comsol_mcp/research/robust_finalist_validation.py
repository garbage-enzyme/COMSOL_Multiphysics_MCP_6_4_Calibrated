"""Caller-owned finalist robustness validation policy contracts."""

from __future__ import annotations

from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .derivative_support import _bounded_json, _finite, _identifier, _object, _sha256

ROBUST_FINALIST_VALIDATION_POLICY_SCHEMA_NAME = "comsol_mcp.robust_finalist_validation_policy"
ROBUST_FINALIST_VALIDATION_POLICY_SCHEMA_VERSION = "1.1.0"
ROBUST_FINALIST_VALIDATION_POLICY_READABLE_VERSIONS = ("1.0.0", "1.1.0")

_BRANCH_MODES = {"required", "not_applicable"}
_OFF_DESIGN_MODES = {"required", "not_requested"}
_EXTERNAL_MODES = {"required", "not_requested"}


def _optional_identifier(value: object, name: str) -> str | None:
    return None if value is None else _identifier(value, name)


def _bounded_text(value: object, name: str, *, maximum: int = 64) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be bounded nonempty text")
    text = value.strip()
    # The bound applies to the canonical stripped text so caller-added
    # indentation cannot push an otherwise valid value over the limit.
    if len(text) > maximum:
        raise ValueError(f"{name} must be bounded nonempty text")
    if any(character in text for character in ("\r", "\n", "\x00")):
        raise ValueError(f"{name} contains a forbidden character")
    return text


def _bounded_integer(value: object, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in the allowed range")
    return value


def _offsets(
    value: object,
    name: str,
    *,
    maximum_items: int,
    maximum_absolute: float,
) -> list[float]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise ValueError(f"{name} must be a bounded list")
    result = sorted({_finite(item, name) for item in value})
    if len(result) != len(value):
        raise ValueError(f"{name} must contain unique values")
    if any(item == 0.0 or abs(item) > maximum_absolute for item in result):
        raise ValueError(f"{name} values are outside the allowed nonzero range")
    return result


def normalize_robust_finalist_validation_policy(value: object) -> dict[str, Any]:
    """Normalize explicit remesh, convergence, branch, off-design, and fidelity policy."""
    bounded = _bounded_json(value, "robust finalist validation policy", 256 * 1024)
    supplied = None
    if isinstance(bounded, dict) and "policy_fingerprint" in bounded:
        supplied = bounded.pop("policy_fingerprint")
        if supplied is None:
            raise ValueError("robust finalist validation policy fingerprint is invalid")
    raw = _object(
        bounded,
        {
            "schema_name",
            "schema_version",
            "policy_id",
            "condition_table_fingerprint",
            "shape_policy_fingerprint",
            "fresh_remesh",
            "mesh_convergence",
            "branch_guard",
            "off_design",
            "external_fidelity",
        },
        "robust finalist validation policy",
    )
    schema_version = raw["schema_version"]
    if (
        raw["schema_name"] != ROBUST_FINALIST_VALIDATION_POLICY_SCHEMA_NAME
        or schema_version not in ROBUST_FINALIST_VALIDATION_POLICY_READABLE_VERSIONS
    ):
        raise ValueError("robust finalist validation policy schema identity is unsupported")

    remesh = _object(
        raw["fresh_remesh"],
        {"required", "explicit_rebuild", "independent_from_optimizer_state"},
        "fresh_remesh",
    )
    if any(remesh[field] is not True for field in remesh):
        raise ValueError("finalist validation requires an independent explicit fresh remesh")

    mesh_fields = {
        "baseline_level_id",
        "finer_level_id",
        "max_relative_objective_change",
        "max_elements_per_model",
        "minimum_element_quality",
        "quality_measure",
    }
    if schema_version == "1.1.0":
        mesh_fields |= {"baseline_mesh_reference_value", "finer_mesh_reference_value"}
    mesh = _object(
        raw["mesh_convergence"],
        mesh_fields,
        "mesh_convergence",
    )
    baseline_level = _identifier(mesh["baseline_level_id"], "baseline_level_id")
    finer_level = _identifier(mesh["finer_level_id"], "finer_level_id")
    if baseline_level == finer_level:
        raise ValueError("mesh convergence levels must be distinct")
    baseline_mesh_reference = None
    finer_mesh_reference = None
    if schema_version == "1.1.0":
        baseline_mesh_reference = _bounded_text(
            mesh["baseline_mesh_reference_value"],
            "mesh_convergence.baseline_mesh_reference_value",
        )
        finer_mesh_reference = _bounded_text(
            mesh["finer_mesh_reference_value"],
            "mesh_convergence.finer_mesh_reference_value",
        )
        if baseline_mesh_reference == finer_mesh_reference:
            raise ValueError("baseline and finer mesh reference values must be distinct")
    maximum_change = _finite(
        mesh["max_relative_objective_change"],
        "mesh_convergence.max_relative_objective_change",
        positive=True,
    )
    if maximum_change > 1.0:
        raise ValueError("mesh convergence relative-change limit must not exceed one")
    minimum_quality = _finite(
        mesh["minimum_element_quality"],
        "mesh_convergence.minimum_element_quality",
        positive=True,
    )
    if minimum_quality > 1.0:
        raise ValueError("mesh convergence minimum quality must not exceed one")

    branch = _object(
        raw["branch_guard"],
        {
            "mode",
            "observable_id",
            "require_same_branch_identity",
            "require_same_mode_order",
            "ambiguity_disposition",
            "disappearance_disposition",
        },
        "branch_guard",
    )
    branch_mode = branch["mode"]
    if branch_mode not in _BRANCH_MODES:
        raise ValueError("branch_guard.mode is unsupported")
    observable_id = _optional_identifier(branch["observable_id"], "branch_guard.observable_id")
    if branch_mode == "required":
        if (
            observable_id is None
            or branch["require_same_branch_identity"] is not True
            or branch["require_same_mode_order"] is not True
            or branch["ambiguity_disposition"] != "reject"
            or branch["disappearance_disposition"] != "reject"
        ):
            raise ValueError("required branch guard must fail closed on identity or order drift")
    elif (
        observable_id is not None
        or branch["require_same_branch_identity"] is not False
        or branch["require_same_mode_order"] is not False
        or branch["ambiguity_disposition"] != "not_applicable"
        or branch["disappearance_disposition"] != "not_applicable"
    ):
        raise ValueError("not-applicable branch guard must not claim branch evidence")

    off_design = _object(
        raw["off_design"],
        {
            "mode",
            "validation_only",
            "include_in_optimizer",
            "wavelength_relative_offsets",
            "angle_offsets_deg",
        },
        "off_design",
    )
    off_design_mode = off_design["mode"]
    if off_design_mode not in _OFF_DESIGN_MODES:
        raise ValueError("off_design.mode is unsupported")
    wavelength_offsets = _offsets(
        off_design["wavelength_relative_offsets"],
        "off_design.wavelength_relative_offsets",
        maximum_items=16,
        maximum_absolute=0.5,
    )
    angle_offsets = _offsets(
        off_design["angle_offsets_deg"],
        "off_design.angle_offsets_deg",
        maximum_items=16,
        maximum_absolute=90.0,
    )
    if off_design_mode == "required":
        if (
            not wavelength_offsets
            or not angle_offsets
            or off_design["validation_only"] is not True
            or off_design["include_in_optimizer"] is not False
        ):
            raise ValueError("required off-design points must be validation-only and explicit")
    elif (
        wavelength_offsets
        or angle_offsets
        or off_design["validation_only"] is not False
        or off_design["include_in_optimizer"] is not False
    ):
        raise ValueError("not-requested off-design validation must not declare sampling")

    external_fields = {"mode", "primary_backend", "fallback_mode", "automatic_fallback"}
    if schema_version == "1.1.0":
        external_fields.add("maximum_absolute_condition_delta")
    external = _object(
        raw["external_fidelity"],
        external_fields,
        "external_fidelity",
    )
    external_mode = external["mode"]
    if external_mode not in _EXTERNAL_MODES:
        raise ValueError("external_fidelity.mode is unsupported")
    primary_backend = external["primary_backend"]
    fallback_mode = external["fallback_mode"]
    if external["automatic_fallback"] is not False:
        raise ValueError("external fidelity validation forbids automatic fallback")
    maximum_external_delta = None
    if schema_version == "1.1.0":
        maximum_external_delta = _finite(
            external["maximum_absolute_condition_delta"],
            "external_fidelity.maximum_absolute_condition_delta",
            positive=True,
        )
    if external_mode == "required":
        if primary_backend != "independent_comsol" or fallback_mode != "explicit_manual_rcwa":
            raise ValueError("required external fidelity must use COMSOL first and manual RCWA")
    elif primary_backend is not None or fallback_mode != "not_requested":
        raise ValueError("not-requested external fidelity must not declare a backend")

    body = {
        "schema_name": ROBUST_FINALIST_VALIDATION_POLICY_SCHEMA_NAME,
        "schema_version": schema_version,
        "policy_id": _identifier(raw["policy_id"], "policy_id"),
        "condition_table_fingerprint": _sha256(
            raw["condition_table_fingerprint"], "condition_table_fingerprint"
        ),
        "shape_policy_fingerprint": _sha256(
            raw["shape_policy_fingerprint"], "shape_policy_fingerprint"
        ),
        "fresh_remesh": {
            "required": True,
            "explicit_rebuild": True,
            "independent_from_optimizer_state": True,
        },
        "mesh_convergence": {
            "baseline_level_id": baseline_level,
            "finer_level_id": finer_level,
            "max_relative_objective_change": maximum_change,
            "max_elements_per_model": _bounded_integer(
                mesh["max_elements_per_model"],
                "mesh_convergence.max_elements_per_model",
                minimum=1,
                maximum=1_000_000_000,
            ),
            "minimum_element_quality": minimum_quality,
            "quality_measure": _identifier(mesh["quality_measure"], "quality_measure"),
        },
        "branch_guard": {
            "mode": branch_mode,
            "observable_id": observable_id,
            "require_same_branch_identity": branch["require_same_branch_identity"],
            "require_same_mode_order": branch["require_same_mode_order"],
            "ambiguity_disposition": branch["ambiguity_disposition"],
            "disappearance_disposition": branch["disappearance_disposition"],
        },
        "off_design": {
            "mode": off_design_mode,
            "validation_only": off_design["validation_only"],
            "include_in_optimizer": False,
            "wavelength_relative_offsets": wavelength_offsets,
            "angle_offsets_deg": angle_offsets,
        },
        "external_fidelity": {
            "mode": external_mode,
            "primary_backend": primary_backend,
            "fallback_mode": fallback_mode,
            "automatic_fallback": False,
        },
    }
    if schema_version == "1.1.0":
        body["mesh_convergence"].update(
            {
                "baseline_mesh_reference_value": baseline_mesh_reference,
                "finer_mesh_reference_value": finer_mesh_reference,
            }
        )
        body["external_fidelity"]["maximum_absolute_condition_delta"] = maximum_external_delta
    body["policy_fingerprint"] = domain_sha256_v2(
        ROBUST_FINALIST_VALIDATION_POLICY_SCHEMA_NAME, body
    )
    if supplied is not None and supplied != body["policy_fingerprint"]:
        raise ValueError("robust finalist validation policy fingerprint is invalid")
    return body


__all__ = [
    "ROBUST_FINALIST_VALIDATION_POLICY_SCHEMA_NAME",
    "ROBUST_FINALIST_VALIDATION_POLICY_READABLE_VERSIONS",
    "ROBUST_FINALIST_VALIDATION_POLICY_SCHEMA_VERSION",
    "normalize_robust_finalist_validation_policy",
]
