"""Solver-free contracts for the reference-power licensed physical-evidence gate."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

from .contracts import canonical_sha256
from .power_audit import normalize_declared_plane_flux


REFERENCE_POWER_CONTRACT_SCHEMA = "comsol_mcp.h1_licensed_gate"
REFERENCE_POWER_EXECUTION_SCHEMA = "comsol_mcp.h1_execution_spec"
REFERENCE_POWER_SCHEMA_VERSION = "1.0.0"
MAX_INPUT_BYTES = 256 * 1024
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TAG = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_TOP_CONTRACT = {
    "schema_name", "schema_version", "fixture_id", "real_comsol_required",
    "runner", "execution_spec_environment", "limits", "acceptance",
}
_LIMIT_FIELDS = {"max_contract_bytes", "max_spec_bytes", "max_artifact_files", "max_artifact_bytes"}
_ACCEPTANCE_FIELDS = {
    "reference_air", "declared_flux", "wavelength", "source_unchanged",
    "clone_cleanup_proved", "reversed_sign_must_fail", "internal_consistency_cannot_substitute",
}
_REFERENCE_ACCEPTANCE_FIELDS = {"reflection_max", "r_plus_t_residual_max", "target_to_transverse_ratio_min"}
_FLUX_ACCEPTANCE_FIELDS = {"margin", "closure_abs_max"}
_WAVELENGTH_ACCEPTANCE_FIELDS = {"absolute_m_max", "relative_max"}
_TOP_SPEC = {
    "schema_name", "schema_version", "config_id", "source_model_path",
    "expected_source_sha256", "artifact_dir", "model", "wavelength",
    "reference_air", "declared_plane_flux",
}
_MODEL_FIELDS = {"component_tag", "physics_tag", "study_tag", "study_step_tag", "study_step_property"}
_WAVELENGTH_FIELDS = {"value", "unit", "parameter"}
_REFERENCE_FIELDS = {
    "expected_material_tags", "all_domain_ids", "top_air_domain_ids",
    "top_air_coordinate_range", "target_axis", "aggregation", "r_expression", "t_expression",
}


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _exact_fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing or unknown:
        raise ValueError(f"{label} fields mismatch: missing={missing}, unknown={unknown}")


def _text(value: Any, label: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be a non-empty string of at most {maximum} characters")
    return value


def _tag(value: Any, label: str) -> str:
    result = _text(value, label, 128)
    if not _TAG.fullmatch(result):
        raise ValueError(f"{label} must be an exact clientapi tag")
    return result


def _finite_nonnegative(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be finite and non-negative")
    result = float(value)
    if not math.isfinite(result) or result < 0.0:
        raise ValueError(f"{label} must be finite and non-negative")
    return result


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _positive_ids(value: Any, label: str) -> list[int]:
    if not isinstance(value, list) or not value or len(value) > 512:
        raise ValueError(f"{label} must contain 1..512 positive IDs")
    result = [_positive_int(item, f"{label}[{index}]") for index, item in enumerate(value)]
    if len(result) != len(set(result)):
        raise ValueError(f"{label} must not contain duplicates")
    return sorted(result)


def _coordinate_range(value: Any) -> dict[str, list[float]]:
    item = _object(value, "reference_air.top_air_coordinate_range")
    _exact_fields(item, {"x", "y", "z"}, "reference_air.top_air_coordinate_range")
    result: dict[str, list[float]] = {}
    for axis in ("x", "y", "z"):
        bounds = item[axis]
        if not isinstance(bounds, list) or len(bounds) != 2:
            raise ValueError(f"reference_air.top_air_coordinate_range.{axis} must contain two numbers")
        low = float(bounds[0])
        high = float(bounds[1])
        if not math.isfinite(low) or not math.isfinite(high) or low > high:
            raise ValueError(f"reference_air.top_air_coordinate_range.{axis} is invalid")
        result[axis] = [low, high]
    return result


def _relative_repo_path(value: Any, label: str) -> str:
    text = _text(value, label, 512)
    path = Path(text)
    if path.is_absolute() or ".." in path.parts or re.match(r"^[A-Za-z]:", text):
        raise ValueError(f"{label} must be a sanitized repository-relative path")
    return path.as_posix()


def validate_reference_power_acceptance_contract(value: Mapping[str, Any]) -> dict[str, Any]:
    contract = _object(dict(value), "h1_acceptance_contract")
    _exact_fields(contract, _TOP_CONTRACT, "h1_acceptance_contract")
    if contract["schema_name"] != REFERENCE_POWER_CONTRACT_SCHEMA or contract["schema_version"] != REFERENCE_POWER_SCHEMA_VERSION:
        raise ValueError("h1_acceptance_contract schema is unsupported")
    _text(contract["fixture_id"], "h1_acceptance_contract.fixture_id", 128)
    if contract["real_comsol_required"] is not True:
        raise ValueError("h1_acceptance_contract.real_comsol_required must be true")
    _relative_repo_path(contract["runner"], "h1_acceptance_contract.runner")
    _text(contract["execution_spec_environment"], "h1_acceptance_contract.execution_spec_environment", 128)
    limits = _object(contract["limits"], "h1_acceptance_contract.limits")
    _exact_fields(limits, _LIMIT_FIELDS, "h1_acceptance_contract.limits")
    for name in _LIMIT_FIELDS:
        _positive_int(limits[name], f"h1_acceptance_contract.limits.{name}")
    if limits["max_contract_bytes"] > MAX_INPUT_BYTES or limits["max_spec_bytes"] > MAX_INPUT_BYTES:
        raise ValueError(f"reference-power JSON input limits cannot exceed {MAX_INPUT_BYTES} bytes")
    acceptance = _object(contract["acceptance"], "h1_acceptance_contract.acceptance")
    _exact_fields(acceptance, _ACCEPTANCE_FIELDS, "h1_acceptance_contract.acceptance")
    reference = _object(acceptance["reference_air"], "acceptance.reference_air")
    _exact_fields(reference, _REFERENCE_ACCEPTANCE_FIELDS, "acceptance.reference_air")
    flux = _object(acceptance["declared_flux"], "acceptance.declared_flux")
    _exact_fields(flux, _FLUX_ACCEPTANCE_FIELDS, "acceptance.declared_flux")
    wavelength = _object(acceptance["wavelength"], "acceptance.wavelength")
    _exact_fields(wavelength, _WAVELENGTH_ACCEPTANCE_FIELDS, "acceptance.wavelength")
    for label, item in (("reference_air", reference), ("declared_flux", flux), ("wavelength", wavelength)):
        for name, number in item.items():
            _finite_nonnegative(number, f"acceptance.{label}.{name}")
    for name in (
        "source_unchanged", "clone_cleanup_proved", "reversed_sign_must_fail",
        "internal_consistency_cannot_substitute",
    ):
        if acceptance[name] is not True:
            raise ValueError(f"acceptance.{name} must be true")
    encoded = json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(encoded) > limits["max_contract_bytes"]:
        raise ValueError("h1_acceptance_contract exceeds its declared byte limit")
    return deepcopy(contract)


def _validate_flux_declaration(value: Any) -> dict[str, Any]:
    declaration = _object(value, "h1_execution_spec.declared_plane_flux")
    candidate = {}
    for name, plane in declaration.items():
        item = _object(plane, f"declared_plane_flux.{name}")
        sign = item.get("positive_power_sign")
        candidate[name] = {**item, "raw_power_w": float(sign) if sign in {-1, 1} else 0.0}
    normalized = normalize_declared_plane_flux(candidate)
    return {
        name: {
            key: plane[key]
            for key in (
                "expression", "selection_ids", "plane_coordinate_m", "normal",
                "medium_id", "positive_power_sign",
            )
        }
        for name, plane in normalized["planes"].items()
    }


def validate_reference_power_execution_spec(
    value: Mapping[str, Any],
    contract: Mapping[str, Any],
    *,
    verify_files: bool = False,
) -> dict[str, Any]:
    strict_contract = validate_reference_power_acceptance_contract(contract)
    spec = _object(dict(value), "h1_execution_spec")
    _exact_fields(spec, _TOP_SPEC, "h1_execution_spec")
    if spec["schema_name"] != REFERENCE_POWER_EXECUTION_SCHEMA or spec["schema_version"] != REFERENCE_POWER_SCHEMA_VERSION:
        raise ValueError("h1_execution_spec schema is unsupported")
    config_id = _text(spec["config_id"], "h1_execution_spec.config_id", 128)
    source_path = Path(_text(spec["source_model_path"], "h1_execution_spec.source_model_path")).expanduser()
    artifact_dir = Path(_text(spec["artifact_dir"], "h1_execution_spec.artifact_dir")).expanduser()
    if not source_path.is_absolute() or not artifact_dir.is_absolute():
        raise ValueError("source_model_path and artifact_dir must be absolute")
    if not str(artifact_dir).isascii():
        raise ValueError("artifact_dir must be ASCII-only")
    expected_hash = _text(spec["expected_source_sha256"], "h1_execution_spec.expected_source_sha256", 64).lower()
    if not _HEX64.fullmatch(expected_hash):
        raise ValueError("expected_source_sha256 must be exactly 64 hexadecimal characters")
    model = _object(spec["model"], "h1_execution_spec.model")
    _exact_fields(model, _MODEL_FIELDS, "h1_execution_spec.model")
    normalized_model = {name: _tag(model[name], f"model.{name}") for name in _MODEL_FIELDS}
    wavelength = _object(spec["wavelength"], "h1_execution_spec.wavelength")
    _exact_fields(wavelength, _WAVELENGTH_FIELDS, "h1_execution_spec.wavelength")
    wavelength_value = _finite_nonnegative(wavelength["value"], "wavelength.value")
    if wavelength_value <= 0.0:
        raise ValueError("wavelength.value must be positive")
    normalized_wavelength = {
        "value": wavelength_value,
        "unit": _text(wavelength["unit"], "wavelength.unit", 32),
        "parameter": _tag(wavelength["parameter"], "wavelength.parameter"),
    }
    reference = _object(spec["reference_air"], "h1_execution_spec.reference_air")
    _exact_fields(reference, _REFERENCE_FIELDS, "h1_execution_spec.reference_air")
    material_tags = reference["expected_material_tags"]
    if not isinstance(material_tags, list) or len(material_tags) > 128:
        raise ValueError("reference_air.expected_material_tags must be a bounded list")
    normalized_material_tags = sorted(_tag(item, "reference_air.expected_material_tags item") for item in material_tags)
    if len(normalized_material_tags) != len(set(normalized_material_tags)):
        raise ValueError("reference_air.expected_material_tags must not contain duplicates")
    all_domains = _positive_ids(reference["all_domain_ids"], "reference_air.all_domain_ids")
    top_domains = _positive_ids(reference["top_air_domain_ids"], "reference_air.top_air_domain_ids")
    if not set(top_domains).issubset(all_domains):
        raise ValueError("reference_air.top_air_domain_ids must be a subset of all_domain_ids")
    if reference["target_axis"] not in {"x", "y", "z"}:
        raise ValueError("reference_air.target_axis must be x, y, or z")
    if reference["aggregation"] not in {"rms_abs", "median_abs"}:
        raise ValueError("reference_air.aggregation must be rms_abs or median_abs")
    normalized_reference = {
        "expected_material_tags": normalized_material_tags,
        "all_domain_ids": all_domains,
        "top_air_domain_ids": top_domains,
        "top_air_coordinate_range": _coordinate_range(reference["top_air_coordinate_range"]),
        "target_axis": reference["target_axis"],
        "aggregation": reference["aggregation"],
        "r_expression": _text(reference["r_expression"], "reference_air.r_expression", 1024),
        "t_expression": _text(reference["t_expression"], "reference_air.t_expression", 1024),
    }
    normalized = {
        "schema_name": REFERENCE_POWER_EXECUTION_SCHEMA,
        "schema_version": REFERENCE_POWER_SCHEMA_VERSION,
        "config_id": config_id,
        "source_model_path": str(source_path.resolve()),
        "expected_source_sha256": expected_hash,
        "artifact_dir": str(artifact_dir.resolve()),
        "model": normalized_model,
        "wavelength": normalized_wavelength,
        "reference_air": normalized_reference,
        "declared_plane_flux": _validate_flux_declaration(spec["declared_plane_flux"]),
    }
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(encoded) > strict_contract["limits"]["max_spec_bytes"]:
        raise ValueError("h1_execution_spec exceeds its declared byte limit")
    if verify_files:
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        actual = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if actual != expected_hash:
            raise ValueError("source model SHA-256 does not match expected_source_sha256")
        artifact_dir.mkdir(parents=True, exist_ok=True)
    return normalized


def load_bounded_json(path: Path, maximum_bytes: int) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.stat().st_size > maximum_bytes:
        raise ValueError(f"JSON input exceeds {maximum_bytes} bytes: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    return _object(value, path.name)


def build_reference_power_dry_run_receipt(
    contract: Mapping[str, Any],
    spec: Mapping[str, Any] | None = None,
    *,
    verify_files: bool = False,
) -> dict[str, Any]:
    strict_contract = validate_reference_power_acceptance_contract(contract)
    receipt: dict[str, Any] = {
        "schema_name": "comsol_mcp.h1_dry_run_receipt",
        "schema_version": REFERENCE_POWER_SCHEMA_VERSION,
        "fixture_id": strict_contract["fixture_id"],
        "contract_sha256": canonical_sha256(strict_contract),
        "real_comsol_started": False,
        "contract_valid": True,
        "spec_valid": None,
    }
    if spec is not None:
        normalized = validate_reference_power_execution_spec(spec, strict_contract, verify_files=verify_files)
        path_free = deepcopy(normalized)
        path_free.pop("source_model_path")
        path_free.pop("artifact_dir")
        receipt.update(
            {
                "spec_valid": True,
                "config_id": normalized["config_id"],
                "config_sha256": canonical_sha256(path_free),
                "source_sha256": normalized["expected_source_sha256"],
                "paths_redacted": True,
            }
        )
    return receipt


__all__ = [
    "REFERENCE_POWER_CONTRACT_SCHEMA",
    "REFERENCE_POWER_EXECUTION_SCHEMA",
    "REFERENCE_POWER_SCHEMA_VERSION",
    "build_reference_power_dry_run_receipt",
    "load_bounded_json",
    "validate_reference_power_acceptance_contract",
    "validate_reference_power_execution_spec",
]
