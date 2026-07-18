"""Solver-free convergence evaluation over ordered spectral evidence levels."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import PurePosixPath
import re
from typing import Any, Mapping

from comsol_mcp.evidence.spectral_characterization import (
    validate_spectral_analysis_decision,
    validate_spectral_characterization,
    validate_spectral_point_bundle,
)


CONVERGENCE_LADDER_SCHEMA = "comsol_mcp.convergence_ladder"
CONVERGENCE_EVALUATION_SCHEMA = "comsol_mcp.convergence_evaluation"
CONVERGENCE_SCHEMA_VERSION = "1.0.0"
MAX_CONVERGENCE_LEVELS = 32
MAX_OPTIONAL_METRICS = 32

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_LEVEL_INPUT_FIELDS = {
    "level_id", "ordinal", "declared_predecessor_level_id",
    "source_model_sha256", "configuration_sha256", "mesh_counts",
    "material_identity_sha256", "incidence_identity_sha256",
    "spectral_bundle", "analysis_decision", "candidate_measurements",
    "optional_field_metrics", "fixed_reference_diagnostics",
}
_MESH_FIELDS = {"element_count", "vertex_count"}
_METRIC_FIELDS = {"value", "unit", "evidence_artifact_sha256"}
_LEVEL_SUMMARY_FIELDS = {
    "level_id", "ordinal", "declared_predecessor_level_id", "source_model",
    "configuration_sha256", "mesh_counts", "material_identity_sha256",
    "incidence_identity_sha256", "spectral_artifacts", "evidence_state",
    "measurements", "fit_support_sensitivity", "optional_field_metrics",
    "fixed_reference_diagnostics", "level_sha256",
}
_POLICY_FIELDS = {
    "policy_id", "metrics", "minimum_level_count", "governing_pairs",
    "relative_denominator", "declared_cap_reached",
}
_RULE_FIELDS = {"metric", "unit", "absolute_tolerance", "relative_tolerance"}
_BUILTIN_METRIC_UNITS = {
    "peak_wavelength_m": "m",
    "peak_response_value": "1",
    "fwhm_m": "m",
    "quality_factor": "1",
}


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("convergence evidence must contain finite JSON values") from exc


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


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _positive_count(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _bounded_text(value: Any, label: str, maximum: int = 128) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{label} must be nonempty and at most {maximum} characters")
    return value


def _relative_identity(value: Any, label: str) -> str:
    text = _bounded_text(value, label, 512).replace("\\", "/")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or re.match(r"^[A-Za-z]:", text):
        raise ValueError(f"{label} must be relative and traversal-free")
    return text


def _normalize_mesh_counts(value: Any, label: str) -> dict[str, int]:
    item = _exact_fields(value, _MESH_FIELDS, label)
    return {
        "element_count": _positive_count(item["element_count"], f"{label}.element_count"),
        "vertex_count": _positive_count(item["vertex_count"], f"{label}.vertex_count"),
    }


def _normalize_metric_mapping(value: Any, label: str) -> dict[str, dict[str, Any]]:
    item = _mapping(value, label)
    if len(item) > MAX_OPTIONAL_METRICS:
        raise ValueError(f"{label} exceeds its metric count limit")
    normalized = {}
    for name in sorted(item):
        metric_name = _identifier(name, f"{label} metric name")
        metric = _exact_fields(item[name], _METRIC_FIELDS, f"{label}.{name}")
        normalized[metric_name] = {
            "value": _finite(metric["value"], f"{label}.{name}.value"),
            "unit": _bounded_text(metric["unit"], f"{label}.{name}.unit"),
            "evidence_artifact_sha256": _hash(
                metric["evidence_artifact_sha256"],
                f"{label}.{name}.evidence_artifact_sha256",
            ),
        }
    return normalized


def _normalize_sensitivity(value: Any, label: str) -> dict[str, Any]:
    item = _mapping(value, label)
    if set(item) != {"state", "measurements", "spans", "policy_authority"}:
        raise ValueError(f"{label} fields are invalid")
    measurements = item["measurements"]
    if not isinstance(measurements, list) or len(measurements) > 16:
        raise ValueError(f"{label}.measurements must be a bounded list")
    normalized_measurements = []
    for index, measurement in enumerate(measurements):
        measurement_label = f"{label}.measurements[{index}]"
        entry = _mapping(measurement, measurement_label)
        state = entry.get("state")
        support_count = _positive_count(
            entry.get("support_point_count"), f"{measurement_label}.support_point_count"
        )
        if state == "fit_failed":
            if set(entry) != {"support_point_count", "state", "failure_reason"}:
                raise ValueError(f"{measurement_label} failure fields are invalid")
            normalized_measurements.append({
                "support_point_count": support_count,
                "state": state,
                "failure_reason": _bounded_text(
                    entry["failure_reason"], f"{measurement_label}.failure_reason", 2048
                ),
            })
            continue
        expected = {
            "support_point_count", "state", "peak_wavelength_m",
            "peak_response_value", "fwhm_m", "quality_factor",
            "support_rows", "diagnostics",
        }
        if state != "measured" or set(entry) != expected:
            raise ValueError(f"{measurement_label} measured fields are invalid")
        support_rows = entry["support_rows"]
        if not isinstance(support_rows, list) or not support_rows:
            raise ValueError(f"{measurement_label}.support_rows must be nonempty")
        hashes = []
        for row_index, row in enumerate(support_rows):
            if not isinstance(row, Mapping) or "raw_row_sha256" not in row:
                raise ValueError(f"{measurement_label}.support_rows[{row_index}] is invalid")
            hashes.append(_hash(
                row["raw_row_sha256"],
                f"{measurement_label}.support_rows[{row_index}].raw_row_sha256",
            ))
        normalized_measurements.append({
            "support_point_count": support_count,
            "state": state,
            "peak_wavelength_m": _finite(
                entry["peak_wavelength_m"], f"{measurement_label}.peak_wavelength_m"
            ),
            "peak_response_value": _finite(
                entry["peak_response_value"], f"{measurement_label}.peak_response_value"
            ),
            "fwhm_m": None if entry["fwhm_m"] is None else _finite(
                entry["fwhm_m"], f"{measurement_label}.fwhm_m"
            ),
            "quality_factor": None if entry["quality_factor"] is None else _finite(
                entry["quality_factor"], f"{measurement_label}.quality_factor"
            ),
            "support_row_hashes": hashes,
        })
    counts = [entry["support_point_count"] for entry in normalized_measurements]
    if counts != sorted(counts) or len(counts) != len(set(counts)):
        raise ValueError(f"{label} support counts must be sorted and unique")
    return {
        "state": _bounded_text(item["state"], f"{label}.state"),
        "measurements": normalized_measurements,
        "policy_authority": item["policy_authority"] is True,
    }


def _summarize_level(value: Any, expected_ordinal: int) -> dict[str, Any]:
    label = f"levels[{expected_ordinal}]"
    item = _exact_fields(value, _LEVEL_INPUT_FIELDS, label)
    ordinal = item["ordinal"]
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal != expected_ordinal:
        raise ValueError(f"{label}.ordinal must match list order")
    level_id = _identifier(item["level_id"], f"{label}.level_id")
    predecessor = item["declared_predecessor_level_id"]
    if predecessor is not None:
        predecessor = _identifier(predecessor, f"{label}.declared_predecessor_level_id")
    source_hash = _hash(item["source_model_sha256"], f"{label}.source_model_sha256")
    configuration_hash = _hash(
        item["configuration_sha256"], f"{label}.configuration_sha256"
    )
    bundle = validate_spectral_point_bundle(item["spectral_bundle"])
    decision = validate_spectral_analysis_decision(item["analysis_decision"], bundle=bundle)
    characterization = validate_spectral_characterization(
        item["candidate_measurements"], bundle=bundle, decision=decision
    )
    if bundle["source_model"]["sha256"] != source_hash:
        raise ValueError(f"{label} source model hash does not match its spectral bundle")
    if bundle["configuration_sha256"] != configuration_hash:
        raise ValueError(f"{label} configuration hash does not match its spectral bundle")

    candidate = characterization["candidate"]
    measured = characterization["measurement_state"] == "measured" and candidate is not None
    fwhm = candidate["fwhm"] if measured else None
    quality = candidate["quality_factor"] if measured else None
    measurements = {
        "peak_wavelength_m": candidate["peak"]["wavelength_m"] if measured else None,
        "peak_response_value": candidate["peak"]["response_value"] if measured else None,
        "fwhm_m": (
            fwhm["value_m"] if fwhm is not None and fwhm["state"] == "bracketed" else None
        ),
        "quality_factor": (
            quality["value"]
            if quality is not None and quality["state"] == "computed_from_bracketed_fwhm"
            else None
        ),
    }
    sensitivity = (
        _normalize_sensitivity(
            candidate["fit_support_sensitivity"], f"{label}.fit_support_sensitivity"
        )
        if measured else {"state": "unavailable", "measurements": [], "policy_authority": False}
    )
    body = {
        "level_id": level_id,
        "ordinal": ordinal,
        "declared_predecessor_level_id": predecessor,
        "source_model": {
            "relative_identity": bundle["source_model"]["relative_identity"],
            "sha256": source_hash,
        },
        "configuration_sha256": configuration_hash,
        "mesh_counts": _normalize_mesh_counts(item["mesh_counts"], f"{label}.mesh_counts"),
        "material_identity_sha256": _hash(
            item["material_identity_sha256"], f"{label}.material_identity_sha256"
        ),
        "incidence_identity_sha256": _hash(
            item["incidence_identity_sha256"], f"{label}.incidence_identity_sha256"
        ),
        "spectral_artifacts": {
            "bundle_sha256": bundle["bundle_sha256"],
            "decision_sha256": decision["decision_sha256"],
            "characterization_sha256": characterization["characterization_sha256"],
            "analysis_policy_sha256": decision["analysis_policy_sha256"],
            "measurement_configuration_sha256": characterization["measurement_configuration_sha256"],
            "raw_row_sha256s": [row["raw_row_sha256"] for row in bundle["rows"]],
        },
        "evidence_state": (
            "complete_own_peak" if all(value is not None for value in measurements.values())
            else "incomplete_own_peak"
        ),
        "measurements": measurements,
        "fit_support_sensitivity": sensitivity,
        "optional_field_metrics": _normalize_metric_mapping(
            item["optional_field_metrics"], f"{label}.optional_field_metrics"
        ),
        "fixed_reference_diagnostics": _normalize_metric_mapping(
            item["fixed_reference_diagnostics"], f"{label}.fixed_reference_diagnostics"
        ),
    }
    return {**body, "level_sha256": _sha256(body)}


def _validate_level_summary(value: Any, expected_ordinal: int) -> dict[str, Any]:
    label = f"ladder.levels[{expected_ordinal}]"
    item = _exact_fields(value, _LEVEL_SUMMARY_FIELDS, label)
    ordinal = item["ordinal"]
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal != expected_ordinal:
        raise ValueError(f"{label}.ordinal must match list order")
    level_id = _identifier(item["level_id"], f"{label}.level_id")
    predecessor = item["declared_predecessor_level_id"]
    if predecessor is not None:
        predecessor = _identifier(predecessor, f"{label}.declared_predecessor_level_id")
    source = _exact_fields(
        item["source_model"], {"relative_identity", "sha256"}, f"{label}.source_model"
    )
    normalized_source = {
        "relative_identity": _relative_identity(
            source["relative_identity"], f"{label}.source_model.relative_identity"
        ),
        "sha256": _hash(source["sha256"], f"{label}.source_model.sha256"),
    }
    artifacts = _exact_fields(
        item["spectral_artifacts"],
        {
            "bundle_sha256", "decision_sha256", "characterization_sha256",
            "analysis_policy_sha256", "measurement_configuration_sha256",
            "raw_row_sha256s",
        },
        f"{label}.spectral_artifacts",
    )
    raw_hashes = artifacts["raw_row_sha256s"]
    if not isinstance(raw_hashes, list) or not 3 <= len(raw_hashes) <= 1024:
        raise ValueError(f"{label}.spectral_artifacts.raw_row_sha256s is invalid")
    normalized_raw_hashes = [
        _hash(value, f"{label}.spectral_artifacts.raw_row_sha256s[{index}]")
        for index, value in enumerate(raw_hashes)
    ]
    if len(normalized_raw_hashes) != len(set(normalized_raw_hashes)):
        raise ValueError(f"{label}.spectral_artifacts contains duplicate raw row hashes")
    normalized_artifacts = {
        name: _hash(artifacts[name], f"{label}.spectral_artifacts.{name}")
        for name in (
            "bundle_sha256", "decision_sha256", "characterization_sha256",
            "analysis_policy_sha256", "measurement_configuration_sha256",
        )
    }
    normalized_artifacts["raw_row_sha256s"] = normalized_raw_hashes
    evidence_state = item["evidence_state"]
    if evidence_state not in {"complete_own_peak", "incomplete_own_peak"}:
        raise ValueError(f"{label}.evidence_state is invalid")
    measurements = _exact_fields(
        item["measurements"], set(_BUILTIN_METRIC_UNITS), f"{label}.measurements"
    )
    normalized_measurements = {
        name: None if measurements[name] is None else _finite(
            measurements[name], f"{label}.measurements.{name}"
        )
        for name in _BUILTIN_METRIC_UNITS
    }
    complete = all(value is not None for value in normalized_measurements.values())
    if (evidence_state == "complete_own_peak") != complete:
        raise ValueError(f"{label}.evidence_state does not match its measurements")
    sensitivity = _mapping(
        item["fit_support_sensitivity"], f"{label}.fit_support_sensitivity"
    )
    if set(sensitivity) != {"state", "measurements", "policy_authority"}:
        raise ValueError(f"{label}.fit_support_sensitivity fields are invalid")
    if sensitivity["state"] not in {
        "not_requested", "measured_not_classified", "unavailable"
    }:
        raise ValueError(f"{label}.fit_support_sensitivity.state is invalid")
    if not isinstance(sensitivity["policy_authority"], bool) or sensitivity["policy_authority"]:
        raise ValueError(f"{label}.fit_support_sensitivity cannot have policy authority")
    sensitivity_measurements = sensitivity["measurements"]
    if not isinstance(sensitivity_measurements, list) or len(sensitivity_measurements) > 16:
        raise ValueError(f"{label}.fit_support_sensitivity.measurements is invalid")
    normalized_sensitivity_measurements = []
    for index, measurement in enumerate(sensitivity_measurements):
        measurement_label = f"{label}.fit_support_sensitivity.measurements[{index}]"
        entry = _mapping(measurement, measurement_label)
        support_count = _positive_count(
            entry.get("support_point_count"), f"{measurement_label}.support_point_count"
        )
        if entry.get("state") == "fit_failed":
            if set(entry) != {"support_point_count", "state", "failure_reason"}:
                raise ValueError(f"{measurement_label} failure fields are invalid")
            normalized_sensitivity_measurements.append({
                "support_point_count": support_count,
                "state": "fit_failed",
                "failure_reason": _bounded_text(
                    entry["failure_reason"], f"{measurement_label}.failure_reason", 2048
                ),
            })
            continue
        expected_measurement = {
            "support_point_count", "state", "peak_wavelength_m",
            "peak_response_value", "fwhm_m", "quality_factor",
            "support_row_hashes",
        }
        if entry.get("state") != "measured" or set(entry) != expected_measurement:
            raise ValueError(f"{measurement_label} measured fields are invalid")
        support_hashes = entry["support_row_hashes"]
        if not isinstance(support_hashes, list) or not support_hashes:
            raise ValueError(f"{measurement_label}.support_row_hashes is invalid")
        normalized_sensitivity_measurements.append({
            "support_point_count": support_count,
            "state": "measured",
            "peak_wavelength_m": _finite(
                entry["peak_wavelength_m"], f"{measurement_label}.peak_wavelength_m"
            ),
            "peak_response_value": _finite(
                entry["peak_response_value"], f"{measurement_label}.peak_response_value"
            ),
            "fwhm_m": None if entry["fwhm_m"] is None else _finite(
                entry["fwhm_m"], f"{measurement_label}.fwhm_m"
            ),
            "quality_factor": None if entry["quality_factor"] is None else _finite(
                entry["quality_factor"], f"{measurement_label}.quality_factor"
            ),
            "support_row_hashes": [
                _hash(digest, f"{measurement_label}.support_row_hashes")
                for digest in support_hashes
            ],
        })
    support_counts = [
        entry["support_point_count"] for entry in normalized_sensitivity_measurements
    ]
    if support_counts != sorted(support_counts) or len(support_counts) != len(set(support_counts)):
        raise ValueError(f"{label}.fit_support_sensitivity support counts are invalid")
    normalized_sensitivity = {
        "state": sensitivity["state"],
        "measurements": normalized_sensitivity_measurements,
        "policy_authority": False,
    }
    body = {
        "level_id": level_id,
        "ordinal": ordinal,
        "declared_predecessor_level_id": predecessor,
        "source_model": normalized_source,
        "configuration_sha256": _hash(
            item["configuration_sha256"], f"{label}.configuration_sha256"
        ),
        "mesh_counts": _normalize_mesh_counts(item["mesh_counts"], f"{label}.mesh_counts"),
        "material_identity_sha256": _hash(
            item["material_identity_sha256"], f"{label}.material_identity_sha256"
        ),
        "incidence_identity_sha256": _hash(
            item["incidence_identity_sha256"], f"{label}.incidence_identity_sha256"
        ),
        "spectral_artifacts": normalized_artifacts,
        "evidence_state": evidence_state,
        "measurements": normalized_measurements,
        "fit_support_sensitivity": normalized_sensitivity,
        "optional_field_metrics": _normalize_metric_mapping(
            item["optional_field_metrics"], f"{label}.optional_field_metrics"
        ),
        "fixed_reference_diagnostics": _normalize_metric_mapping(
            item["fixed_reference_diagnostics"], f"{label}.fixed_reference_diagnostics"
        ),
    }
    supplied_hash = _hash(item["level_sha256"], f"{label}.level_sha256")
    rebuilt = {**body, "level_sha256": _sha256(body)}
    if rebuilt["level_sha256"] != supplied_hash or rebuilt != item:
        raise ValueError(f"{label} is noncanonical or its hash does not match")
    return rebuilt


def _validate_ladder_invariants(levels: list[dict[str, Any]]) -> None:
    collections = (
        ("level IDs", [level["level_id"] for level in levels]),
        ("configuration hashes", [level["configuration_sha256"] for level in levels]),
        ("spectral bundle hashes", [level["spectral_artifacts"]["bundle_sha256"] for level in levels]),
        ("spectral characterization hashes", [
            level["spectral_artifacts"]["characterization_sha256"] for level in levels
        ]),
    )
    for label, values in collections:
        if len(values) != len(set(values)):
            raise ValueError(f"convergence ladder contains duplicate {label}")
    for index, level in enumerate(levels):
        expected = None if index == 0 else levels[index - 1]["level_id"]
        if level["declared_predecessor_level_id"] != expected:
            raise ValueError("declared level adjacency does not match list order")
    if len({level["material_identity_sha256"] for level in levels}) != 1:
        raise ValueError("material identity must remain consistent across the ladder")
    if len({level["incidence_identity_sha256"] for level in levels}) != 1:
        raise ValueError("incidence identity must remain consistent across the ladder")


def build_convergence_ladder(
    *, ladder_id: str, levels: list[Mapping[str, Any]]
) -> dict[str, Any]:
    """Build one ordered immutable ladder from complete spectral artifact triples."""
    if not isinstance(levels, list) or not 2 <= len(levels) <= MAX_CONVERGENCE_LEVELS:
        raise ValueError(f"levels must contain 2..{MAX_CONVERGENCE_LEVELS} entries")
    normalized = [_summarize_level(level, index) for index, level in enumerate(levels)]
    _validate_ladder_invariants(normalized)
    body = {
        "schema_name": CONVERGENCE_LADDER_SCHEMA,
        "schema_version": CONVERGENCE_SCHEMA_VERSION,
        "ladder_id": _identifier(ladder_id, "ladder_id"),
        "level_count": len(normalized),
        "material_identity_sha256": normalized[0]["material_identity_sha256"],
        "incidence_identity_sha256": normalized[0]["incidence_identity_sha256"],
        "levels": normalized,
    }
    return {**body, "ladder_sha256": _sha256(body)}


def validate_convergence_ladder(value: Any) -> dict[str, Any]:
    """Validate a canonical convergence ladder without reading external files."""
    item = _mapping(value, "convergence_ladder")
    expected = {
        "schema_name", "schema_version", "ladder_id", "level_count",
        "material_identity_sha256", "incidence_identity_sha256", "levels",
        "ladder_sha256",
    }
    if set(item) != expected:
        raise ValueError("convergence ladder fields are invalid")
    if item["schema_name"] != CONVERGENCE_LADDER_SCHEMA or item["schema_version"] != CONVERGENCE_SCHEMA_VERSION:
        raise ValueError("convergence ladder schema is unsupported")
    levels = item["levels"]
    if (
        not isinstance(levels, list)
        or not 2 <= len(levels) <= MAX_CONVERGENCE_LEVELS
        or item["level_count"] != len(levels)
    ):
        raise ValueError("convergence ladder level count is invalid")
    normalized = [_validate_level_summary(level, index) for index, level in enumerate(levels)]
    _validate_ladder_invariants(normalized)
    if item["material_identity_sha256"] != normalized[0]["material_identity_sha256"]:
        raise ValueError("ladder material identity does not match its levels")
    if item["incidence_identity_sha256"] != normalized[0]["incidence_identity_sha256"]:
        raise ValueError("ladder incidence identity does not match its levels")
    supplied_hash = _hash(item["ladder_sha256"], "ladder.ladder_sha256")
    body = dict(item)
    body.pop("ladder_sha256")
    if _sha256(body) != supplied_hash:
        raise ValueError("convergence ladder hash does not match")
    return deepcopy(item)


def _nonnegative_optional(value: Any, label: str) -> float | None:
    if value is None:
        return None
    result = _finite(value, label)
    if result < 0.0:
        raise ValueError(f"{label} must be nonnegative")
    return result


def _metric_value(level: Mapping[str, Any], metric: str) -> tuple[float | None, str | None]:
    if metric in _BUILTIN_METRIC_UNITS:
        value = level["measurements"].get(metric)
        return value, _BUILTIN_METRIC_UNITS[metric]
    if metric.startswith("field:"):
        name = metric.split(":", 1)[1]
        record = level["optional_field_metrics"].get(name)
        if record is None:
            return None, None
        return record["value"], record["unit"]
    raise ValueError(f"unsupported convergence metric: {metric}")


def _normalize_convergence_policy(
    value: Any, *, ladder: Mapping[str, Any]
) -> dict[str, Any]:
    item = _exact_fields(value, _POLICY_FIELDS, "convergence_policy")
    metrics = item["metrics"]
    if not isinstance(metrics, list) or not 1 <= len(metrics) <= MAX_OPTIONAL_METRICS:
        raise ValueError("convergence_policy.metrics must be a bounded nonempty list")
    normalized_rules = []
    for index, rule_value in enumerate(metrics):
        label = f"convergence_policy.metrics[{index}]"
        rule = _exact_fields(rule_value, _RULE_FIELDS, label)
        metric = _bounded_text(rule["metric"], f"{label}.metric")
        if metric.startswith("diagnostic:") or metric.startswith("fixed_reference"):
            raise ValueError("fixed-reference diagnostics cannot govern convergence")
        if metric not in _BUILTIN_METRIC_UNITS and not metric.startswith("field:"):
            raise ValueError(f"unsupported convergence metric: {metric}")
        if metric.startswith("field:"):
            field_name = metric.split(":", 1)[1]
            _identifier(field_name, f"{label}.metric field name")
        unit = _bounded_text(rule["unit"], f"{label}.unit")
        absolute = _nonnegative_optional(
            rule["absolute_tolerance"], f"{label}.absolute_tolerance"
        )
        relative = _nonnegative_optional(
            rule["relative_tolerance"], f"{label}.relative_tolerance"
        )
        if absolute is None and relative is None:
            raise ValueError(f"{label} must declare an absolute and/or relative tolerance")
        for level in ladder["levels"]:
            _value, observed_unit = _metric_value(level, metric)
            if observed_unit is not None and observed_unit != unit:
                raise ValueError(f"{label}.unit does not match ladder evidence")
        normalized_rules.append({
            "metric": metric,
            "unit": unit,
            "absolute_tolerance": absolute,
            "relative_tolerance": relative,
        })
    names = [rule["metric"] for rule in normalized_rules]
    if len(names) != len(set(names)):
        raise ValueError("convergence policy metrics must be unique")
    minimum = item["minimum_level_count"]
    if (
        isinstance(minimum, bool) or not isinstance(minimum, int)
        or not 2 <= minimum <= MAX_CONVERGENCE_LEVELS
    ):
        raise ValueError("convergence_policy.minimum_level_count is out of bounds")
    if item["governing_pairs"] not in {"all_adjacent", "final_pair"}:
        raise ValueError("convergence_policy.governing_pairs is unsupported")
    if item["relative_denominator"] not in {"previous_abs", "maximum_abs"}:
        raise ValueError("convergence_policy.relative_denominator is unsupported")
    if not isinstance(item["declared_cap_reached"], bool):
        raise ValueError("convergence_policy.declared_cap_reached must be boolean")
    return {
        "policy_id": _identifier(item["policy_id"], "convergence_policy.policy_id"),
        "metrics": normalized_rules,
        "minimum_level_count": minimum,
        "governing_pairs": item["governing_pairs"],
        "relative_denominator": item["relative_denominator"],
        "declared_cap_reached": item["declared_cap_reached"],
    }


def _relative_change(
    previous: float, current: float, absolute_change: float, convention: str
) -> float | None:
    denominator = (
        abs(previous) if convention == "previous_abs"
        else max(abs(previous), abs(current))
    )
    if denominator == 0.0:
        return 0.0 if absolute_change == 0.0 else None
    return absolute_change / denominator


def _fit_support_comparison(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    rule: Mapping[str, Any],
    relative_denominator: str,
    primary_passed: bool,
) -> dict[str, Any]:
    metric = rule["metric"]
    if metric not in _BUILTIN_METRIC_UNITS:
        return {
            "state": "not_applicable",
            "common_support_point_counts": [],
            "comparisons": [],
            "outcome_changed_by_support": False,
            "policy_authority": False,
        }
    previous_by_count = {
        item["support_point_count"]: item
        for item in previous["fit_support_sensitivity"]["measurements"]
        if item["state"] == "measured" and item.get(metric) is not None
    }
    current_by_count = {
        item["support_point_count"]: item
        for item in current["fit_support_sensitivity"]["measurements"]
        if item["state"] == "measured" and item.get(metric) is not None
    }
    common = sorted(set(previous_by_count) & set(current_by_count))
    comparisons = []
    for count in common:
        previous_value = float(previous_by_count[count][metric])
        current_value = float(current_by_count[count][metric])
        absolute_change = abs(current_value - previous_value)
        relative_change = _relative_change(
            previous_value, current_value, absolute_change, relative_denominator
        )
        absolute_passed = (
            None if rule["absolute_tolerance"] is None
            else absolute_change <= rule["absolute_tolerance"]
        )
        relative_passed = (
            None if rule["relative_tolerance"] is None
            else relative_change is not None and relative_change <= rule["relative_tolerance"]
        )
        declared = [
            outcome for outcome in (absolute_passed, relative_passed)
            if outcome is not None
        ]
        comparisons.append({
            "support_point_count": count,
            "previous_value": previous_value,
            "current_value": current_value,
            "absolute_change": absolute_change,
            "relative_change": relative_change,
            "absolute_passed": absolute_passed,
            "relative_passed": relative_passed,
            "passed": all(declared),
        })
    outcomes = [item["passed"] for item in comparisons]
    changed = bool(outcomes) and (
        any(outcome != primary_passed for outcome in outcomes)
        or len(set(outcomes)) > 1
    )
    return {
        "state": "compared" if comparisons else "not_available",
        "common_support_point_counts": common,
        "comparisons": comparisons,
        "outcome_changed_by_support": changed,
        "policy_authority": False,
    }


def _pair_comparison(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    rule: Mapping[str, Any],
    relative_denominator: str,
) -> dict[str, Any]:
    metric = rule["metric"]
    previous_value, previous_unit = _metric_value(previous, metric)
    current_value, current_unit = _metric_value(current, metric)
    evidence_complete = (
        previous_value is not None
        and current_value is not None
        and previous_unit == current_unit == rule["unit"]
    )
    if not evidence_complete:
        return {
            "metric": metric,
            "unit": rule["unit"],
            "previous_value": previous_value,
            "current_value": current_value,
            "absolute_change": None,
            "relative_change": None,
            "absolute_passed": None,
            "relative_passed": None,
            "passed": False,
            "evidence_complete": False,
            "previous_level_sha256": previous["level_sha256"],
            "current_level_sha256": current["level_sha256"],
            "fit_support_sensitivity": {
                "state": "not_evaluated_incomplete_primary_evidence",
                "common_support_point_counts": [],
                "comparisons": [],
                "outcome_changed_by_support": False,
                "policy_authority": False,
            },
        }
    absolute_change = abs(float(current_value) - float(previous_value))
    relative_change = _relative_change(
        float(previous_value), float(current_value), absolute_change,
        relative_denominator,
    )
    absolute_passed = (
        None if rule["absolute_tolerance"] is None
        else absolute_change <= rule["absolute_tolerance"]
    )
    relative_passed = (
        None if rule["relative_tolerance"] is None
        else relative_change is not None and relative_change <= rule["relative_tolerance"]
    )
    declared_checks = [
        result for result in (absolute_passed, relative_passed) if result is not None
    ]
    passed = all(declared_checks)
    return {
        "metric": metric,
        "unit": rule["unit"],
        "previous_value": previous_value,
        "current_value": current_value,
        "absolute_change": absolute_change,
        "relative_change": relative_change,
        "absolute_passed": absolute_passed,
        "relative_passed": relative_passed,
        "passed": passed,
        "evidence_complete": True,
        "previous_level_sha256": previous["level_sha256"],
        "current_level_sha256": current["level_sha256"],
        "fit_support_sensitivity": _fit_support_comparison(
            previous, current, rule, relative_denominator, passed
        ),
    }


def _monotonicity_observations(
    levels: list[Mapping[str, Any]], rules: list[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    observations = []
    for rule in rules:
        values = [_metric_value(level, rule["metric"])[0] for level in levels]
        if any(value is None for value in values):
            state = "unavailable"
            differences = []
        else:
            numeric = [float(value) for value in values]
            differences = [current - previous for previous, current in zip(numeric, numeric[1:])]
            if all(change == 0.0 for change in differences):
                state = "constant"
            elif all(change >= 0.0 for change in differences):
                state = "nondecreasing"
            elif all(change <= 0.0 for change in differences):
                state = "nonincreasing"
            else:
                state = "non_monotonic"
        observations.append({
            "metric": rule["metric"],
            "unit": rule["unit"],
            "state": state,
            "adjacent_signed_changes": differences,
            "convergence_proof": False,
            "policy_authority": False,
        })
    for metric in ("element_count", "vertex_count"):
        values = [level["mesh_counts"][metric] for level in levels]
        differences = [current - previous for previous, current in zip(values, values[1:])]
        state = (
            "constant" if all(change == 0 for change in differences)
            else "nondecreasing" if all(change >= 0 for change in differences)
            else "nonincreasing" if all(change <= 0 for change in differences)
            else "non_monotonic"
        )
        observations.append({
            "metric": f"mesh:{metric}",
            "unit": "count",
            "state": state,
            "adjacent_signed_changes": differences,
            "convergence_proof": False,
            "policy_authority": False,
        })
    return observations


def _fixed_reference_pair_diagnostics(
    levels: list[Mapping[str, Any]], relative_denominator: str
) -> list[dict[str, Any]]:
    diagnostics = []
    for index in range(1, len(levels)):
        previous = levels[index - 1]
        current = levels[index]
        names = sorted(
            set(previous["fixed_reference_diagnostics"])
            | set(current["fixed_reference_diagnostics"])
        )
        comparisons = []
        for name in names:
            previous_record = previous["fixed_reference_diagnostics"].get(name)
            current_record = current["fixed_reference_diagnostics"].get(name)
            complete = (
                previous_record is not None
                and current_record is not None
                and previous_record["unit"] == current_record["unit"]
            )
            absolute_change = (
                abs(current_record["value"] - previous_record["value"])
                if complete else None
            )
            relative_change = (
                _relative_change(
                    previous_record["value"], current_record["value"],
                    absolute_change, relative_denominator,
                )
                if complete else None
            )
            comparisons.append({
                "diagnostic": name,
                "unit": previous_record["unit"] if previous_record is not None else (
                    current_record["unit"] if current_record is not None else None
                ),
                "previous_value": previous_record["value"] if previous_record else None,
                "current_value": current_record["value"] if current_record else None,
                "absolute_change": absolute_change,
                "relative_change": relative_change,
                "evidence_complete": complete,
                "diagnostic_only": True,
                "policy_authority": False,
            })
        diagnostics.append({
            "pair_index": index - 1,
            "previous_level_id": previous["level_id"],
            "current_level_id": current["level_id"],
            "comparisons": comparisons,
            "governs_convergence": False,
        })
    return diagnostics


def evaluate_convergence(
    ladder: Mapping[str, Any], convergence_policy: Mapping[str, Any]
) -> dict[str, Any]:
    """Compare adjacent own-peak evidence under one caller-supplied policy."""
    normalized_ladder = validate_convergence_ladder(ladder)
    policy = _normalize_convergence_policy(
        convergence_policy, ladder=normalized_ladder
    )
    levels = normalized_ladder["levels"]
    pairs = []
    for index in range(1, len(levels)):
        previous = levels[index - 1]
        current = levels[index]
        comparisons = [
            _pair_comparison(
                previous, current, rule, policy["relative_denominator"]
            )
            for rule in policy["metrics"]
        ]
        pairs.append({
            "pair_index": index - 1,
            "previous_level_id": previous["level_id"],
            "current_level_id": current["level_id"],
            "declared_adjacent": current["declared_predecessor_level_id"] == previous["level_id"],
            "comparisons": comparisons,
            "evidence_complete": all(item["evidence_complete"] for item in comparisons),
            "passed": all(item["passed"] for item in comparisons),
        })
    governing = pairs if policy["governing_pairs"] == "all_adjacent" else pairs[-1:]
    fit_sensitive = any(
        comparison["fit_support_sensitivity"]["outcome_changed_by_support"]
        for pair in governing for comparison in pair["comparisons"]
    )
    issues = []
    if len(levels) < policy["minimum_level_count"]:
        issues.append("minimum_level_count_not_met")
    if any(not pair["evidence_complete"] for pair in pairs):
        issues.append("declared_metric_evidence_incomplete")
    if issues:
        disposition = "invalid_evidence"
        reason_code = issues[0]
    elif fit_sensitive and policy["declared_cap_reached"]:
        disposition = "unresolved_at_declared_cap"
        reason_code = "fit_support_sensitive_at_declared_cap"
    elif fit_sensitive:
        disposition = "residual"
        reason_code = "fit_support_sensitive"
    elif all(pair["passed"] for pair in governing):
        disposition = "accepted"
        reason_code = "all_governing_metric_checks_passed"
    elif policy["declared_cap_reached"]:
        disposition = "unresolved_at_declared_cap"
        reason_code = "governing_metric_checks_failed_at_declared_cap"
    else:
        disposition = "residual"
        reason_code = "governing_metric_checks_failed"
    body = {
        "schema_name": CONVERGENCE_EVALUATION_SCHEMA,
        "schema_version": CONVERGENCE_SCHEMA_VERSION,
        "ladder_id": normalized_ladder["ladder_id"],
        "ladder_sha256": normalized_ladder["ladder_sha256"],
        "convergence_policy": policy,
        "convergence_policy_sha256": _sha256(policy),
        "pair_comparisons": pairs,
        "governing_pair_indices": [pair["pair_index"] for pair in governing],
        "evidence_issues": issues,
        "fit_sensitive": fit_sensitive,
        "monotonicity_observations": _monotonicity_observations(
            levels, policy["metrics"]
        ),
        "fixed_reference_diagnostics": _fixed_reference_pair_diagnostics(
            levels, policy["relative_denominator"]
        ),
        "scientific_disposition": disposition,
        "reason_code": reason_code,
        "undeclared_configuration_started": False,
    }
    return {**body, "evaluation_sha256": _sha256(body)}


def validate_convergence_evaluation(
    value: Any, *, ladder: Mapping[str, Any]
) -> dict[str, Any]:
    """Recompute one convergence evaluation and reject hash tampering."""
    item = _mapping(value, "convergence_evaluation")
    expected = {
        "schema_name", "schema_version", "ladder_id", "ladder_sha256",
        "convergence_policy", "convergence_policy_sha256", "pair_comparisons",
        "governing_pair_indices", "evidence_issues", "scientific_disposition",
        "fit_sensitive", "monotonicity_observations",
        "fixed_reference_diagnostics", "reason_code",
        "undeclared_configuration_started", "evaluation_sha256",
    }
    if set(item) != expected:
        raise ValueError("convergence evaluation fields are invalid")
    rebuilt = evaluate_convergence(ladder, item["convergence_policy"])
    if item != rebuilt:
        raise ValueError("convergence evaluation is noncanonical or its hash does not match")
    return deepcopy(rebuilt)


__all__ = [
    "CONVERGENCE_EVALUATION_SCHEMA", "CONVERGENCE_LADDER_SCHEMA",
    "CONVERGENCE_SCHEMA_VERSION",
    "MAX_CONVERGENCE_LEVELS", "build_convergence_ladder",
    "evaluate_convergence", "validate_convergence_evaluation",
    "validate_convergence_ladder",
]
