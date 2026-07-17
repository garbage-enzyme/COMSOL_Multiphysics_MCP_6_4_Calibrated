"""Solver-free convergence evaluation over ordered spectral evidence levels."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
from typing import Any, Mapping

from src.evidence.spectral_characterization import (
    validate_spectral_analysis_decision,
    validate_spectral_characterization,
    validate_spectral_point_bundle,
)


CONVERGENCE_LADDER_SCHEMA = "comsol_mcp.convergence_ladder"
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
    supplied_hash = _hash(item["level_sha256"], f"{label}.level_sha256")
    body = dict(item)
    body.pop("level_sha256")
    if _sha256(body) != supplied_hash:
        raise ValueError(f"{label} hash does not match")
    if item["ordinal"] != expected_ordinal:
        raise ValueError(f"{label}.ordinal must match list order")
    _identifier(item["level_id"], f"{label}.level_id")
    predecessor = item["declared_predecessor_level_id"]
    if predecessor is not None:
        _identifier(predecessor, f"{label}.declared_predecessor_level_id")
    _hash(item["configuration_sha256"], f"{label}.configuration_sha256")
    source = _mapping(item["source_model"], f"{label}.source_model")
    _hash(source.get("sha256"), f"{label}.source_model.sha256")
    _normalize_mesh_counts(item["mesh_counts"], f"{label}.mesh_counts")
    _hash(item["material_identity_sha256"], f"{label}.material_identity_sha256")
    _hash(item["incidence_identity_sha256"], f"{label}.incidence_identity_sha256")
    return deepcopy(item)


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


__all__ = [
    "CONVERGENCE_LADDER_SCHEMA", "CONVERGENCE_SCHEMA_VERSION",
    "MAX_CONVERGENCE_LEVELS", "build_convergence_ladder",
    "validate_convergence_ladder",
]
