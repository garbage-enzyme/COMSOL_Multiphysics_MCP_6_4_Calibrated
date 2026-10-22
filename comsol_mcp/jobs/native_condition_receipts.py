"""Raw optical condition receipt validation without campaign policy."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

CONDITION_RECEIPT_SCHEMA_NAME = "comsol_mcp.robust_condition_receipt"
CONDITION_RECEIPT_SCHEMA_VERSION = "1.0.0"

DEFAULT_RANGE_EPSILON = 1e-9
DEFAULT_CLOSURE_ABS_TOLERANCE = 1e-4


def evaluate_scientific_gate(
    reflectance: float,
    transmittance: float,
    absorption: float,
    *,
    range_epsilon: float = DEFAULT_RANGE_EPSILON,
    closure_abs_tolerance: float = DEFAULT_CLOSURE_ABS_TOLERANCE,
) -> dict[str, Any]:
    """Evaluate the physical-range and power-closure policy explicitly.

    Finite raw measurements are always kept; this gate classifies them so a
    raw forward value is never silently promoted into a physical acceptance
    decision, and so a tiny negative residual from floating-point noise stays
    a measurement instead of destroying the evidence. The thresholds live in
    one explicit policy contract that callers may tighten or loosen for their
    own campaign; the identity, finiteness and mesh checks remain structural
    and still refuse before any receipt exists.
    """
    range_checks = {
        name: -range_epsilon <= value <= 1.0 + range_epsilon
        for name, value in (
            ("reflectance", reflectance),
            ("transmittance", transmittance),
            ("absorption", absorption),
        )
    }
    closure_residual = reflectance + transmittance + absorption - 1.0
    passed = all(range_checks.values()) and abs(closure_residual) <= closure_abs_tolerance
    return {
        "passed": passed,
        "range_checks": range_checks,
        "range_epsilon": range_epsilon,
        "closure_residual": closure_residual,
        "closure_abs_tolerance": closure_abs_tolerance,
    }


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def condition_tensor_expressions(
    spec: Mapping[str, Any], condition: Mapping[str, Any]
) -> list[str]:
    configuration = spec["adapter_configuration"]["configuration"]
    states = configuration["material_tensor_rows"]["states"]
    state = next(
        (item for item in states if item["state_id"] == condition["material_state_id"]), None
    )
    if state is None:
        raise ValueError("condition material state has no tensor rows")
    wavelength = condition["wavelength_m"]
    sample = next(
        (
            item
            for item in state["rows"]
            if math.isclose(item["wavelength_m"], wavelength, rel_tol=1e-12, abs_tol=1e-18)
        ),
        None,
    )
    if sample is None:
        raise ValueError("condition wavelength has no exact tensor sample")

    def expression(real_key: str, imaginary_key: str) -> str:
        real = _finite(sample[real_key], real_key)
        imaginary = _finite(sample[imaginary_key], imaginary_key)
        return f"({real:.17g})+({imaginary:.17g})*i"

    zero = "0"
    return [
        expression("xx_real", "xx_imag"),
        zero,
        zero,
        zero,
        expression("yy_real", "yy_imag"),
        zero,
        zero,
        zero,
        expression("zz_real", "zz_imag"),
    ]


def _normalize_receipt(
    condition: Mapping[str, Any],
    result: Mapping[str, Any],
    controls: Mapping[str, Any],
    mesh_policy: Mapping[str, Any],
) -> dict[str, Any]:
    required = {
        "condition_id",
        "observable_id",
        "observable_value",
        "requested_wavelength_m",
        "evaluated_wavelength_m",
        "solved_wavelength_m",
        "reflectance",
        "transmittance",
        "absorption",
        "mesh_elements",
        "minimum_mesh_quality",
        "dataset_id",
        "solution_id",
    }
    if set(result) != required:
        raise ValueError("robust condition backend receipt fields are invalid")
    if result["condition_id"] != condition["condition_id"]:
        raise ValueError("robust condition backend changed the condition identity")
    if result["observable_id"] != condition["observable_id"]:
        raise ValueError("robust condition backend changed the observable identity")
    if result["dataset_id"] != controls["dataset_tag"]:
        raise ValueError("robust condition backend changed the dataset identity")
    if result["solution_id"] != controls["solution_tag"]:
        raise ValueError("robust condition backend changed the solution identity")
    requested = _finite(result["requested_wavelength_m"], "requested_wavelength_m")
    evaluated = _finite(result["evaluated_wavelength_m"], "evaluated_wavelength_m")
    solved = _finite(result["solved_wavelength_m"], "solved_wavelength_m")
    if not math.isclose(requested, condition["wavelength_m"], rel_tol=1e-12, abs_tol=1e-18):
        raise ValueError("robust condition backend changed the requested wavelength")
    if not math.isclose(evaluated, requested, rel_tol=1e-9, abs_tol=1e-15) or not math.isclose(
        solved, requested, rel_tol=1e-9, abs_tol=1e-15
    ):
        raise ValueError("robust condition wavelength synchronization failed")
    reflectance = _finite(result["reflectance"], "reflectance")
    transmittance = _finite(result["transmittance"], "transmittance")
    absorption = _finite(result["absorption"], "absorption")
    scientific_gate = evaluate_scientific_gate(reflectance, transmittance, absorption)
    elements = result["mesh_elements"]
    if isinstance(elements, bool) or not isinstance(elements, int) or elements < 1:
        raise ValueError("robust condition mesh element count is invalid")
    if elements > mesh_policy["max_elements_per_model"]:
        raise ValueError("robust condition mesh element cap was exceeded")
    minimum_quality = _finite(result["minimum_mesh_quality"], "minimum_mesh_quality")
    if minimum_quality < mesh_policy["minimum_element_quality"]:
        raise ValueError("robust condition mesh quality is below the caller threshold")
    body = {
        "schema_name": CONDITION_RECEIPT_SCHEMA_NAME,
        "schema_version": CONDITION_RECEIPT_SCHEMA_VERSION,
        "condition_id": condition["condition_id"],
        "condition_order": condition["order"],
        "material_state_id": condition["material_state_id"],
        "polarization_basis_id": condition["polarization_basis_id"],
        "incidence_elevation_deg": condition["incidence_elevation_deg"],
        "incidence_azimuth_deg": condition["incidence_azimuth_deg"],
        "observable_id": condition["observable_id"],
        "observable_value": _finite(result["observable_value"], "observable_value"),
        "requested_wavelength_m": requested,
        "evaluated_wavelength_m": evaluated,
        "solved_wavelength_m": solved,
        "reflectance": reflectance,
        "transmittance": transmittance,
        "absorption": absorption,
        "closure": reflectance + transmittance + absorption,
        "mesh_elements": elements,
        "minimum_mesh_quality": minimum_quality,
        "dataset_id": str(result["dataset_id"]),
        "solution_id": str(result["solution_id"]),
        "scientific_gate": scientific_gate,
        "disposition": "measured" if scientific_gate["passed"] else "measured_gate_failed",
    }
    body["receipt_fingerprint"] = domain_sha256_v2(CONDITION_RECEIPT_SCHEMA_NAME, body)
    return body
