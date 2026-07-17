"""Pure normalization for bounded durable spectral-characterization jobs."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping

from src.build_identity import get_build_identity
from src.evidence.spectral_characterization import (
    MAX_SPECTRAL_POINTS,
    normalize_spectral_analysis_policy,
    normalize_spectral_measurement_configuration,
)

from .resource_admission import normalize_resource_policy
from .store import JOB_SCHEMA_VERSION


MAX_INITIAL_GRID_POINTS = 257
MAX_STAGE_POINTS = 257
MAX_REFINEMENT_STAGES = 8
MAX_WINDOW_EXPANSIONS = 8
MAX_SPECTRAL_JOB_SPEC_BYTES = 512 * 1024
MAX_COLLECTOR_INPUT_BYTES = 64 * 1024
SPECTRAL_JOB_DRIVER_VERSION = "1.0.0"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TAG = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
_SHA256 = re.compile(r"^[0-9A-Fa-f]{64}$")
_LOCKED_COLLECTOR_INPUTS = frozenset(
    {
        "model_name",
        "wavelength_value",
        "wavelength_unit",
        "wavelength_parameter",
        "expected_source_sha256",
        "config_id",
        "artifact_dir",
        "session_state",
        "active_profile",
        "ownership_preflight",
        "clone_factory",
        "clone_register",
        "clone_cleanup",
    }
)
_ALLOWED_COLLECTOR_INPUTS = frozenset(
    {
        "component_tag",
        "physics_tag",
        "study_tag",
        "study_step_tag",
        "study_step_property",
        "r_expression",
        "t_expression",
        "a_expression",
        "top_air_selection",
        "top_air_domain_ids",
        "top_air_coordinate_range",
        "loss_map",
        "power_provenance",
        "declared_plane_flux",
        "internal_absorption",
        "air_reference_config_id",
        "validation_policy",
    }
)
_REQUIRED_COLLECTOR_INPUTS = frozenset(
    {
        "component_tag",
        "physics_tag",
        "study_tag",
        "study_step_tag",
        "study_step_property",
        "r_expression",
        "t_expression",
        "a_expression",
        "top_air_coordinate_range",
    }
)


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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object with string keys")
    return dict(value)


def _exact_mapping(value: object, fields: set[str], name: str) -> dict[str, Any]:
    raw = _mapping(value, name)
    if set(raw) != fields:
        raise ValueError(f"{name} requires exactly: {', '.join(sorted(fields))}")
    return raw


def _finite(
    value: object,
    name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    number = float(value)
    if (
        not math.isfinite(number)
        or (positive and number <= 0.0)
        or (nonnegative and number < 0.0)
    ):
        qualifier = (
            "positive and finite"
            if positive
            else "nonnegative and finite"
            if nonnegative
            else "finite"
        )
        raise ValueError(f"{name} must be {qualifier}")
    return number


def _integer(
    value: object,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _portable_relative_path(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError(f"{name} must be one portable relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{name} must be one portable relative path")
    if len(value) > 512:
        raise ValueError(f"{name} exceeds 512 characters")
    return path.as_posix()


def _normalize_json_object(value: object, name: str) -> dict[str, Any]:
    raw = _mapping(value, name)
    try:
        encoded = _canonical_bytes(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain only finite JSON values") from exc
    if len(encoded) > MAX_COLLECTOR_INPUT_BYTES:
        raise ValueError(f"{name} exceeds {MAX_COLLECTOR_INPUT_BYTES} bytes")
    return json.loads(encoded.decode("utf-8"))


def _normalize_initial_grid(value: object) -> dict[str, Any]:
    raw = _exact_mapping(
        value,
        {"lower_m", "upper_m", "point_count"},
        "initial_grid",
    )
    lower = _finite(raw["lower_m"], "initial_grid.lower_m", positive=True)
    upper = _finite(raw["upper_m"], "initial_grid.upper_m", positive=True)
    if upper <= lower:
        raise ValueError("initial_grid.upper_m must exceed lower_m")
    return {
        "lower_m": lower,
        "upper_m": upper,
        "point_count": _integer(
            raw["point_count"],
            "initial_grid.point_count",
            minimum=3,
            maximum=MAX_INITIAL_GRID_POINTS,
        ),
    }


def _normalize_refinement_policy(value: object) -> dict[str, Any]:
    fields = {
        "maximum_stages",
        "points_per_stage",
        "span_shrink_factor",
        "minimum_spacing_m",
        "peak_shift_abs_tolerance_m",
        "fit_support_peak_abs_tolerance_m",
        "fit_support_fwhm_abs_tolerance_m",
        "fit_support_quality_factor_abs_tolerance",
    }
    raw = _exact_mapping(value, fields, "refinement_policy")
    stages = _integer(
        raw["maximum_stages"],
        "refinement_policy.maximum_stages",
        minimum=0,
        maximum=MAX_REFINEMENT_STAGES,
    )
    points = _integer(
        raw["points_per_stage"],
        "refinement_policy.points_per_stage",
        minimum=3,
        maximum=MAX_STAGE_POINTS,
    )
    if points % 2 == 0:
        raise ValueError("refinement_policy.points_per_stage must be odd")
    shrink = _finite(
        raw["span_shrink_factor"],
        "refinement_policy.span_shrink_factor",
        positive=True,
    )
    if shrink <= 1.0 or shrink > 16.0:
        raise ValueError("refinement_policy.span_shrink_factor must be greater than 1 and at most 16")
    return {
        "maximum_stages": stages,
        "points_per_stage": points,
        "span_shrink_factor": shrink,
        "minimum_spacing_m": _finite(
            raw["minimum_spacing_m"],
            "refinement_policy.minimum_spacing_m",
            positive=True,
        ),
        "peak_shift_abs_tolerance_m": _finite(
            raw["peak_shift_abs_tolerance_m"],
            "refinement_policy.peak_shift_abs_tolerance_m",
            nonnegative=True,
        ),
        "fit_support_peak_abs_tolerance_m": _finite(
            raw["fit_support_peak_abs_tolerance_m"],
            "refinement_policy.fit_support_peak_abs_tolerance_m",
            nonnegative=True,
        ),
        "fit_support_fwhm_abs_tolerance_m": _finite(
            raw["fit_support_fwhm_abs_tolerance_m"],
            "refinement_policy.fit_support_fwhm_abs_tolerance_m",
            nonnegative=True,
        ),
        "fit_support_quality_factor_abs_tolerance": _finite(
            raw["fit_support_quality_factor_abs_tolerance"],
            "refinement_policy.fit_support_quality_factor_abs_tolerance",
            nonnegative=True,
        ),
    }


def _normalize_expansion_policy(
    value: object,
    *,
    initial_grid: Mapping[str, Any],
) -> dict[str, Any]:
    fields = {
        "maximum_expansions",
        "points_per_expansion",
        "span_multiplier",
        "absolute_lower_m",
        "absolute_upper_m",
    }
    raw = _exact_mapping(value, fields, "expansion_policy")
    lower = _finite(raw["absolute_lower_m"], "expansion_policy.absolute_lower_m", positive=True)
    upper = _finite(raw["absolute_upper_m"], "expansion_policy.absolute_upper_m", positive=True)
    if upper <= lower:
        raise ValueError("expansion_policy.absolute_upper_m must exceed absolute_lower_m")
    if lower > initial_grid["lower_m"] or upper < initial_grid["upper_m"]:
        raise ValueError("expansion_policy absolute bounds must contain the initial grid")
    points = _integer(
        raw["points_per_expansion"],
        "expansion_policy.points_per_expansion",
        minimum=3,
        maximum=MAX_STAGE_POINTS,
    )
    multiplier = _finite(
        raw["span_multiplier"],
        "expansion_policy.span_multiplier",
        positive=True,
    )
    if multiplier <= 1.0 or multiplier > 4.0:
        raise ValueError("expansion_policy.span_multiplier must be greater than 1 and at most 4")
    return {
        "maximum_expansions": _integer(
            raw["maximum_expansions"],
            "expansion_policy.maximum_expansions",
            minimum=0,
            maximum=MAX_WINDOW_EXPANSIONS,
        ),
        "points_per_expansion": points,
        "span_multiplier": multiplier,
        "absolute_lower_m": lower,
        "absolute_upper_m": upper,
    }


def _normalize_collector(value: object) -> dict[str, Any]:
    raw = _exact_mapping(value, {"name", "inputs"}, "collector")
    if raw["name"] != "wave_optics_point_audit":
        raise ValueError("collector.name must be wave_optics_point_audit")
    inputs = _normalize_json_object(raw["inputs"], "collector.inputs")
    conflicts = sorted(set(inputs) & _LOCKED_COLLECTOR_INPUTS)
    if conflicts:
        raise ValueError(f"collector.inputs attempt to override locked fields: {conflicts}")
    unknown = sorted(set(inputs) - _ALLOWED_COLLECTOR_INPUTS)
    missing = sorted(_REQUIRED_COLLECTOR_INPUTS - set(inputs))
    if unknown or missing:
        raise ValueError(f"collector.inputs has unsupported={unknown} missing={missing}")
    for field in (
        "component_tag",
        "physics_tag",
        "study_tag",
        "study_step_tag",
        "study_step_property",
    ):
        if not isinstance(inputs[field], str) or not _TAG.fullmatch(inputs[field]):
            raise ValueError(f"collector.inputs.{field} must be one exact tag")
    for field in ("r_expression", "t_expression", "a_expression"):
        if (
            not isinstance(inputs[field], str)
            or not inputs[field].strip()
            or len(inputs[field]) > 1024
        ):
            raise ValueError(f"collector.inputs.{field} must be a bounded nonempty expression")
    selection = inputs.get("top_air_selection")
    domains = inputs.get("top_air_domain_ids")
    if selection is None and not domains:
        raise ValueError("collector.inputs requires top_air_selection or top_air_domain_ids")
    coordinate_range = inputs["top_air_coordinate_range"]
    if not isinstance(coordinate_range, dict) or set(coordinate_range) != {"x", "y", "z"}:
        raise ValueError("collector.inputs.top_air_coordinate_range requires exactly x, y, and z")
    for axis in ("x", "y", "z"):
        limits = coordinate_range[axis]
        if not isinstance(limits, list) or len(limits) != 2:
            raise ValueError(f"collector.inputs.top_air_coordinate_range.{axis} must contain two limits")
        low = _finite(limits[0], f"collector.inputs.top_air_coordinate_range.{axis}[0]")
        high = _finite(limits[1], f"collector.inputs.top_air_coordinate_range.{axis}[1]")
        if low > high:
            raise ValueError(f"collector.inputs.top_air_coordinate_range.{axis} must be ordered")
    return {"name": raw["name"], "inputs": inputs}


def current_spectral_driver_identity() -> dict[str, str]:
    """Bind resume to the exact shipped package bytes and driver contract."""
    build = get_build_identity()
    return {
        "implementation": "src.jobs.spectral_worker",
        "driver_version": SPECTRAL_JOB_DRIVER_VERSION,
        "package_content_sha256": build["package_content_sha256"],
        "build_identity_sha256": build["build_identity_sha256"],
    }


def validate_spectral_driver_identity(spec: Mapping[str, Any]) -> dict[str, str]:
    """Fail closed when an immutable job belongs to different package bytes."""
    observed = spec.get("driver_identity")
    expected = current_spectral_driver_identity()
    if observed != expected:
        raise ValueError("spectral job driver identity differs from the running package")
    return expected


def normalize_spectral_characterization_job_spec(raw_spec: object) -> dict[str, Any]:
    """Normalize one immutable adaptive spectrum request without importing COMSOL."""
    raw = _mapping(raw_spec, "spectral characterization job specification")
    allowed = {
        "job_type",
        "source_model_path",
        "source_model_relative_identity",
        "configuration_sha256",
        "parameter_state",
        "wavelength_parameter",
        "initial_grid",
        "refinement_policy",
        "expansion_policy",
        "maximum_points",
        "collector",
        "analysis_policy",
        "measurement_configuration",
        "resource_policy",
        "cores",
        "version",
        "max_retries",
        "continue_on_error",
    }
    unknown = sorted(set(raw) - allowed)
    missing = sorted(allowed - {"version", "max_retries", "continue_on_error"} - set(raw))
    if unknown or missing:
        raise ValueError(f"spectral characterization job has unsupported={unknown} missing={missing}")
    if raw.get("job_type") != "spectral_characterization":
        raise ValueError("job_type must be spectral_characterization")

    source_value = raw["source_model_path"]
    if not isinstance(source_value, str) or not source_value.strip():
        raise ValueError("source_model_path must be a nonempty string")
    source = Path(source_value).expanduser().resolve()
    if not source.is_file() or source.suffix.casefold() != ".mph":
        raise ValueError("source_model_path must name an existing MPH file")
    configuration = raw["configuration_sha256"]
    if not isinstance(configuration, str) or not _SHA256.fullmatch(configuration):
        raise ValueError("configuration_sha256 must be exactly 64 hexadecimal characters")
    wavelength_parameter = raw["wavelength_parameter"]
    if not isinstance(wavelength_parameter, str) or not _TAG.fullmatch(wavelength_parameter):
        raise ValueError("wavelength_parameter must be one exact tag")

    parameter_state = _normalize_json_object(raw["parameter_state"], "parameter_state")
    initial = _normalize_initial_grid(raw["initial_grid"])
    refinement = _normalize_refinement_policy(raw["refinement_policy"])
    expansion = _normalize_expansion_policy(raw["expansion_policy"], initial_grid=initial)
    maximum_points = _integer(
        raw["maximum_points"],
        "maximum_points",
        minimum=3,
        maximum=MAX_SPECTRAL_POINTS,
    )
    if initial["point_count"] > maximum_points:
        raise ValueError("initial grid exceeds maximum_points")
    analysis_policy = normalize_spectral_analysis_policy(raw["analysis_policy"])
    if analysis_policy["minimum_point_count"] > initial["point_count"]:
        raise ValueError("analysis_policy.minimum_point_count exceeds the initial grid")
    measurement = normalize_spectral_measurement_configuration(raw["measurement_configuration"])

    resource_policy = normalize_resource_policy(raw["resource_policy"])
    if resource_policy is None:
        raise ValueError("resource_policy is required")
    rules = resource_policy["rules"]
    wall_fields = {"wall_time_budget_seconds", "minimum_next_point_seconds"}
    if not wall_fields <= set(rules):
        raise ValueError("resource_policy must declare a wall-time budget")
    if not set(rules) - wall_fields:
        raise ValueError("resource_policy must declare at least one non-wall resource limit")
    if maximum_points * rules["minimum_next_point_seconds"] > rules["wall_time_budget_seconds"]:
        raise ValueError("maximum_points exceed the caller-declared wall-time budget")

    cores = _integer(raw["cores"], "cores", minimum=1, maximum=1024)
    max_retries = _integer(raw.get("max_retries", 0), "max_retries", minimum=0, maximum=3)
    continue_on_error = raw.get("continue_on_error", False)
    if not isinstance(continue_on_error, bool):
        raise ValueError("continue_on_error must be boolean")
    version = raw.get("version")
    if version is not None and (
        not isinstance(version, str) or not version.strip() or len(version) > 32
    ):
        raise ValueError("version must be a bounded nonempty string when provided")

    spec = {
        "job_type": "spectral_characterization",
        "schema_version": JOB_SCHEMA_VERSION,
        "source_model_path": str(source),
        "source_model_relative_identity": _portable_relative_path(
            raw["source_model_relative_identity"],
            "source_model_relative_identity",
        ),
        "source_model_sha256": _sha256_file(source),
        "configuration_sha256": configuration.lower(),
        "parameter_state": parameter_state,
        "parameter_state_sha256": _fingerprint(parameter_state),
        "wavelength_parameter": wavelength_parameter,
        "initial_grid": initial,
        "refinement_policy": refinement,
        "expansion_policy": expansion,
        "maximum_points": maximum_points,
        "collector": _normalize_collector(raw["collector"]),
        "analysis_policy": analysis_policy,
        "measurement_configuration": measurement,
        "resource_policy": resource_policy,
        "cores": cores,
        "version": version.strip() if isinstance(version, str) else None,
        "max_retries": max_retries,
        "continue_on_error": continue_on_error,
        "driver_identity": current_spectral_driver_identity(),
    }
    encoded = _canonical_bytes(spec)
    if len(encoded) > MAX_SPECTRAL_JOB_SPEC_BYTES:
        raise ValueError(
            f"spectral characterization job exceeds {MAX_SPECTRAL_JOB_SPEC_BYTES} bytes"
        )
    spec["spec_fingerprint"] = _fingerprint(spec)
    return spec


__all__ = [
    "MAX_INITIAL_GRID_POINTS",
    "MAX_REFINEMENT_STAGES",
    "MAX_SPECTRAL_JOB_SPEC_BYTES",
    "MAX_STAGE_POINTS",
    "MAX_WINDOW_EXPANSIONS",
    "SPECTRAL_JOB_DRIVER_VERSION",
    "current_spectral_driver_identity",
    "normalize_spectral_characterization_job_spec",
    "validate_spectral_driver_identity",
]
