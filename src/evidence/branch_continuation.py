"""Solver-free branch-continuation state binding and validation."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import PurePosixPath
import re
from typing import Any, Mapping

from src.evidence.spectral_characterization import (
    validate_spectral_analysis_decision,
    validate_spectral_characterization,
    validate_spectral_point_bundle,
)


BRANCH_CONTINUATION_STATES_SCHEMA = "comsol_mcp.branch_continuation_states"
BRANCH_CONTINUATION_SCHEMA_VERSION = "1.0.0"
MAX_CONTINUATION_STATES = 64
MAX_OPTIONAL_METRICS = 32

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_POLARIZATIONS = frozenset({
    "TE", "TM", "S", "P", "rhcp", "lhcp", "unpolarized",
})
_STATE_INPUT_FIELDS = {
    "state_id", "ordinal", "declared_predecessor_state_id",
    "coordinate_name", "coordinate_value", "coordinate_unit",
    "coordinate_identity_sha256", "polarization",
    "source_model_sha256", "configuration_sha256",
    "material_identity_sha256", "search_window_m",
    "spectral_bundle", "analysis_decision", "candidate_measurements",
    "optional_field_metrics",
}
_WINDOW_FIELDS = {"lower_m", "upper_m"}
_METRIC_FIELDS = {"value", "unit", "evidence_artifact_sha256"}
_CANDIDATE_FIELDS = {
    "classification", "measurement_state",
    "peak_wavelength_m", "peak_response_value",
    "fwhm_m", "quality_factor",
}
_STATE_SUMMARY_FIELDS = {
    "state_id", "ordinal", "declared_predecessor_state_id",
    "coordinate_name", "coordinate_value", "coordinate_unit",
    "coordinate_identity_sha256", "polarization",
    "source_model", "configuration_sha256",
    "material_identity_sha256", "search_window_m",
    "spectral_artifacts", "candidate",
    "optional_field_metrics", "state_sha256",
}
_VALID_CLASSIFICATIONS = frozenset({
    "interior_candidate", "boundary_high", "multi_candidate",
    "flat", "under_sampled", "no_candidate", "invalid_evidence",
})


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "branch continuation evidence must contain finite JSON values"
        ) from exc


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


def _normalize_search_window(value: Any, label: str) -> dict[str, float]:
    item = _exact_fields(value, _WINDOW_FIELDS, label)
    lower = _finite(item["lower_m"], f"{label}.lower_m")
    upper = _finite(item["upper_m"], f"{label}.upper_m")
    if not (0.0 < lower < upper) or not math.isfinite(upper):
        raise ValueError(f"{label} must have 0 < lower_m < upper_m")
    return {"lower_m": lower, "upper_m": upper}


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


def _extract_candidate(
    decision: Mapping[str, Any],
    characterization: Mapping[str, Any],
) -> dict[str, Any]:
    classification = decision["classification"]
    if classification not in _VALID_CLASSIFICATIONS:
        raise ValueError(
            f"spectral decision classification {classification!r} is unsupported"
        )
    measurement_state = characterization["measurement_state"]
    if measurement_state not in ("measured", "not_measured"):
        raise ValueError("spectral characterization measurement_state is invalid")
    candidate = characterization["candidate"]
    measured = measurement_state == "measured" and candidate is not None
    if measured:
        peak = candidate["peak"]
        fwhm = candidate["fwhm"]
        quality = candidate["quality_factor"]
        return {
            "classification": classification,
            "measurement_state": "measured",
            "peak_wavelength_m": _finite(
                peak["wavelength_m"], "candidate.peak.wavelength_m"
            ),
            "peak_response_value": _finite(
                peak["response_value"], "candidate.peak.response_value"
            ),
            "fwhm_m": (
                _finite(fwhm["value_m"], "candidate.fwhm.value_m")
                if fwhm["state"] == "bracketed" and fwhm["value_m"] is not None
                else None
            ),
            "quality_factor": (
                _finite(quality["value"], "candidate.quality_factor.value")
                if quality["state"] == "computed_from_bracketed_fwhm"
                and quality["value"] is not None
                else None
            ),
        }
    return {
        "classification": classification,
        "measurement_state": "not_measured",
        "peak_wavelength_m": None,
        "peak_response_value": None,
        "fwhm_m": None,
        "quality_factor": None,
    }


def _summarize_state(value: Any, expected_ordinal: int) -> dict[str, Any]:
    label = f"states[{expected_ordinal}]"
    item = _exact_fields(value, _STATE_INPUT_FIELDS, label)
    ordinal = item["ordinal"]
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal != expected_ordinal:
        raise ValueError(f"{label}.ordinal must match list order")
    state_id = _identifier(item["state_id"], f"{label}.state_id")
    predecessor = item["declared_predecessor_state_id"]
    if predecessor is not None:
        predecessor = _identifier(predecessor, f"{label}.declared_predecessor_state_id")
    coordinate_name = _bounded_text(
        item["coordinate_name"], f"{label}.coordinate_name"
    )
    coordinate_value = _finite(
        item["coordinate_value"], f"{label}.coordinate_value"
    )
    coordinate_unit = _bounded_text(
        item["coordinate_unit"], f"{label}.coordinate_unit"
    )
    coordinate_identity = _hash(
        item["coordinate_identity_sha256"], f"{label}.coordinate_identity_sha256"
    )
    polarization = _bounded_text(item["polarization"], f"{label}.polarization")
    if polarization not in _POLARIZATIONS:
        raise ValueError(f"{label}.polarization is not a recognized convention")
    source_hash = _hash(item["source_model_sha256"], f"{label}.source_model_sha256")
    configuration_hash = _hash(
        item["configuration_sha256"], f"{label}.configuration_sha256"
    )
    material_hash = _hash(
        item["material_identity_sha256"], f"{label}.material_identity_sha256"
    )
    search_window = _normalize_search_window(
        item["search_window_m"], f"{label}.search_window_m"
    )
    bundle = validate_spectral_point_bundle(item["spectral_bundle"])
    decision = validate_spectral_analysis_decision(
        item["analysis_decision"], bundle=bundle
    )
    characterization = validate_spectral_characterization(
        item["candidate_measurements"], bundle=bundle, decision=decision
    )
    if bundle["source_model"]["sha256"] != source_hash:
        raise ValueError(f"{label} source model hash does not match its spectral bundle")
    if bundle["configuration_sha256"] != configuration_hash:
        raise ValueError(f"{label} configuration hash does not match its spectral bundle")
    candidate = _extract_candidate(decision, characterization)
    body = {
        "state_id": state_id,
        "ordinal": ordinal,
        "declared_predecessor_state_id": predecessor,
        "coordinate_name": coordinate_name,
        "coordinate_value": coordinate_value,
        "coordinate_unit": coordinate_unit,
        "coordinate_identity_sha256": coordinate_identity,
        "polarization": polarization,
        "source_model": {
            "relative_identity": bundle["source_model"]["relative_identity"],
            "sha256": source_hash,
        },
        "configuration_sha256": configuration_hash,
        "material_identity_sha256": material_hash,
        "search_window_m": search_window,
        "spectral_artifacts": {
            "bundle_sha256": bundle["bundle_sha256"],
            "decision_sha256": decision["decision_sha256"],
            "characterization_sha256": characterization["characterization_sha256"],
            "analysis_policy_sha256": decision["analysis_policy_sha256"],
            "measurement_configuration_sha256": characterization[
                "measurement_configuration_sha256"
            ],
            "raw_row_sha256s": [
                row["raw_row_sha256"] for row in bundle["rows"]
            ],
        },
        "candidate": candidate,
        "optional_field_metrics": _normalize_metric_mapping(
            item["optional_field_metrics"], f"{label}.optional_field_metrics"
        ),
    }
    return {**body, "state_sha256": _sha256(body)}


def _validate_state_summary(value: Any, expected_ordinal: int) -> dict[str, Any]:
    label = f"states[{expected_ordinal}]"
    item = _exact_fields(value, _STATE_SUMMARY_FIELDS, label)
    ordinal = item["ordinal"]
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal != expected_ordinal:
        raise ValueError(f"{label}.ordinal must match list order")
    state_id = _identifier(item["state_id"], f"{label}.state_id")
    predecessor = item["declared_predecessor_state_id"]
    if predecessor is not None:
        predecessor = _identifier(predecessor, f"{label}.declared_predecessor_state_id")
    coordinate_name = _bounded_text(
        item["coordinate_name"], f"{label}.coordinate_name"
    )
    coordinate_value = _finite(
        item["coordinate_value"], f"{label}.coordinate_value"
    )
    coordinate_unit = _bounded_text(
        item["coordinate_unit"], f"{label}.coordinate_unit"
    )
    coordinate_identity = _hash(
        item["coordinate_identity_sha256"], f"{label}.coordinate_identity_sha256"
    )
    polarization = _bounded_text(item["polarization"], f"{label}.polarization")
    if polarization not in _POLARIZATIONS:
        raise ValueError(f"{label}.polarization is not a recognized convention")
    source = _exact_fields(
        item["source_model"], {"relative_identity", "sha256"}, f"{label}.source_model"
    )
    normalized_source = {
        "relative_identity": _relative_identity(
            source["relative_identity"], f"{label}.source_model.relative_identity"
        ),
        "sha256": _hash(source["sha256"], f"{label}.source_model.sha256"),
    }
    material_hash = _hash(
        item["material_identity_sha256"], f"{label}.material_identity_sha256"
    )
    configuration_hash = _hash(
        item["configuration_sha256"], f"{label}.configuration_sha256"
    )
    search_window = _normalize_search_window(
        item["search_window_m"], f"{label}.search_window_m"
    )
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
        _hash(digest, f"{label}.spectral_artifacts.raw_row_sha256s[{index}]")
        for index, digest in enumerate(raw_hashes)
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
    candidate = _exact_fields(item["candidate"], _CANDIDATE_FIELDS, f"{label}.candidate")
    classification = candidate["classification"]
    if classification not in _VALID_CLASSIFICATIONS:
        raise ValueError(f"{label}.candidate.classification is unsupported")
    measurement_state = candidate["measurement_state"]
    if measurement_state not in ("measured", "not_measured"):
        raise ValueError(f"{label}.candidate.measurement_state is invalid")
    complete = measurement_state == "measured"
    normalized_candidate = {
        "classification": classification,
        "measurement_state": measurement_state,
        "peak_wavelength_m": (
            _finite(candidate["peak_wavelength_m"], f"{label}.candidate.peak_wavelength_m")
            if complete else None
        ),
        "peak_response_value": (
            _finite(candidate["peak_response_value"], f"{label}.candidate.peak_response_value")
            if complete else None
        ),
        "fwhm_m": (
            None if candidate["fwhm_m"] is None
            else _finite(candidate["fwhm_m"], f"{label}.candidate.fwhm_m")
        ),
        "quality_factor": (
            None if candidate["quality_factor"] is None
            else _finite(candidate["quality_factor"], f"{label}.candidate.quality_factor")
        ),
    }
    if complete and normalized_candidate["peak_wavelength_m"] is None:
        raise ValueError(f"{label}.candidate measured state requires a peak wavelength")
    body = {
        "state_id": state_id,
        "ordinal": ordinal,
        "declared_predecessor_state_id": predecessor,
        "coordinate_name": coordinate_name,
        "coordinate_value": coordinate_value,
        "coordinate_unit": coordinate_unit,
        "coordinate_identity_sha256": coordinate_identity,
        "polarization": polarization,
        "source_model": normalized_source,
        "configuration_sha256": configuration_hash,
        "material_identity_sha256": material_hash,
        "search_window_m": search_window,
        "spectral_artifacts": normalized_artifacts,
        "candidate": normalized_candidate,
        "optional_field_metrics": _normalize_metric_mapping(
            item["optional_field_metrics"], f"{label}.optional_field_metrics"
        ),
    }
    supplied_hash = _hash(item["state_sha256"], f"{label}.state_sha256")
    rebuilt = {**body, "state_sha256": _sha256(body)}
    if rebuilt["state_sha256"] != supplied_hash or rebuilt != item:
        raise ValueError(f"{label} is noncanonical or its hash does not match")
    return rebuilt


def _validate_states_invariants(states: list[dict[str, Any]]) -> None:
    collections = (
        ("state IDs", [state["state_id"] for state in states]),
        ("configuration hashes", [state["configuration_sha256"] for state in states]),
        ("coordinate identity hashes", [
            state["coordinate_identity_sha256"] for state in states
        ]),
        ("spectral bundle hashes", [
            state["spectral_artifacts"]["bundle_sha256"] for state in states
        ]),
        ("spectral characterization hashes", [
            state["spectral_artifacts"]["characterization_sha256"] for state in states
        ]),
    )
    for label, values in collections:
        if len(values) != len(set(values)):
            raise ValueError(f"continuation states contain duplicate {label}")
    coordinate_values = [state["coordinate_value"] for state in states]
    if len(coordinate_values) != len(set(coordinate_values)):
        raise ValueError("continuation states contain duplicate coordinate values")
    for index, state in enumerate(states):
        expected = None if index == 0 else states[index - 1]["state_id"]
        if state["declared_predecessor_state_id"] != expected:
            raise ValueError("declared state adjacency does not match list order")
    consistent = {
        "coordinate_name": [state["coordinate_name"] for state in states],
        "coordinate_unit": [state["coordinate_unit"] for state in states],
        "polarization": [state["polarization"] for state in states],
        "material_identity_sha256": [
            state["material_identity_sha256"] for state in states
        ],
    }
    for name, values in consistent.items():
        if len(set(values)) != 1:
            raise ValueError(f"{name} must remain consistent across continuation states")


def build_continuation_states(
    *, states_id: str, states: list[Mapping[str, Any]]
) -> dict[str, Any]:
    """Build one ordered immutable continuation-state collection from spectral evidence."""
    if not isinstance(states, list) or not 2 <= len(states) <= MAX_CONTINUATION_STATES:
        raise ValueError(f"states must contain 2..{MAX_CONTINUATION_STATES} entries")
    normalized = [_summarize_state(state, index) for index, state in enumerate(states)]
    _validate_states_invariants(normalized)
    body = {
        "schema_name": BRANCH_CONTINUATION_STATES_SCHEMA,
        "schema_version": BRANCH_CONTINUATION_SCHEMA_VERSION,
        "states_id": _identifier(states_id, "states_id"),
        "state_count": len(normalized),
        "coordinate_name": normalized[0]["coordinate_name"],
        "coordinate_unit": normalized[0]["coordinate_unit"],
        "polarization": normalized[0]["polarization"],
        "material_identity_sha256": normalized[0]["material_identity_sha256"],
        "states": normalized,
    }
    return {**body, "states_sha256": _sha256(body)}


def validate_continuation_states(value: Any) -> dict[str, Any]:
    """Validate a canonical continuation-state collection without reading external files."""
    item = _mapping(value, "continuation_states")
    expected = {
        "schema_name", "schema_version", "states_id", "state_count",
        "coordinate_name", "coordinate_unit", "polarization",
        "material_identity_sha256", "states", "states_sha256",
    }
    if set(item) != expected:
        raise ValueError("continuation states fields are invalid")
    if (
        item["schema_name"] != BRANCH_CONTINUATION_STATES_SCHEMA
        or item["schema_version"] != BRANCH_CONTINUATION_SCHEMA_VERSION
    ):
        raise ValueError("continuation states schema is unsupported")
    states = item["states"]
    if (
        not isinstance(states, list)
        or not 2 <= len(states) <= MAX_CONTINUATION_STATES
        or item["state_count"] != len(states)
    ):
        raise ValueError("continuation states count is invalid")
    normalized = [
        _validate_state_summary(state, index) for index, state in enumerate(states)
    ]
    _validate_states_invariants(normalized)
    if item["coordinate_name"] != normalized[0]["coordinate_name"]:
        raise ValueError("continuation states coordinate_name does not match its states")
    if item["coordinate_unit"] != normalized[0]["coordinate_unit"]:
        raise ValueError("continuation states coordinate_unit does not match its states")
    if item["polarization"] != normalized[0]["polarization"]:
        raise ValueError("continuation states polarization does not match its states")
    if item["material_identity_sha256"] != normalized[0]["material_identity_sha256"]:
        raise ValueError("continuation states material identity does not match its states")
    supplied_hash = _hash(item["states_sha256"], "continuation_states.states_sha256")
    body = dict(item)
    body.pop("states_sha256")
    if _sha256(body) != supplied_hash:
        raise ValueError("continuation states hash does not match")
    return deepcopy(item)


__all__ = [
    "BRANCH_CONTINUATION_SCHEMA_VERSION",
    "BRANCH_CONTINUATION_STATES_SCHEMA",
    "MAX_CONTINUATION_STATES",
    "build_continuation_states",
    "validate_continuation_states",
]
