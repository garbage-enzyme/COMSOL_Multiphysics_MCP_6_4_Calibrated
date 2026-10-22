"""Bounded execution-only requests for one caller-declared native condition."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .condition_adapter import normalize_robust_shape_adapter_configuration
from .condition_table import normalize_optimization_condition_table
from .derivative_support import _bounded_json, _finite, _object, normalize_derivative_support
from .material_tensor_rows import bind_robust_material_tensor_rows
from .shape_support import normalize_shape_support_policy

NATIVE_CONDITION_COLLECTOR = "native_condition_gradient"
NATIVE_FORWARD_COLLECTOR = "native_condition_forward"
NATIVE_CONDITION_COLLECTORS = frozenset({NATIVE_CONDITION_COLLECTOR, NATIVE_FORWARD_COLLECTOR})
NATIVE_CONDITION_REQUEST_SCHEMA_NAME = "comsol_mcp.native_condition_gradient_request"
NATIVE_FORWARD_REQUEST_SCHEMA_NAME = "comsol_mcp.native_condition_forward_request"
NATIVE_CONDITION_REQUEST_SCHEMA_VERSION = "1.0.0"
MAX_NATIVE_CONDITION_REQUEST_BYTES = 64 * 1024


def normalize_native_condition_request(value: object, *, gradients: bool = True) -> dict[str, Any]:
    """Validate one fixed condition without scoring or changing caller controls."""
    bounded = _bounded_json(value, "native condition request", MAX_NATIVE_CONDITION_REQUEST_BYTES)
    supplied = bounded.pop("request_fingerprint", None) if isinstance(bounded, dict) else None
    raw = _object(
        bounded,
        {
            "schema_name",
            "schema_version",
            "support",
            "shape_policy",
            "adapter_configuration",
            "condition_table",
            "initial_values",
        },
        "native condition request",
    )
    schema = (
        NATIVE_CONDITION_REQUEST_SCHEMA_NAME if gradients else NATIVE_FORWARD_REQUEST_SCHEMA_NAME
    )
    if (
        raw["schema_name"] != schema
        or raw["schema_version"] != NATIVE_CONDITION_REQUEST_SCHEMA_VERSION
    ):
        raise ValueError("native condition request schema is unsupported")
    support = normalize_derivative_support(raw["support"])
    shape = normalize_shape_support_policy(raw["shape_policy"])
    adapter = normalize_robust_shape_adapter_configuration(
        raw["adapter_configuration"], support, shape
    )
    if adapter["adapter_id"] != "lin2025_pedot_cylinder_v1":
        raise ValueError("native condition adapter is unsupported")
    table = normalize_optimization_condition_table(raw["condition_table"])
    if len(table["conditions"]) != 1 or table["conditions"][0]["active"] is not True:
        raise ValueError("native collector requires exactly one explicit active condition")
    controls = adapter["configuration"]["condition_controls"]
    if gradients and controls["schema_version"] not in {"1.4.0", "1.5.0"}:
        raise ValueError("native condition requires explicit sensitivity controls")
    if controls["observable_expression"] != support["objective"]["expression"]:
        raise ValueError("native condition observable differs from derivative support")
    if adapter["binding"]["source_sha256"] != support["source_identity"]:
        raise ValueError("native adapter and derivative support source identities differ")
    bind_robust_material_tensor_rows(
        adapter["configuration"]["material_tensor_rows"],
        table,
        expected_temperature_k=adapter["binding"]["temperature_k"],
    )
    values = raw["initial_values"]
    if not isinstance(values, list) or len(values) != len(support["variables"]):
        raise ValueError("native initial values must match the declared variables")
    normalized = [_finite(item, "initial value") for item in values]
    if any(
        not variable["lower"] <= number <= variable["upper"]
        for number, variable in zip(normalized, support["variables"], strict=True)
    ):
        raise ValueError("native initial values exceed the declared bounds")
    body = {
        "schema_name": (
            NATIVE_CONDITION_REQUEST_SCHEMA_NAME
            if gradients
            else NATIVE_FORWARD_REQUEST_SCHEMA_NAME
        ),
        "schema_version": NATIVE_CONDITION_REQUEST_SCHEMA_VERSION,
        "support": support,
        "shape_policy": shape,
        # Preserve the validated input form. The adapter normalizer emits derived fields.
        "adapter_configuration": raw["adapter_configuration"],
        "condition_table": table,
        "initial_values": normalized,
    }
    body["request_fingerprint"] = domain_sha256_v2(schema, body)
    if supplied is not None and supplied != body["request_fingerprint"]:
        raise ValueError("native condition request fingerprint is invalid")
    return dict(_bounded_json(body, "native condition request", MAX_NATIVE_CONDITION_REQUEST_BYTES))


def native_condition_execution_spec(value: object, *, gradients: bool = True) -> dict[str, Any]:
    """Compile the validated native adapter without creating a solver or owner."""
    request = normalize_native_condition_request(value, gradients=gradients)
    return {
        **request,
        "adapter_configuration": normalize_robust_shape_adapter_configuration(
            request["adapter_configuration"], request["support"], request["shape_policy"]
        ),
        "mesh_admission": request["shape_policy"]["mesh_admission"],
    }


def bind_native_condition_point(
    inputs: object,
    point: Mapping[str, Any],
    source_sha256: str,
    *,
    gradients: bool = True,
) -> dict[str, Any]:
    """Reject source, wavelength and incidence drift before model mutation."""
    spec = native_condition_execution_spec(inputs, gradients=gradients)
    if spec["support"]["source_identity"] != source_sha256:
        raise ValueError("native condition source differs from the matrix source")
    condition = spec["condition_table"]["conditions"][0]
    wavelength = point["wavelength"]
    scales = {"m": 1.0, "um": 1e-6, "nm": 1e-9}
    unit = wavelength["unit"]
    controls = spec["adapter_configuration"]["configuration"]["condition_controls"]
    if (
        unit not in scales
        or wavelength["parameter"] != controls["wavelength_parameter"]
        or not math.isclose(
            float(wavelength["value"]) * scales[unit], condition["wavelength_m"], rel_tol=1e-12
        )
    ):
        raise ValueError("native condition wavelength differs from the matrix point")
    incidence = point.get("incidence")
    if not isinstance(incidence, Mapping) or any(
        incidence.get(key) != condition[field]
        for key, field in (
            ("theta_degrees", "incidence_elevation_deg"),
            ("phi_degrees", "incidence_azimuth_deg"),
            ("polarization", "polarization_basis_id"),
        )
    ):
        raise ValueError("native condition incidence differs from the matrix point")
    return spec
