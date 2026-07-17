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


SPECTRAL_BUNDLE_SCHEMA = "comsol_mcp.spectral_point_bundle"
SPECTRAL_DECISION_SCHEMA = "comsol_mcp.spectral_analysis_decision"
SPECTRAL_CHARACTERIZATION_SCHEMA = "comsol_mcp.spectral_characterization"
SPECTRAL_SCHEMA_VERSION = "1.0.0"
MAX_SPECTRAL_POINTS = 4096
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
    if item["peak_method"] not in {"measured_grid", "quadratic_interpolation"}:
        raise ValueError(
            "measurement_configuration.peak_method must be measured_grid or quadratic_interpolation"
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
    return {
        "peak_method": item["peak_method"],
        "baseline_rule": item["baseline_rule"],
        "baseline_response_value": declared_baseline,
        "fwhm_definition": item["fwhm_definition"],
    }


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
    if configuration["peak_method"] == "measured_grid":
        peak_wavelength = wavelengths[candidate_index]
        peak_oriented_response = oriented[candidate_index]
        support_indices = [candidate_index]
        peak_diagnostics = {
            "interpolation": "none",
            "residual_sum_squares": 0.0,
        }
    else:
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

    if configuration["baseline_rule"] == "local_prominence":
        left_floor = min(oriented[: candidate_index + 1])
        right_floor = min(oriented[candidate_index:])
        baseline = max(left_floor, right_floor)
    elif configuration["baseline_rule"] == "window_endpoints_mean":
        baseline = (oriented[0] + oriented[-1]) / 2.0
    else:
        declared_baseline = configuration["baseline_response_value"]
        assert declared_baseline is not None
        baseline = declared_baseline if polarity == "maximum" else -declared_baseline
    half_prominence = baseline + (peak_oriented_response - baseline) / 2.0
    left, right = _crossing_brackets(
        wavelengths, oriented, candidate_index, half_prominence
    )
    missing_sides = [
        side for side, crossing in (("left", left), ("right", right)) if crossing is None
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
            "left_crossing_m": None if left is None else left[2],
            "right_crossing_m": None if right is None else right[2],
            "value_m": None,
        }
        quality_factor = {
            "state": "not_computed",
            "reason_code": "fwhm_unbracketed",
            "value": None,
        }
    else:
        assert left is not None and right is not None
        width = right[2] - left[2]
        if not math.isfinite(width) or width <= 0.0:
            raise ValueError("bracketed half-prominence width must be positive and finite")
        fwhm = {
            "state": "bracketed",
            "definition": "half_prominence",
            "baseline_oriented_response": baseline,
            "half_prominence_oriented_response": half_prominence,
            "missing_sides": [],
            "left_crossing_m": left[2],
            "right_crossing_m": right[2],
            "value_m": width,
            "crossing_rows": [
                [_row_reference(rows[index]) for index in left[:2]],
                [_row_reference(rows[index]) for index in right[:2]],
            ],
        }
        quality_factor = {
            "state": "computed_from_bracketed_fwhm",
            "reason_code": "bracketed_half_prominence",
            "value": peak_wavelength / width,
        }

    peak_response = (
        peak_oriented_response if polarity == "maximum" else -peak_oriented_response
    )
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
    "validate_spectral_analysis_decision",
    "validate_spectral_characterization",
    "validate_spectral_point_bundle",
]
