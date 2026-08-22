"""Canonical gradient evidence and native optimizer configuration contracts."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .derivative_support import (
    _bounded_json,
    _finite,
    _identifier,
    _object,
    _sha256,
    _text,
    normalize_derivative_support,
)

GRADIENT_RECORD_SCHEMA_NAME = "comsol_mcp.gradient_record"
GRADIENT_RECORD_SCHEMA_VERSION = "1.0.0"
NATIVE_OPTIMIZER_SCHEMA_NAME = "comsol_mcp.native_optimizer_configuration"
NATIVE_OPTIMIZER_SCHEMA_VERSION = "1.1.0"
NATIVE_OPTIMIZER_LEGACY_SCHEMA_VERSION = "1.0.0"
_OPTIMIZER_METHODS = {"gcmma", "mma", "ipopt"}
_EVIDENCE_STATES = {"native_unchecked", "gradient_validated", "restricted", "rejected"}


def _finite_vector(value: object, name: str, *, length: int) -> list[float]:
    if not isinstance(value, list) or len(value) != length:
        raise ValueError(f"{name} must match the canonical variable count")
    return [_finite(item, f"{name}[{index}]") for index, item in enumerate(value)]


def _identifier_vector(value: object, name: str, *, length: int) -> list[str]:
    if not isinstance(value, list) or len(value) != length:
        raise ValueError(f"{name} must match the canonical variable count")
    normalized = [_identifier(item, f"{name}[{index}]") for index, item in enumerate(value)]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{name} must be unique")
    return normalized


def _bool_vector(value: object, name: str, *, length: int) -> list[bool]:
    if (
        not isinstance(value, list)
        or len(value) != length
        or any(not isinstance(item, bool) for item in value)
    ):
        raise ValueError(f"{name} must be a boolean vector matching the variable count")
    return list(value)


def _constraint_values(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError("constraint_values must be an object with string keys")
    if len(value) > 16:
        raise ValueError("constraint_values exceeds the bounded constraint count")
    return {
        _identifier(key, "constraint_values key"): _finite(item, f"constraint_values.{key}")
        for key, item in sorted(value.items())
    }


def normalize_gradient_record(value: object, support: object) -> dict[str, Any]:
    """Normalize one immutable native derivative row against exact support."""
    normalized_support = normalize_derivative_support(support)
    bounded = _bounded_json(value, "gradient record", 256 * 1024)
    supplied = None
    if isinstance(bounded, dict) and "gradient_fingerprint" in bounded:
        supplied = bounded.pop("gradient_fingerprint")
    raw = _object(
        bounded,
        {
            "schema_name",
            "schema_version",
            "support_fingerprint",
            "candidate_fingerprint",
            "variable_order",
            "physical_values",
            "objective_value",
            "constraint_values",
            "native_gradient",
            "native_units",
            "objective_sign",
            "transform_jacobian",
            "optimizer_gradient",
            "active_bounds",
            "projected_gradient",
            "method",
            "identities",
            "suspect_components",
            "evidence_state",
        },
        "gradient record",
    )
    if (
        raw["schema_name"] != GRADIENT_RECORD_SCHEMA_NAME
        or raw["schema_version"] != GRADIENT_RECORD_SCHEMA_VERSION
    ):
        raise ValueError("gradient record schema identity is unsupported")
    if raw["support_fingerprint"] != normalized_support["support_fingerprint"]:
        raise ValueError("gradient record support fingerprint changed")
    count = len(normalized_support["variables"])
    expected_order = [item["variable_id"] for item in normalized_support["variables"]]
    variable_order = _identifier_vector(raw["variable_order"], "variable_order", length=count)
    if variable_order != expected_order:
        raise ValueError("gradient variable order differs from derivative support")
    native_units = raw["native_units"]
    if not isinstance(native_units, list) or len(native_units) != count:
        raise ValueError("native_units must match the canonical variable count")
    native_units = [_text(item, "native_units", maximum=64) for item in native_units]
    objective_sign = _finite(raw["objective_sign"], "objective_sign")
    if objective_sign not in {-1.0, 1.0}:
        raise ValueError("objective_sign must be exactly -1 or 1")
    active_bounds = _bool_vector(raw["active_bounds"], "active_bounds", length=count)
    suspect_components = raw["suspect_components"]
    if not isinstance(suspect_components, list) or len(suspect_components) > count:
        raise ValueError("suspect_components must be a bounded list")
    suspect_components = sorted(
        {_identifier(item, "suspect_components") for item in suspect_components}
    )
    if not set(suspect_components).issubset(expected_order):
        raise ValueError("suspect_components must reference the canonical variable order")
    constraint_values = _constraint_values(raw["constraint_values"])
    allowed_constraint_ids = {item["constraint_id"] for item in normalized_support["constraints"]}
    unknown_constraints = sorted(set(constraint_values) - allowed_constraint_ids)
    if unknown_constraints:
        raise ValueError(
            "constraint_values must reference derivative-support constraint ids; "
            f"unknown={unknown_constraints}"
        )
    method = raw["method"]
    if method != normalized_support["derivative_method"]:
        raise ValueError("gradient method differs from derivative support")
    identities = _object(
        raw["identities"],
        {"primal", "adjoint", "mesh", "study", "solution", "dataset"},
        "identities",
    )
    normalized_identities = {
        key: _sha256(item, f"identities.{key}") for key, item in identities.items()
    }
    evidence_state = raw["evidence_state"]
    if evidence_state not in _EVIDENCE_STATES:
        raise ValueError("evidence_state is unsupported")
    body = {
        "schema_name": GRADIENT_RECORD_SCHEMA_NAME,
        "schema_version": GRADIENT_RECORD_SCHEMA_VERSION,
        "support_fingerprint": normalized_support["support_fingerprint"],
        "candidate_fingerprint": _sha256(raw["candidate_fingerprint"], "candidate_fingerprint"),
        "variable_order": variable_order,
        "physical_values": _finite_vector(raw["physical_values"], "physical_values", length=count),
        "objective_value": _finite(raw["objective_value"], "objective_value"),
        "constraint_values": constraint_values,
        "native_gradient": _finite_vector(raw["native_gradient"], "native_gradient", length=count),
        "native_units": native_units,
        "objective_sign": objective_sign,
        "transform_jacobian": _finite_vector(
            raw["transform_jacobian"], "transform_jacobian", length=count
        ),
        "optimizer_gradient": _finite_vector(
            raw["optimizer_gradient"], "optimizer_gradient", length=count
        ),
        "active_bounds": active_bounds,
        "projected_gradient": _finite_vector(
            raw["projected_gradient"], "projected_gradient", length=count
        ),
        "method": method,
        "identities": normalized_identities,
        "suspect_components": suspect_components,
        "evidence_state": evidence_state,
    }
    body["gradient_fingerprint"] = domain_sha256_v2(GRADIENT_RECORD_SCHEMA_NAME, body)
    if supplied is not None and supplied != body["gradient_fingerprint"]:
        raise ValueError("gradient record fingerprint is invalid")
    return body


def normalize_native_optimizer_configuration(value: object) -> dict[str, Any]:
    """Normalize a caller-budgeted condition solver and optimizer configuration."""
    bounded = _bounded_json(value, "native optimizer configuration", 128 * 1024)
    supplied = None
    if isinstance(bounded, dict) and "optimizer_fingerprint" in bounded:
        supplied = bounded.pop("optimizer_fingerprint")
    if not isinstance(bounded, dict):
        raise ValueError("native optimizer configuration must be an object")
    schema_version = bounded.get("schema_version")
    fields = {
        "schema_name",
        "schema_version",
        "optimizer_id",
        "backend",
        "method",
        "move_limit",
        "optimality_tolerance",
        "constraint_tolerance",
        "budget",
        "checkpoint_policy",
        "deterministic_seed",
    }
    if schema_version == NATIVE_OPTIMIZER_SCHEMA_VERSION:
        fields.add("backend_configuration")
    raw = _object(
        bounded,
        fields,
        "native optimizer configuration",
    )
    if raw["schema_name"] != NATIVE_OPTIMIZER_SCHEMA_NAME or raw["schema_version"] not in {
        NATIVE_OPTIMIZER_LEGACY_SCHEMA_VERSION,
        NATIVE_OPTIMIZER_SCHEMA_VERSION,
    }:
        raise ValueError("native optimizer schema identity is unsupported")
    if schema_version == NATIVE_OPTIMIZER_LEGACY_SCHEMA_VERSION:
        if raw["backend"] != "comsol_native":
            raise ValueError("legacy native optimizer backend must be comsol_native")
        backend_configuration = None
    else:
        if raw["backend"] != "mmapy_outer_comsol_conditions":
            raise ValueError("robust optimizer backend must be mmapy_outer_comsol_conditions")
        backend_raw = _object(
            raw["backend_configuration"],
            {
                "condition_solver_backend",
                "package_name",
                "package_version",
                "distribution_license",
                "distribution_sha256",
                "distribution_path",
                "objective_direction",
                "max_inner_iterations",
            },
            "backend_configuration",
        )
        if backend_raw["condition_solver_backend"] != "comsol_native":
            raise ValueError("robust condition solver backend must be comsol_native")
        if backend_raw["package_name"] != "mmapy" or backend_raw["package_version"] != "0.3.1":
            raise ValueError("robust optimizer requires the reviewed mmapy 0.3.1 backend")
        if backend_raw["distribution_license"] != "GPL-3.0-or-later":
            raise ValueError("robust optimizer mmapy license identity differs")
        if backend_raw["objective_direction"] != "maximize":
            raise ValueError("robust optimizer objective direction must be maximize")
        inner = backend_raw["max_inner_iterations"]
        if isinstance(inner, bool) or not isinstance(inner, int) or not 1 <= inner <= 100:
            raise ValueError("backend max_inner_iterations must be in [1, 100]")
        distribution_path = backend_raw["distribution_path"]
        if (
            not isinstance(distribution_path, str)
            or not distribution_path.isascii()
            or not Path(distribution_path).is_absolute()
            or Path(distribution_path).suffix.casefold() != ".whl"
        ):
            raise ValueError("backend distribution_path must be an absolute ASCII wheel path")
        backend_configuration = {
            "condition_solver_backend": "comsol_native",
            "package_name": "mmapy",
            "package_version": "0.3.1",
            "distribution_license": "GPL-3.0-or-later",
            "distribution_sha256": _sha256(
                backend_raw["distribution_sha256"], "backend_configuration.distribution_sha256"
            ),
            "distribution_path": str(Path(distribution_path)),
            "objective_direction": "maximize",
            "max_inner_iterations": inner,
        }
    if raw["method"] not in _OPTIMIZER_METHODS:
        raise ValueError("native optimizer method is unsupported")
    budget = _object(
        raw["budget"],
        {
            "cores",
            "max_solves",
            "max_iterations",
            "max_wall_time_seconds",
            "max_commit_fraction",
            "max_disk_bytes",
            "max_review_items",
        },
        "budget",
    )
    integer_limits = {
        "cores": (1, 1024),
        "max_solves": (1, 100_000),
        "max_iterations": (1, 10_000),
        "max_wall_time_seconds": (1, 31_536_000),
        "max_disk_bytes": (1, 1 << 50),
        "max_review_items": (1, 100_000),
    }
    normalized_budget: dict[str, int | float] = {}
    for key, (minimum, maximum) in integer_limits.items():
        item = budget[key]
        if isinstance(item, bool) or not isinstance(item, int) or not minimum <= item <= maximum:
            raise ValueError(f"budget.{key} must be an integer in the allowed range")
        normalized_budget[key] = item
    commit_fraction = _finite(
        budget["max_commit_fraction"], "budget.max_commit_fraction", positive=True
    )
    if commit_fraction > 1.0:
        raise ValueError("budget.max_commit_fraction must not exceed one")
    normalized_budget["max_commit_fraction"] = commit_fraction
    checkpoint = _object(
        raw["checkpoint_policy"],
        {"every_accepted_iteration", "save_copy", "exact_native_resume_required"},
        "checkpoint_policy",
    )
    if any(not isinstance(item, bool) for item in checkpoint.values()):
        raise ValueError("checkpoint_policy values must be boolean")
    seed = raw["deterministic_seed"]
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= (1 << 63) - 1:
        raise ValueError("deterministic_seed must be a bounded nonnegative integer")
    body = {
        "schema_name": NATIVE_OPTIMIZER_SCHEMA_NAME,
        "schema_version": schema_version,
        "optimizer_id": _identifier(raw["optimizer_id"], "optimizer_id"),
        "backend": raw["backend"],
        "method": raw["method"],
        "move_limit": _finite(raw["move_limit"], "move_limit", positive=True),
        "optimality_tolerance": _finite(
            raw["optimality_tolerance"], "optimality_tolerance", positive=True
        ),
        "constraint_tolerance": _finite(
            raw["constraint_tolerance"], "constraint_tolerance", positive=True
        ),
        "budget": normalized_budget,
        "checkpoint_policy": checkpoint,
        "deterministic_seed": seed,
    }
    if backend_configuration is not None:
        body["backend_configuration"] = backend_configuration
    body["optimizer_fingerprint"] = domain_sha256_v2(NATIVE_OPTIMIZER_SCHEMA_NAME, body)
    if supplied is not None and supplied != body["optimizer_fingerprint"]:
        raise ValueError("native optimizer fingerprint is invalid")
    return body


__all__ = [
    "GRADIENT_RECORD_SCHEMA_NAME",
    "GRADIENT_RECORD_SCHEMA_VERSION",
    "NATIVE_OPTIMIZER_SCHEMA_NAME",
    "NATIVE_OPTIMIZER_SCHEMA_VERSION",
    "NATIVE_OPTIMIZER_LEGACY_SCHEMA_VERSION",
    "normalize_gradient_record",
    "normalize_native_optimizer_configuration",
]
