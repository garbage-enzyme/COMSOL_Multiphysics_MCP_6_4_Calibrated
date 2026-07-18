"""Deterministic solver-free characterization of provenance-bound spectra."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import PurePosixPath
import re
from typing import Any, Mapping

import numpy as np
from scipy.optimize import brentq, curve_fit


SPECTRAL_BUNDLE_SCHEMA = "comsol_mcp.spectral_point_bundle"
SPECTRAL_DECISION_SCHEMA = "comsol_mcp.spectral_analysis_decision"
SPECTRAL_CHARACTERIZATION_SCHEMA = "comsol_mcp.spectral_characterization"
SPECTRAL_SCHEMA_VERSION = "1.0.0"
MAX_SPECTRAL_POINTS = 1024
MAX_PARAMETER_STATE_BYTES = 64 * 1024

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_SOURCE_FIELDS = {"relative_identity", "sha256"}
_WAVELENGTH_FIELDS = {
    "unit",
    "requested_field",
    "evaluated_field",
    "frequency_derived_field",
    "frequency_relation",
}
_ROW_FIELDS = {
    "row_id",
    "raw_row_sha256",
    "configuration_sha256",
    "requested_wavelength_m",
    "evaluated_wavelength_m",
    "frequency_wavelength_m",
    "R",
    "T",
    "A",
}
_POLICY_FIELDS = {
    "response_quantity",
    "candidate_polarity",
    "passivity_abs_tolerance",
    "closure_abs_tolerance",
    "wavelength_sync_abs_m",
    "flat_response_abs_tolerance",
    "minimum_point_count",
}
_MEASUREMENT_FIELDS = {
    "peak_method",
    "baseline_rule",
    "baseline_response_value",
    "fwhm_definition",
    "fit_support_points",
    "fit_support_sensitivity_points",
    "local_polynomial_degree",
    "fit_max_evaluations",
}


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("spectral evidence must contain finite JSON values") from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object with string keys")
    return dict(value)


def _exact_fields(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    item = _mapping(value, label)
    if set(item) != expected:
        raise ValueError(f"{label} fields are invalid")
    return item


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a bounded portable identifier")
    return value


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value.lower()):
        raise ValueError(f"{label} must be exactly 64 hexadecimal characters")
    return value.lower()


def _finite(value: Any, label: str, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    if nonnegative and result < 0.0:
        raise ValueError(f"{label} must be nonnegative")
    return result


def _relative_identity(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError(f"{label} must be a bounded relative identity")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError(f"{label} must be relative and traversal-free")
    return normalized


def _bounded_text(value: Any, label: str, *, maximum: int = 1024) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{label} must be nonempty and at most {maximum} characters")
    return value


def _normalize_source(value: Any) -> dict[str, str]:
    item = _exact_fields(value, _SOURCE_FIELDS, "source_model")
    return {
        "relative_identity": _relative_identity(
            item["relative_identity"], "source_model.relative_identity"
        ),
        "sha256": _hash(item["sha256"], "source_model.sha256"),
    }


def _normalize_wavelength_convention(value: Any) -> dict[str, str]:
    item = _exact_fields(value, _WAVELENGTH_FIELDS, "wavelength_convention")
    if item["unit"] != "m":
        raise ValueError("wavelength_convention.unit must be 'm'")
    if item["frequency_relation"] != "c_const/frequency":
        raise ValueError(
            "wavelength_convention.frequency_relation must be 'c_const/frequency'"
        )
    return {
        key: _bounded_text(item[key], f"wavelength_convention.{key}", maximum=128)
        for key in sorted(_WAVELENGTH_FIELDS)
    }


def _normalize_expressions(value: Any) -> dict[str, str]:
    item = _exact_fields(value, {"R", "T", "A"}, "expressions")
    return {
        name: _bounded_text(item[name], f"expressions.{name}")
        for name in ("R", "T", "A")
    }


def _normalize_row(value: Any, index: int, configuration_sha256: str) -> dict[str, Any]:
    label = f"rows[{index}]"
    item = _exact_fields(value, _ROW_FIELDS, label)
    row_configuration = _hash(
        item["configuration_sha256"], f"{label}.configuration_sha256"
    )
    if row_configuration != configuration_sha256:
        raise ValueError(f"{label}.configuration_sha256 does not match the bundle")
    return {
        "row_id": _identifier(item["row_id"], f"{label}.row_id"),
        "raw_row_sha256": _hash(item["raw_row_sha256"], f"{label}.raw_row_sha256"),
        "configuration_sha256": row_configuration,
        "requested_wavelength_m": _finite(
            item["requested_wavelength_m"], f"{label}.requested_wavelength_m"
        ),
        "evaluated_wavelength_m": _finite(
            item["evaluated_wavelength_m"], f"{label}.evaluated_wavelength_m"
        ),
        "frequency_wavelength_m": _finite(
            item["frequency_wavelength_m"], f"{label}.frequency_wavelength_m"
        ),
        "R": _finite(item["R"], f"{label}.R"),
        "T": _finite(item["T"], f"{label}.T"),
        "A": _finite(item["A"], f"{label}.A"),
    }


def build_spectral_point_bundle(
    *,
    bundle_id: str,
    source_model: Mapping[str, Any],
    configuration_sha256: str,
    parameter_state: Mapping[str, Any],
    wavelength_convention: Mapping[str, Any],
    expressions: Mapping[str, Any],
    rows: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Normalize immutable point projections and bind them to their raw row hashes."""
    configuration = _hash(configuration_sha256, "configuration_sha256")
    if not isinstance(rows, list) or not 3 <= len(rows) <= MAX_SPECTRAL_POINTS:
        raise ValueError(f"rows must contain 3..{MAX_SPECTRAL_POINTS} entries")
    parameters = _mapping(parameter_state, "parameter_state")
    if len(_canonical_bytes(parameters)) > MAX_PARAMETER_STATE_BYTES:
        raise ValueError("parameter_state exceeds its byte limit")
    normalized_rows = [
        _normalize_row(row, index, configuration) for index, row in enumerate(rows)
    ]
    row_ids = [row["row_id"] for row in normalized_rows]
    raw_hashes = [row["raw_row_sha256"] for row in normalized_rows]
    wavelengths = [row["requested_wavelength_m"] for row in normalized_rows]
    if len(row_ids) != len(set(row_ids)):
        raise ValueError("row IDs must be unique")
    if len(raw_hashes) != len(set(raw_hashes)):
        raise ValueError("raw row hashes must be unique")
    if any(value <= 0.0 for value in wavelengths):
        raise ValueError("requested wavelengths must be positive")
    if any(right <= left for left, right in zip(wavelengths, wavelengths[1:])):
        raise ValueError("requested wavelengths must be sorted and unique")
    body = {
        "schema_name": SPECTRAL_BUNDLE_SCHEMA,
        "schema_version": SPECTRAL_SCHEMA_VERSION,
        "bundle_id": _identifier(bundle_id, "bundle_id"),
        "source_model": _normalize_source(source_model),
        "configuration_sha256": configuration,
        "parameter_state": deepcopy(parameters),
        "parameter_state_sha256": _sha256(parameters),
        "wavelength_convention": _normalize_wavelength_convention(
            wavelength_convention
        ),
        "expressions": _normalize_expressions(expressions),
        "rows": normalized_rows,
    }
    return {**body, "bundle_sha256": _sha256(body)}


def validate_spectral_point_bundle(value: Any) -> dict[str, Any]:
    """Validate a canonical spectral point bundle without mutating it."""
    item = _mapping(value, "spectral_bundle")
    expected = {
        "schema_name",
        "schema_version",
        "bundle_id",
        "source_model",
        "configuration_sha256",
        "parameter_state",
        "parameter_state_sha256",
        "wavelength_convention",
        "expressions",
        "rows",
        "bundle_sha256",
    }
    if set(item) != expected:
        raise ValueError("spectral bundle fields are invalid")
    if (
        item["schema_name"] != SPECTRAL_BUNDLE_SCHEMA
        or item["schema_version"] != SPECTRAL_SCHEMA_VERSION
    ):
        raise ValueError("spectral bundle schema is unsupported")
    rebuilt = build_spectral_point_bundle(
        bundle_id=item["bundle_id"],
        source_model=item["source_model"],
        configuration_sha256=item["configuration_sha256"],
        parameter_state=item["parameter_state"],
        wavelength_convention=item["wavelength_convention"],
        expressions=item["expressions"],
        rows=item["rows"],
    )
    if item["parameter_state_sha256"] != rebuilt["parameter_state_sha256"]:
        raise ValueError("parameter state hash does not match")
    if item["bundle_sha256"] != rebuilt["bundle_sha256"] or item != rebuilt:
        raise ValueError("spectral bundle is noncanonical or its hash does not match")
    return deepcopy(rebuilt)


def _normalize_analysis_policy(value: Any) -> dict[str, Any]:
    item = _exact_fields(value, _POLICY_FIELDS, "analysis_policy")
    response = item["response_quantity"]
    polarity = item["candidate_polarity"]
    if response not in {"R", "T", "A"}:
        raise ValueError("analysis_policy.response_quantity must be R, T, or A")
    if polarity not in {"maximum", "minimum"}:
        raise ValueError(
            "analysis_policy.candidate_polarity must be maximum or minimum"
        )
    minimum = item["minimum_point_count"]
    if isinstance(minimum, bool) or not isinstance(minimum, int) or not 3 <= minimum <= 101:
        raise ValueError("analysis_policy.minimum_point_count must be 3..101")
    return {
        "response_quantity": response,
        "candidate_polarity": polarity,
        "passivity_abs_tolerance": _finite(
            item["passivity_abs_tolerance"],
            "analysis_policy.passivity_abs_tolerance",
            nonnegative=True,
        ),
        "closure_abs_tolerance": _finite(
            item["closure_abs_tolerance"],
            "analysis_policy.closure_abs_tolerance",
            nonnegative=True,
        ),
        "wavelength_sync_abs_m": _finite(
            item["wavelength_sync_abs_m"],
            "analysis_policy.wavelength_sync_abs_m",
            nonnegative=True,
        ),
        "flat_response_abs_tolerance": _finite(
            item["flat_response_abs_tolerance"],
            "analysis_policy.flat_response_abs_tolerance",
            nonnegative=True,
        ),
        "minimum_point_count": minimum,
    }


def normalize_spectral_analysis_policy(value: Any) -> dict[str, Any]:
    """Return one canonical caller-owned spectral evidence policy."""
    return deepcopy(_normalize_analysis_policy(value))


def build_spectral_analysis_decision(
    bundle: Mapping[str, Any], analysis_policy: Mapping[str, Any]
) -> dict[str, Any]:
    """Apply caller-declared evidence gates and classify spectral candidates."""
    normalized = validate_spectral_point_bundle(bundle)
    policy = _normalize_analysis_policy(analysis_policy)
    rows = normalized["rows"]
    passivity_tolerance = policy["passivity_abs_tolerance"]
    closure_tolerance = policy["closure_abs_tolerance"]
    sync_tolerance = policy["wavelength_sync_abs_m"]
    row_checks = []
    invalid_rows = []
    for row in rows:
        closure = abs(1.0 - row["R"] - row["T"] - row["A"])
        sync_error = max(
            abs(row["requested_wavelength_m"] - row["evaluated_wavelength_m"]),
            abs(row["requested_wavelength_m"] - row["frequency_wavelength_m"]),
        )
        passive = all(
            -passivity_tolerance <= row[name] <= 1.0 + passivity_tolerance
            for name in ("R", "T", "A")
        )
        checks = {
            "row_id": row["row_id"],
            "raw_row_sha256": row["raw_row_sha256"],
            "passivity_passed": passive,
            "closure_abs": closure,
            "closure_passed": closure <= closure_tolerance,
            "wavelength_sync_abs_m": sync_error,
            "wavelength_sync_passed": sync_error <= sync_tolerance,
        }
        if not all(
            checks[name]
            for name in (
                "passivity_passed",
                "closure_passed",
                "wavelength_sync_passed",
            )
        ):
            invalid_rows.append(row["row_id"])
        row_checks.append(checks)

    response = [row[policy["response_quantity"]] for row in rows]
    oriented = response if policy["candidate_polarity"] == "maximum" else [-v for v in response]
    span = max(response) - min(response)
    local_indices = [
        index
        for index in range(1, len(rows) - 1)
        if oriented[index] > oriented[index - 1]
        and oriented[index] > oriented[index + 1]
    ]
    boundary_indices = []
    if oriented[0] > oriented[1]:
        boundary_indices.append(0)
    if oriented[-1] > oriented[-2]:
        boundary_indices.append(len(rows) - 1)

    if invalid_rows:
        classification = "invalid_evidence"
    elif len(rows) < policy["minimum_point_count"]:
        classification = "under_sampled"
    elif span <= policy["flat_response_abs_tolerance"]:
        classification = "flat"
    elif boundary_indices and max(oriented[index] for index in boundary_indices) >= max(oriented):
        classification = "boundary_high"
    elif len(local_indices) > 1:
        classification = "multi_candidate"
    elif len(local_indices) == 1:
        classification = "interior_candidate"
    else:
        classification = "no_candidate"

    evidence_rows = [
        {"row_id": row["row_id"], "raw_row_sha256": row["raw_row_sha256"]}
        for row in rows
    ]
    body = {
        "schema_name": SPECTRAL_DECISION_SCHEMA,
        "schema_version": SPECTRAL_SCHEMA_VERSION,
        "bundle_id": normalized["bundle_id"],
        "bundle_sha256": normalized["bundle_sha256"],
        "configuration_sha256": normalized["configuration_sha256"],
        "analysis_policy": policy,
        "analysis_policy_sha256": _sha256(policy),
        "classification": classification,
        "candidate_row_ids": [rows[index]["row_id"] for index in local_indices],
        "boundary_row_ids": [rows[index]["row_id"] for index in boundary_indices],
        "invalid_row_ids": invalid_rows,
        "response_span": span,
        "row_checks": row_checks,
        "evidence_rows": evidence_rows,
    }
    return {**body, "decision_sha256": _sha256(body)}


def validate_spectral_analysis_decision(
    value: Any, *, bundle: Mapping[str, Any]
) -> dict[str, Any]:
    """Recompute a decision from its exact bundle and reject hash tampering."""
    item = _mapping(value, "spectral_decision")
    expected = {
        "schema_name",
        "schema_version",
        "bundle_id",
        "bundle_sha256",
        "configuration_sha256",
        "analysis_policy",
        "analysis_policy_sha256",
        "classification",
        "candidate_row_ids",
        "boundary_row_ids",
        "invalid_row_ids",
        "response_span",
        "row_checks",
        "evidence_rows",
        "decision_sha256",
    }
    if set(item) != expected:
        raise ValueError("spectral decision fields are invalid")
    rebuilt = build_spectral_analysis_decision(bundle, item["analysis_policy"])
    if item != rebuilt:
        raise ValueError("spectral decision is noncanonical or its hash does not match")
    return deepcopy(rebuilt)


def _normalize_measurement_configuration(value: Any) -> dict[str, str]:
    item = _exact_fields(value, _MEASUREMENT_FIELDS, "measurement_configuration")
    fit_methods = {"local_polynomial_fit", "lorentzian_fit", "fano_fit"}
    if item["peak_method"] not in {
        "measured_grid",
        "quadratic_interpolation",
        *fit_methods,
    }:
        raise ValueError(
            "measurement_configuration.peak_method is unsupported"
        )
    if item["baseline_rule"] not in {
        "local_prominence",
        "window_endpoints_mean",
        "declared_response",
    }:
        raise ValueError(
            "measurement_configuration.baseline_rule must be local_prominence, window_endpoints_mean, or declared_response"
        )
    declared_baseline = item["baseline_response_value"]
    if item["baseline_rule"] == "declared_response":
        declared_baseline = _finite(
            declared_baseline,
            "measurement_configuration.baseline_response_value",
        )
    elif declared_baseline is not None:
        raise ValueError(
            "measurement_configuration.baseline_response_value is only valid with declared_response"
        )
    if item["fwhm_definition"] != "half_prominence":
        raise ValueError(
            "measurement_configuration.fwhm_definition must be half_prominence"
        )
    support = item["fit_support_points"]
    sensitivity = item["fit_support_sensitivity_points"]
    degree = item["local_polynomial_degree"]
    max_evaluations = item["fit_max_evaluations"]
    if item["peak_method"] in fit_methods:
        minimum = 5 if item["peak_method"] != "fano_fit" else 7
        if (
            isinstance(support, bool)
            or not isinstance(support, int)
            or not minimum <= support <= 101
            or support % 2 == 0
        ):
            raise ValueError(
                f"measurement_configuration.fit_support_points must be odd and {minimum}..101"
            )
        if not isinstance(sensitivity, list) or len(sensitivity) > 16:
            raise ValueError(
                "measurement_configuration.fit_support_sensitivity_points must be a bounded list"
            )
        normalized_sensitivity = []
        for index, count in enumerate(sensitivity):
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or not minimum <= count <= 101
                or count % 2 == 0
            ):
                raise ValueError(
                    f"fit_support_sensitivity_points[{index}] must be odd and {minimum}..101"
                )
            normalized_sensitivity.append(count)
        if len(normalized_sensitivity) != len(set(normalized_sensitivity)):
            raise ValueError("fit support sensitivity point counts must be unique")
        normalized_sensitivity.sort()
        if (
            isinstance(max_evaluations, bool)
            or not isinstance(max_evaluations, int)
            or not 100 <= max_evaluations <= 100000
        ):
            raise ValueError(
                "measurement_configuration.fit_max_evaluations must be 100..100000"
            )
        if item["peak_method"] == "local_polynomial_fit":
            if isinstance(degree, bool) or degree not in {2, 3, 4}:
                raise ValueError(
                    "measurement_configuration.local_polynomial_degree must be 2, 3, or 4"
                )
            if support <= degree:
                raise ValueError("fit support must exceed local polynomial degree")
        elif degree is not None:
            raise ValueError(
                "measurement_configuration.local_polynomial_degree is only valid for local_polynomial_fit"
            )
    else:
        if support is not None or sensitivity not in ([], None) or degree is not None or max_evaluations is not None:
            raise ValueError("fit settings are only valid for fit peak methods")
        normalized_sensitivity = []
        sensitivity = []
    return {
        "peak_method": item["peak_method"],
        "baseline_rule": item["baseline_rule"],
        "baseline_response_value": declared_baseline,
        "fwhm_definition": item["fwhm_definition"],
        "fit_support_points": support,
        "fit_support_sensitivity_points": normalized_sensitivity,
        "local_polynomial_degree": degree,
        "fit_max_evaluations": max_evaluations,
    }


def normalize_spectral_measurement_configuration(value: Any) -> dict[str, Any]:
    """Return one canonical caller-owned peak and linewidth configuration."""
    return deepcopy(_normalize_measurement_configuration(value))


def _row_reference(row: Mapping[str, Any]) -> dict[str, str]:
    return {"row_id": row["row_id"], "raw_row_sha256": row["raw_row_sha256"]}


def _linear_crossing(
    x0: float, y0: float, x1: float, y1: float, level: float
) -> float:
    if y1 == y0:
        raise ValueError("half-prominence crossing has zero response slope")
    fraction = (level - y0) / (y1 - y0)
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("half-prominence crossing is outside its row bracket")
    return x0 + fraction * (x1 - x0)


def _crossing_brackets(
    wavelengths: list[float], oriented: list[float], candidate_index: int, level: float
) -> tuple[tuple[int, int, float] | None, tuple[int, int, float] | None]:
    left = None
    for index in range(candidate_index - 1, -1, -1):
        if oriented[index] <= level <= oriented[index + 1]:
            left = (
                index,
                index + 1,
                _linear_crossing(
                    wavelengths[index],
                    oriented[index],
                    wavelengths[index + 1],
                    oriented[index + 1],
                    level,
                ),
            )
            break
    right = None
    for index in range(candidate_index, len(oriented) - 1):
        if oriented[index] >= level >= oriented[index + 1]:
            right = (
                index,
                index + 1,
                _linear_crossing(
                    wavelengths[index],
                    oriented[index],
                    wavelengths[index + 1],
                    oriented[index + 1],
                    level,
                ),
            )
            break
    return left, right


def _quadratic_peak(
    wavelengths: list[float], oriented: list[float], candidate_index: int
) -> tuple[float, float, list[int], dict[str, Any]]:
    support = [candidate_index - 1, candidate_index, candidate_index + 1]
    x = np.asarray([wavelengths[index] for index in support], dtype=float)
    y = np.asarray([oriented[index] for index in support], dtype=float)
    origin = float(x[1])
    scale = float(max(abs(x[0] - origin), abs(x[2] - origin)))
    if scale == 0.0:
        raise ValueError("quadratic interpolation support has zero wavelength span")
    normalized_x = (x - origin) / scale
    coefficients = np.polyfit(normalized_x, y, 2)
    curvature, slope, intercept = (float(value) for value in coefficients)
    if curvature >= 0.0:
        raise ValueError("quadratic interpolation does not have the requested peak curvature")
    vertex = -slope / (2.0 * curvature)
    wavelength = origin + vertex * scale
    if not float(x[0]) <= wavelength <= float(x[-1]):
        raise ValueError("quadratic interpolation peak is outside its support bracket")
    response = float(np.polyval(coefficients, vertex))
    residuals = y - np.polyval(coefficients, normalized_x)
    diagnostics = {
        "coordinate_origin_m": origin,
        "coordinate_scale_m": scale,
        "coefficients_oriented": [curvature, slope, intercept],
        "residual_sum_squares": float(np.dot(residuals, residuals)),
    }
    return wavelength, response, support, diagnostics


def _centered_support(candidate_index: int, count: int, row_count: int) -> list[int]:
    radius = count // 2
    if candidate_index - radius < 0 or candidate_index + radius >= row_count:
        raise ValueError("declared fit support does not fit around the candidate")
    return list(range(candidate_index - radius, candidate_index + radius + 1))


def _covariance_diagnostics(covariance: np.ndarray | None) -> tuple[list[list[float]] | None, float | None]:
    if covariance is None or covariance.shape[0] == 0 or not np.all(np.isfinite(covariance)):
        return None, None
    condition = float(np.linalg.cond(covariance))
    return covariance.tolist(), condition if math.isfinite(condition) else None


def _model_crossings(
    model,
    *,
    left_bound: float,
    peak: float,
    right_bound: float,
    level: float,
) -> tuple[float | None, float | None]:
    if not left_bound < peak < right_bound:
        return None, None

    def locate(start: float, stop: float, reverse: bool) -> float | None:
        grid = np.linspace(start, stop, 1025)
        values = np.asarray([float(model(value) - level) for value in grid])
        pairs = range(len(grid) - 1)
        if reverse:
            pairs = reversed(range(len(grid) - 1))
        for index in pairs:
            first = values[index]
            second = values[index + 1]
            if first == 0.0:
                return float(grid[index])
            if first * second < 0.0 or second == 0.0:
                return float(
                    brentq(
                        lambda coordinate: float(model(coordinate) - level),
                        float(grid[index]),
                        float(grid[index + 1]),
                    )
                )
        return None

    return locate(left_bound, peak, True), locate(peak, right_bound, False)


def _fit_candidate(
    *,
    method: str,
    wavelengths: list[float],
    oriented: list[float],
    candidate_index: int,
    support_count: int,
    baseline: float,
    polynomial_degree: int | None,
    max_evaluations: int,
) -> dict[str, Any]:
    support_indices = _centered_support(candidate_index, support_count, len(wavelengths))
    x = np.asarray([wavelengths[index] for index in support_indices], dtype=float)
    y = np.asarray([oriented[index] for index in support_indices], dtype=float)
    origin = float(wavelengths[candidate_index])
    scale = float(max(abs(x[0] - origin), abs(x[-1] - origin)))
    if scale <= 0.0:
        raise ValueError("fit support has zero wavelength span")
    z = (x - origin) / scale
    covariance = None
    parameter_names: list[str]
    parameter_values: list[float]

    if method == "local_polynomial_fit":
        if polynomial_degree is None:
            raise ValueError(
                "polynomial_degree is required for local_polynomial_fit"
            )
        coefficients, covariance = np.polyfit(z, y, polynomial_degree, cov="unscaled")
        polynomial = np.poly1d(coefficients)
        derivative = np.polyder(polynomial)
        candidates = []
        for root in np.roots(derivative):
            if abs(float(np.imag(root))) > 1.0e-10:
                continue
            coordinate = float(np.real(root))
            if -1.0 < coordinate < 1.0 and float(np.polyval(np.polyder(polynomial, 2), coordinate)) < 0.0:
                candidates.append(coordinate)
        if not candidates:
            raise ValueError("local polynomial fit has no interior peak")
        peak_z = min(candidates, key=lambda value: abs(value))
        model = lambda coordinate: float(polynomial(coordinate))
        parameter_names = [f"coefficient_degree_{degree}" for degree in range(polynomial_degree, -1, -1)]
        parameter_values = [float(value) for value in coefficients]
        measured_condition = float(np.linalg.cond(np.vander(z, polynomial_degree + 1)))
        design_condition = measured_condition if math.isfinite(measured_condition) else None
        covariance_kind = "unscaled_design"
    elif method == "lorentzian_fit":
        def lorentzian(coordinate, offset, amplitude, center, half_width):
            return offset + amplitude / (1.0 + ((coordinate - center) / half_width) ** 2)

        response_span = float(max(y) - min(y))
        spacing = float(min(np.diff(z)))
        lower = [-10.0, 0.0, -1.0, max(spacing * 1.0e-4, 1.0e-9)]
        upper = [10.0, max(10.0, response_span * 100.0), 1.0, 10.0]
        parameters, covariance = curve_fit(
            lorentzian,
            z,
            y,
            p0=[float(min(y)), max(response_span, 1.0e-9), 0.0, max(spacing, 0.1)],
            bounds=(lower, upper),
            maxfev=max_evaluations,
        )
        model = lambda coordinate: float(lorentzian(coordinate, *parameters))
        peak_z = float(parameters[2])
        parameter_names = ["offset", "amplitude", "center_scaled", "half_width_scaled"]
        parameter_values = [float(value) for value in parameters]
        design_condition = None
        covariance_kind = "curve_fit_estimate"
    elif method == "fano_fit":
        def fano(coordinate, offset, amplitude, center, half_width, asymmetry):
            epsilon = (coordinate - center) / half_width
            return offset + amplitude * (asymmetry + epsilon) ** 2 / (1.0 + epsilon**2)

        response_span = float(max(y) - min(y))
        spacing = float(min(np.diff(z)))
        attempts = []
        for sign, q0 in ((-1.0, -2.0), (1.0, 2.0)):
            q_bounds = (-20.0, -0.05) if sign < 0.0 else (0.05, 20.0)
            try:
                parameters, candidate_covariance = curve_fit(
                    fano,
                    z,
                    y,
                    p0=[float(min(y)), max(response_span / 5.0, 1.0e-9), 0.0, max(spacing, 0.1), q0],
                    bounds=(
                        [-10.0, 0.0, -1.0, max(spacing * 1.0e-4, 1.0e-9), q_bounds[0]],
                        [10.0, max(10.0, response_span * 100.0), 1.0, 10.0, q_bounds[1]],
                    ),
                    maxfev=max_evaluations,
                )
            except (RuntimeError, ValueError):
                continue
            residual = y - fano(z, *parameters)
            attempts.append((float(np.dot(residual, residual)), parameters, candidate_covariance))
        if not attempts:
            raise ValueError("Fano fit failed for both asymmetry branches")
        _best_residual, parameters, covariance = min(attempts, key=lambda item: item[0])
        model = lambda coordinate: float(fano(coordinate, *parameters))
        peak_z = float(parameters[2] + parameters[3] / parameters[4])
        parameter_names = ["offset", "amplitude", "center_scaled", "half_width_scaled", "asymmetry"]
        parameter_values = [float(value) for value in parameters]
        design_condition = None
        covariance_kind = "curve_fit_estimate"
    else:
        raise ValueError("unsupported fit method")

    if not -1.0 < peak_z < 1.0:
        raise ValueError("fitted peak is outside its declared support window")
    peak_oriented = float(model(peak_z))
    measured_fit = np.asarray([model(value) for value in z])
    residuals = y - measured_fit
    half_prominence = baseline + (peak_oriented - baseline) / 2.0
    left_z, right_z = _model_crossings(
        model,
        left_bound=-1.0,
        peak=peak_z,
        right_bound=1.0,
        level=half_prominence,
    )
    covariance_values, covariance_condition = _covariance_diagnostics(covariance)
    peak_wavelength = origin + peak_z * scale
    left_wavelength = None if left_z is None else origin + left_z * scale
    right_wavelength = None if right_z is None else origin + right_z * scale
    width = (
        None
        if left_wavelength is None or right_wavelength is None
        else right_wavelength - left_wavelength
    )
    quality_factor = (
        None if width is None or width <= 0.0 else peak_wavelength / width
    )
    return {
        "support_indices": support_indices,
        "peak_wavelength_m": peak_wavelength,
        "peak_oriented_response": peak_oriented,
        "left_crossing_m": left_wavelength,
        "right_crossing_m": right_wavelength,
        "fwhm_m": width,
        "quality_factor": quality_factor,
        "baseline_oriented_response": baseline,
        "half_prominence_oriented_response": half_prominence,
        "diagnostics": {
            "fit_window_m": [float(x[0]), float(x[-1])],
            "support_point_count": support_count,
            "coordinate_origin_m": origin,
            "coordinate_scale_m": scale,
            "parameter_names": parameter_names,
            "parameter_values": parameter_values,
            "residual_sum_squares": float(np.dot(residuals, residuals)),
            "root_mean_square_residual": float(np.sqrt(np.mean(residuals**2))),
            "covariance": covariance_values,
            "covariance_condition": covariance_condition,
            "covariance_kind": covariance_kind,
            "design_matrix_condition": design_condition,
        },
    }


def build_spectral_characterization(
    bundle: Mapping[str, Any],
    decision: Mapping[str, Any],
    measurement_configuration: Mapping[str, Any],
) -> dict[str, Any]:
    """Measure one classified candidate without changing raw point evidence."""
    normalized = validate_spectral_point_bundle(bundle)
    normalized_decision = validate_spectral_analysis_decision(decision, bundle=normalized)
    configuration = _normalize_measurement_configuration(measurement_configuration)
    rows = normalized["rows"]
    response_name = normalized_decision["analysis_policy"]["response_quantity"]
    polarity = normalized_decision["analysis_policy"]["candidate_polarity"]
    wavelengths = [row["requested_wavelength_m"] for row in rows]
    measured_response = [row[response_name] for row in rows]
    oriented = measured_response if polarity == "maximum" else [-value for value in measured_response]
    evidence_rows = [_row_reference(row) for row in rows]
    configuration_sha256 = _sha256(configuration)

    body: dict[str, Any] = {
        "schema_name": SPECTRAL_CHARACTERIZATION_SCHEMA,
        "schema_version": SPECTRAL_SCHEMA_VERSION,
        "bundle_id": normalized["bundle_id"],
        "bundle_sha256": normalized["bundle_sha256"],
        "decision_sha256": normalized_decision["decision_sha256"],
        "configuration_sha256": normalized["configuration_sha256"],
        "measurement_configuration": configuration,
        "measurement_configuration_sha256": configuration_sha256,
        "algorithm": {
            "implementation": "src.evidence.spectral_characterization",
            "version": SPECTRAL_SCHEMA_VERSION,
        },
        "measurement_state": "not_measured",
        "reason_code": f"classification_{normalized_decision['classification']}",
        "candidate": None,
        "evidence_binding": {
            "analysis_policy_sha256": normalized_decision["analysis_policy_sha256"],
            "measurement_configuration_sha256": configuration_sha256,
            "rows": evidence_rows,
        },
    }
    if normalized_decision["classification"] != "interior_candidate":
        return {**body, "characterization_sha256": _sha256(body)}

    candidate_id = normalized_decision["candidate_row_ids"][0]
    candidate_index = next(
        index for index, row in enumerate(rows) if row["row_id"] == candidate_id
    )
    if configuration["baseline_rule"] == "local_prominence":
        left_floor = min(oriented[: candidate_index + 1])
        right_floor = min(oriented[candidate_index:])
        baseline = max(left_floor, right_floor)
    elif configuration["baseline_rule"] == "window_endpoints_mean":
        baseline = (oriented[0] + oriented[-1]) / 2.0
    else:
        declared_baseline = configuration["baseline_response_value"]
        if declared_baseline is None:
            raise RuntimeError(
                "validated fixed baseline configuration is missing its value"
            )
        baseline = declared_baseline if polarity == "maximum" else -declared_baseline

    fit_methods = {"local_polynomial_fit", "lorentzian_fit", "fano_fit"}
    if configuration["peak_method"] == "measured_grid":
        peak_wavelength = wavelengths[candidate_index]
        peak_oriented_response = oriented[candidate_index]
        support_indices = [candidate_index]
        peak_diagnostics = {
            "interpolation": "none",
            "residual_sum_squares": 0.0,
        }
        fit_result = None
    elif configuration["peak_method"] == "quadratic_interpolation":
        try:
            (
                peak_wavelength,
                peak_oriented_response,
                support_indices,
                peak_diagnostics,
            ) = _quadratic_peak(wavelengths, oriented, candidate_index)
        except ValueError as exc:
            body["reason_code"] = "peak_interpolation_failed"
            body["candidate"] = {"failure_reason": str(exc)}
            return {**body, "characterization_sha256": _sha256(body)}
        fit_result = None
    else:
        if configuration["peak_method"] not in fit_methods:
            raise RuntimeError(
                "validated measurement configuration has an unsupported peak method"
            )
        try:
            fit_result = _fit_candidate(
                method=configuration["peak_method"],
                wavelengths=wavelengths,
                oriented=oriented,
                candidate_index=candidate_index,
                support_count=configuration["fit_support_points"],
                baseline=baseline,
                polynomial_degree=configuration["local_polynomial_degree"],
                max_evaluations=configuration["fit_max_evaluations"],
            )
        except (RuntimeError, TypeError, ValueError, np.linalg.LinAlgError) as exc:
            body["reason_code"] = "peak_fit_failed"
            body["candidate"] = {"failure_reason": str(exc)}
            return {**body, "characterization_sha256": _sha256(body)}
        peak_wavelength = fit_result["peak_wavelength_m"]
        peak_oriented_response = fit_result["peak_oriented_response"]
        support_indices = fit_result["support_indices"]
        peak_diagnostics = fit_result["diagnostics"]

    half_prominence = baseline + (peak_oriented_response - baseline) / 2.0
    if fit_result is None:
        left, right = _crossing_brackets(
            wavelengths, oriented, candidate_index, half_prominence
        )
        left_crossing = None if left is None else left[2]
        right_crossing = None if right is None else right[2]
    else:
        left = right = None
        left_crossing = fit_result["left_crossing_m"]
        right_crossing = fit_result["right_crossing_m"]
    missing_sides = [
        side
        for side, crossing in (("left", left_crossing), ("right", right_crossing))
        if crossing is None
    ]
    fwhm: dict[str, Any]
    quality_factor: dict[str, Any]
    if missing_sides:
        fwhm = {
            "state": "unbracketed",
            "definition": "half_prominence",
            "baseline_oriented_response": baseline,
            "half_prominence_oriented_response": half_prominence,
            "missing_sides": missing_sides,
            "left_crossing_m": left_crossing,
            "right_crossing_m": right_crossing,
            "value_m": None,
        }
        quality_factor = {
            "state": "not_computed",
            "reason_code": "fwhm_unbracketed",
            "value": None,
        }
    else:
        if left_crossing is None or right_crossing is None:
            raise RuntimeError(
                "complete crossing classification is missing a crossing value"
            )
        width = right_crossing - left_crossing
        if not math.isfinite(width) or width <= 0.0:
            raise ValueError("bracketed half-prominence width must be positive and finite")
        fwhm = {
            "state": "bracketed",
            "definition": "half_prominence",
            "baseline_oriented_response": baseline,
            "half_prominence_oriented_response": half_prominence,
            "missing_sides": [],
            "left_crossing_m": left_crossing,
            "right_crossing_m": right_crossing,
            "value_m": width,
            "crossing_rows": (
                [
                    [_row_reference(rows[index]) for index in left[:2]],
                    [_row_reference(rows[index]) for index in right[:2]],
                ]
                if left is not None and right is not None
                else []
            ),
        }
        quality_factor = {
            "state": "computed_from_bracketed_fwhm",
            "reason_code": "bracketed_half_prominence",
            "value": peak_wavelength / width,
        }

    peak_response = (
        peak_oriented_response if polarity == "maximum" else -peak_oriented_response
    )
    sensitivity_measurements = []
    if configuration["peak_method"] in fit_methods:
        for support_count in configuration["fit_support_sensitivity_points"]:
            try:
                measured = _fit_candidate(
                    method=configuration["peak_method"],
                    wavelengths=wavelengths,
                    oriented=oriented,
                    candidate_index=candidate_index,
                    support_count=support_count,
                    baseline=baseline,
                    polynomial_degree=configuration["local_polynomial_degree"],
                    max_evaluations=configuration["fit_max_evaluations"],
                )
                sensitivity_measurements.append(
                    {
                        "support_point_count": support_count,
                        "state": "measured",
                        "peak_wavelength_m": measured["peak_wavelength_m"],
                        "peak_response_value": (
                            measured["peak_oriented_response"]
                            if polarity == "maximum"
                            else -measured["peak_oriented_response"]
                        ),
                        "fwhm_m": measured["fwhm_m"],
                        "quality_factor": measured["quality_factor"],
                        "support_rows": [
                            _row_reference(rows[index])
                            for index in measured["support_indices"]
                        ],
                        "diagnostics": measured["diagnostics"],
                    }
                )
            except (RuntimeError, TypeError, ValueError, np.linalg.LinAlgError) as exc:
                sensitivity_measurements.append(
                    {
                        "support_point_count": support_count,
                        "state": "fit_failed",
                        "failure_reason": str(exc),
                    }
                )
    successful_sensitivity = [
        item for item in sensitivity_measurements if item["state"] == "measured"
    ]

    def measured_span(field: str) -> float | None:
        values = [
            item[field]
            for item in successful_sensitivity
            if item.get(field) is not None
        ]
        return None if len(values) < 2 else max(values) - min(values)

    fit_support_sensitivity = {
        "state": (
            "not_requested"
            if not configuration["fit_support_sensitivity_points"]
            else "measured_not_classified"
        ),
        "measurements": sensitivity_measurements,
        "spans": {
            "peak_wavelength_m": measured_span("peak_wavelength_m"),
            "peak_response_value": measured_span("peak_response_value"),
            "fwhm_m": measured_span("fwhm_m"),
            "quality_factor": measured_span("quality_factor"),
        },
        "policy_authority": False,
    }
    candidate = {
        "candidate_row": _row_reference(rows[candidate_index]),
        "peak": {
            "method": configuration["peak_method"],
            "wavelength_m": peak_wavelength,
            "response_quantity": response_name,
            "response_value": peak_response,
            "support_rows": [_row_reference(rows[index]) for index in support_indices],
            "diagnostics": peak_diagnostics,
        },
        "fwhm": fwhm,
        "quality_factor": quality_factor,
        "fit_support_sensitivity": fit_support_sensitivity,
    }
    body.update(
        {
            "measurement_state": "measured",
            "reason_code": "candidate_measured",
            "candidate": candidate,
        }
    )
    return {**body, "characterization_sha256": _sha256(body)}


def validate_spectral_characterization(
    value: Any,
    *,
    bundle: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> dict[str, Any]:
    """Recompute one characterization and reject noncanonical measurements."""
    item = _mapping(value, "spectral_characterization")
    expected = {
        "schema_name",
        "schema_version",
        "bundle_id",
        "bundle_sha256",
        "decision_sha256",
        "configuration_sha256",
        "measurement_configuration",
        "measurement_configuration_sha256",
        "algorithm",
        "measurement_state",
        "reason_code",
        "candidate",
        "evidence_binding",
        "characterization_sha256",
    }
    if set(item) != expected:
        raise ValueError("spectral characterization fields are invalid")
    rebuilt = build_spectral_characterization(
        bundle, decision, item["measurement_configuration"]
    )
    if item != rebuilt:
        raise ValueError(
            "spectral characterization is noncanonical or its hash does not match"
        )
    return deepcopy(rebuilt)


__all__ = [
    "MAX_SPECTRAL_POINTS",
    "SPECTRAL_BUNDLE_SCHEMA",
    "SPECTRAL_CHARACTERIZATION_SCHEMA",
    "SPECTRAL_DECISION_SCHEMA",
    "SPECTRAL_SCHEMA_VERSION",
    "build_spectral_analysis_decision",
    "build_spectral_characterization",
    "build_spectral_point_bundle",
    "normalize_spectral_analysis_policy",
    "normalize_spectral_measurement_configuration",
    "validate_spectral_analysis_decision",
    "validate_spectral_characterization",
    "validate_spectral_point_bundle",
]
