"""Durable per-condition execution shared by licensed robust-shape adapters."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from comsol_mcp.durable import atomic_write_json, domain_sha256_v2

from .robust_shape_rows import append_robust_shape_row, read_robust_shape_rows
from .store import read_json

CONDITION_RECEIPT_SCHEMA_NAME = "comsol_mcp.robust_condition_receipt"
CONDITION_RECEIPT_SCHEMA_VERSION = "1.0.0"


class RobustConditionBackend(Protocol):
    """One already-owned serial backend for exact robust conditions."""

    def evaluate_condition(
        self, condition: Mapping[str, Any], tensor_expressions: list[str]
    ) -> Mapping[str, Any]: ...


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _tensor_expressions(spec: Mapping[str, Any], condition: Mapping[str, Any]) -> list[str]:
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
    if not math.isclose(reflectance + transmittance + absorption, 1.0, abs_tol=1e-4):
        raise ValueError("robust condition power closure failed")
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
        "disposition": "measured",
    }
    body["receipt_fingerprint"] = domain_sha256_v2(CONDITION_RECEIPT_SCHEMA_NAME, body)
    return body


def execute_robust_conditions(
    spec: Mapping[str, Any],
    directory: Path,
    *,
    attempt: int,
    backend: RobustConditionBackend,
    cancel_requested: Callable[[], bool],
) -> list[dict[str, Any]]:
    """Execute or recover every active objective condition without duplicate solves."""
    rows_path = directory / "robust_shape_rows.jsonl"
    existing = read_robust_shape_rows(rows_path, job_fingerprint=spec["spec_fingerprint"])
    completed = {
        row["payload"]["condition_id"]: row
        for row in existing
        if row["kind"] == "condition" and row["payload"]["status"] == "completed"
    }
    observations: list[dict[str, Any]] = []
    active = [
        item
        for item in spec["condition_table"]["conditions"]
        if item["active"] and item["objective_role"] == "objective"
    ]
    execution_limit = spec.get("condition_execution_limit")
    if execution_limit is not None:
        active = active[:execution_limit]
    controls = spec["adapter_configuration"]["configuration"]["condition_controls"]
    mesh_policy = spec["finalist_validation_policy"]["mesh_convergence"]
    for condition in active:
        if cancel_requested():
            raise InterruptedError("robust condition execution was cancelled")
        receipt_path = directory / f"condition-{condition['order']:04d}.json"
        row = completed.get(condition["condition_id"])
        if receipt_path.exists():
            receipt = read_json(receipt_path)
            if (
                receipt.get("schema_name") != CONDITION_RECEIPT_SCHEMA_NAME
                or receipt.get("condition_id") != condition["condition_id"]
                or receipt.get("receipt_fingerprint")
                != domain_sha256_v2(
                    CONDITION_RECEIPT_SCHEMA_NAME,
                    {key: value for key, value in receipt.items() if key != "receipt_fingerprint"},
                )
            ):
                raise ValueError("persisted robust condition receipt is invalid")
        elif row is not None:
            raise ValueError("completed robust condition row lacks its full receipt")
        else:
            result = backend.evaluate_condition(condition, _tensor_expressions(spec, condition))
            receipt = _normalize_receipt(condition, result, controls, mesh_policy)
            atomic_write_json(receipt_path, receipt)
        if row is None:
            row = append_robust_shape_row(
                rows_path,
                job_fingerprint=spec["spec_fingerprint"],
                attempt=attempt,
                kind="condition",
                payload={
                    "iteration_id": "it-0",
                    "condition_id": condition["condition_id"],
                    "condition_order": condition["order"],
                    "status": "completed",
                    "observation_fingerprint": receipt["receipt_fingerprint"],
                    "objective_contribution": receipt["observable_value"],
                    "reason_code": "licensed_condition_measured",
                },
            )
        elif row["payload"]["observation_fingerprint"] != receipt["receipt_fingerprint"]:
            raise ValueError("robust condition row differs from its full receipt")
        observations.append(
            {
                "condition_id": condition["condition_id"],
                "observable_id": condition["observable_id"],
                "value": receipt["observable_value"],
                "evidence_sha256": receipt["receipt_fingerprint"],
                "disposition": "measured",
            }
        )
    return observations


__all__ = [
    "CONDITION_RECEIPT_SCHEMA_NAME",
    "CONDITION_RECEIPT_SCHEMA_VERSION",
    "RobustConditionBackend",
    "execute_robust_conditions",
]
