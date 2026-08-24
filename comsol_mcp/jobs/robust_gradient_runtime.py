"""Durable condition-level native gradients and exact robust aggregation."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from comsol_mcp.durable import atomic_write_json, domain_sha256_v2
from comsol_mcp.research.robust_objectives import (
    aggregate_robust_absolute_contrast_gradient,
)

from .robust_condition_runtime import condition_tensor_expressions
from .store import read_json

CONDITION_GRADIENT_RECEIPT_SCHEMA_NAME = "comsol_mcp.robust_condition_gradient_receipt"
CONDITION_GRADIENT_RECEIPT_SCHEMA_VERSION = "1.0.0"
AGGREGATE_GRADIENT_ARTIFACT_NAME = "native-aggregate-gradient.json"


class RobustConditionGradientBackend(Protocol):
    """One already-owned serial backend for exact native condition gradients."""

    def evaluate_condition_gradient(
        self,
        condition: Mapping[str, Any],
        tensor_expressions: list[str],
        variable_ids: list[str],
    ) -> Mapping[str, Any]: ...


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value.casefold())
    ):
        raise ValueError(f"{name} must be a SHA-256 digest")
    return value.casefold()


def _active_conditions(spec: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        condition
        for condition in spec["condition_table"]["conditions"]
        if condition["active"] and condition["objective_role"] == "objective"
    ]


def _normalize_backend_result(
    spec: Mapping[str, Any],
    condition: Mapping[str, Any],
    result: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> dict[str, Any]:
    fields = {
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
        "derivative_dataset_id",
        "derivative_solution_id",
        "variable_ids",
        "raw_gradients",
        "accepted_real_gradients",
        "gradient_unit",
        "identity_fingerprints",
    }
    raw = dict(result)
    if set(raw) != fields:
        raise ValueError("native condition gradient backend fields are invalid")
    if raw["condition_id"] != condition["condition_id"]:
        raise ValueError("native condition gradient changed the condition identity")
    if raw["observable_id"] != condition["observable_id"]:
        raise ValueError("native condition gradient changed the observable identity")
    variable_ids = [item["variable_id"] for item in spec["support"]["variables"]]
    if raw["variable_ids"] != variable_ids:
        raise ValueError("native condition gradient variable order changed")
    raw_gradients = raw["raw_gradients"]
    accepted = raw["accepted_real_gradients"]
    if (
        not isinstance(raw_gradients, list)
        or len(raw_gradients) != len(variable_ids)
        or not isinstance(accepted, list)
        or len(accepted) != len(variable_ids)
    ):
        raise ValueError("native condition gradient vector shape is invalid")
    normalized_raw: list[dict[str, float]] = []
    normalized_accepted: list[float] = []
    for index, (native, accepted_value) in enumerate(zip(raw_gradients, accepted, strict=True)):
        if not isinstance(native, Mapping) or set(native) != {"real", "imaginary"}:
            raise ValueError("native condition raw gradient fields are invalid")
        real = _finite(native["real"], f"raw_gradients[{index}].real")
        imaginary = _finite(native["imaginary"], f"raw_gradients[{index}].imaginary")
        real_accepted = _finite(accepted_value, f"accepted_real_gradients[{index}]")
        # The accepted value may be recomputed through a different arithmetic
        # path; compare within the module's evidence tolerance instead of
        # requiring exact bit equality.
        if not math.isclose(real_accepted, real, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("accepted native gradient component differs from the raw real part")
        normalized_raw.append({"real": real, "imaginary": imaginary})
        normalized_accepted.append(real_accepted)
    expected_unit = spec["support"]["result_identity"]["derivative_units"]
    if raw["gradient_unit"] != expected_unit:
        raise ValueError("native condition gradient unit changed")
    requested = _finite(raw["requested_wavelength_m"], "requested_wavelength_m")
    evaluated = _finite(raw["evaluated_wavelength_m"], "evaluated_wavelength_m")
    solved = _finite(raw["solved_wavelength_m"], "solved_wavelength_m")
    if not math.isclose(requested, condition["wavelength_m"], rel_tol=1e-12, abs_tol=1e-18):
        raise ValueError("native condition gradient requested wavelength changed")
    if not math.isclose(evaluated, requested, rel_tol=1e-9, abs_tol=1e-15) or not math.isclose(
        solved, requested, rel_tol=1e-9, abs_tol=1e-15
    ):
        raise ValueError("native condition gradient wavelength synchronization failed")
    objective_value = _finite(raw["observable_value"], "observable_value")
    if not math.isclose(
        objective_value,
        _finite(observation["value"], "baseline observation value"),
        rel_tol=1e-6,
        abs_tol=1e-9,
    ):
        raise ValueError("native condition gradient objective differs from baseline evidence")
    reflectance = _finite(raw["reflectance"], "reflectance")
    transmittance = _finite(raw["transmittance"], "transmittance")
    absorption = _finite(raw["absorption"], "absorption")
    for name, value in (
        ("reflectance", reflectance),
        ("transmittance", transmittance),
        ("absorption", absorption),
    ):
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"native condition gradient {name} must be within [0, 1]")
    closure = reflectance + transmittance + absorption
    if not math.isclose(closure, 1.0, rel_tol=0.0, abs_tol=1e-4):
        raise ValueError("native condition gradient power closure failed")
    elements = raw["mesh_elements"]
    if isinstance(elements, bool) or not isinstance(elements, int) or elements < 1:
        raise ValueError("native condition gradient mesh element count is invalid")
    mesh_policy = spec["finalist_validation_policy"]["mesh_convergence"]
    if elements > mesh_policy["max_elements_per_model"]:
        raise ValueError("native condition gradient mesh element cap was exceeded")
    minimum_quality = _finite(raw["minimum_mesh_quality"], "minimum_mesh_quality")
    if minimum_quality < mesh_policy["minimum_element_quality"]:
        raise ValueError("native condition gradient mesh quality is below the caller threshold")
    identities = raw["identity_fingerprints"]
    identity_fields = {"primal", "adjoint", "study", "solution", "dataset"}
    if not isinstance(identities, Mapping) or set(identities) != identity_fields:
        raise ValueError("native condition gradient identity fields are invalid")
    normalized_identities = {
        key: _digest(identities[key], f"identity_fingerprints.{key}")
        for key in sorted(identity_fields)
    }
    controls = spec["adapter_configuration"]["configuration"]["condition_controls"]
    if raw["dataset_id"] != controls["dataset_tag"]:
        raise ValueError("native condition gradient changed the forward dataset identity")
    if raw["solution_id"] != controls["solution_tag"]:
        raise ValueError("native condition gradient changed the forward solution identity")
    for name in ("derivative_dataset_id", "derivative_solution_id"):
        if not isinstance(raw[name], str) or not raw[name] or len(raw[name]) > 128:
            raise ValueError(f"native condition gradient {name} is invalid")
    body = {
        "schema_name": CONDITION_GRADIENT_RECEIPT_SCHEMA_NAME,
        "schema_version": CONDITION_GRADIENT_RECEIPT_SCHEMA_VERSION,
        "condition_id": condition["condition_id"],
        "condition_order": condition["order"],
        "observable_id": condition["observable_id"],
        "observable_value": objective_value,
        "baseline_observation_fingerprint": _digest(
            observation["evidence_sha256"], "baseline observation fingerprint"
        ),
        "requested_wavelength_m": requested,
        "evaluated_wavelength_m": evaluated,
        "solved_wavelength_m": solved,
        "reflectance": reflectance,
        "transmittance": transmittance,
        "absorption": absorption,
        "closure": closure,
        "mesh_elements": elements,
        "minimum_mesh_quality": minimum_quality,
        "dataset_id": raw["dataset_id"],
        "solution_id": raw["solution_id"],
        "derivative_dataset_id": raw["derivative_dataset_id"],
        "derivative_solution_id": raw["derivative_solution_id"],
        "variable_ids": variable_ids,
        "raw_gradients": normalized_raw,
        "accepted_real_gradients": normalized_accepted,
        "gradient_unit": expected_unit,
        "identity_fingerprints": normalized_identities,
        "disposition": "measured",
    }
    return {
        **body,
        "receipt_fingerprint": domain_sha256_v2(CONDITION_GRADIENT_RECEIPT_SCHEMA_NAME, body),
    }


def _validate_persisted_receipt(
    receipt: object,
    *,
    spec: Mapping[str, Any],
    condition: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(receipt, Mapping):
        raise ValueError("persisted native condition gradient receipt is invalid")
    normalized = dict(receipt)
    supplied = normalized.pop("receipt_fingerprint", None)
    required = {
        "schema_name",
        "schema_version",
        "condition_id",
        "condition_order",
        "observable_id",
        "observable_value",
        "baseline_observation_fingerprint",
        "requested_wavelength_m",
        "evaluated_wavelength_m",
        "solved_wavelength_m",
        "reflectance",
        "transmittance",
        "absorption",
        "closure",
        "mesh_elements",
        "minimum_mesh_quality",
        "dataset_id",
        "solution_id",
        "derivative_dataset_id",
        "derivative_solution_id",
        "variable_ids",
        "raw_gradients",
        "accepted_real_gradients",
        "gradient_unit",
        "identity_fingerprints",
        "disposition",
    }
    expected = domain_sha256_v2(CONDITION_GRADIENT_RECEIPT_SCHEMA_NAME, normalized)
    if (
        set(normalized) != required
        or normalized.get("schema_name") != CONDITION_GRADIENT_RECEIPT_SCHEMA_NAME
        or normalized.get("schema_version") != CONDITION_GRADIENT_RECEIPT_SCHEMA_VERSION
        or normalized.get("condition_id") != condition["condition_id"]
        or normalized.get("condition_order") != condition["order"]
        or normalized.get("disposition") != "measured"
        or supplied != expected
    ):
        raise ValueError("persisted native condition gradient receipt is invalid")
    regenerated = _normalize_backend_result(
        spec,
        condition,
        {
            key: normalized[key]
            for key in (
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
                "derivative_dataset_id",
                "derivative_solution_id",
                "variable_ids",
                "raw_gradients",
                "accepted_real_gradients",
                "gradient_unit",
                "identity_fingerprints",
            )
        },
        observation,
    )
    persisted = {**normalized, "receipt_fingerprint": expected}
    if regenerated != persisted:
        raise ValueError("persisted native condition gradient receipt is invalid")
    return persisted


def execute_native_condition_gradients(
    spec: Mapping[str, Any],
    directory: Path,
    *,
    backend: RobustConditionGradientBackend,
    observations: list[dict[str, Any]],
    cancel_requested: Callable[[], bool],
) -> dict[str, Any]:
    """Execute, recover, and aggregate the complete ordered native gradient set."""
    conditions = _active_conditions(spec)
    if spec.get("condition_execution_limit") is not None:
        raise ValueError("native aggregate gradients require the complete condition table")
    observation_by_id = {item["condition_id"]: item for item in observations}
    if len(observation_by_id) != len(observations):
        raise ValueError("native aggregate gradients require unique baseline observations")
    if set(observation_by_id) != {item["condition_id"] for item in conditions}:
        raise ValueError("native aggregate gradients require complete baseline observations")
    variable_ids = [item["variable_id"] for item in spec["support"]["variables"]]
    condition_gradients: list[dict[str, Any]] = []
    for condition in conditions:
        if cancel_requested():
            raise InterruptedError("native condition gradient execution was cancelled")
        path = directory / f"condition-gradient-{condition['order']:04d}.json"
        if path.exists():
            receipt = _validate_persisted_receipt(
                read_json(path),
                spec=spec,
                condition=condition,
                observation=observation_by_id[condition["condition_id"]],
            )
        else:
            result = backend.evaluate_condition_gradient(
                condition,
                condition_tensor_expressions(spec, condition),
                variable_ids,
            )
            receipt = _normalize_backend_result(
                spec,
                condition,
                result,
                observation_by_id[condition["condition_id"]],
            )
            atomic_write_json(path, receipt)
        condition_gradients.append(
            {
                "condition_id": condition["condition_id"],
                "variable_ids": variable_ids,
                "values": receipt["accepted_real_gradients"],
                "evidence_sha256": receipt["receipt_fingerprint"],
                "disposition": "measured",
            }
        )
    aggregate = aggregate_robust_absolute_contrast_gradient(
        spec["objective"],
        spec["condition_table"],
        observations,
        condition_gradients,
    )
    atomic_write_json(directory / AGGREGATE_GRADIENT_ARTIFACT_NAME, aggregate)
    return aggregate


__all__ = [
    "AGGREGATE_GRADIENT_ARTIFACT_NAME",
    "CONDITION_GRADIENT_RECEIPT_SCHEMA_NAME",
    "CONDITION_GRADIENT_RECEIPT_SCHEMA_VERSION",
    "RobustConditionGradientBackend",
    "execute_native_condition_gradients",
]
