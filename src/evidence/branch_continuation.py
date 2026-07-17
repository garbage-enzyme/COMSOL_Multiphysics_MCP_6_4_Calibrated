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
BRANCH_CONTINUATION_PLAN_SCHEMA = "comsol_mcp.branch_continuation_plan"
BRANCH_CONTINUATION_SCHEMA_VERSION = "2.0.0"
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
_SPECTRAL_ROW_BINDING_FIELDS = {"raw_row_sha256", "requested_wavelength_m"}
_CANDIDATE_FIELDS = {
    "classification", "measurement_state",
    "peak_wavelength_m", "peak_response_value",
    "fwhm_m", "quality_factor", "boundary_side",
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
    bundle: Mapping[str, Any],
) -> dict[str, Any]:
    classification = decision["classification"]
    if classification not in _VALID_CLASSIFICATIONS:
        raise ValueError(
            f"spectral decision classification {classification!r} is unsupported"
        )
    measurement_state = characterization["measurement_state"]
    if measurement_state not in ("measured", "not_measured"):
        raise ValueError("spectral characterization measurement_state is invalid")
    boundary_side = None
    if classification == "boundary_high":
        boundary_ids = set(decision["boundary_row_ids"])
        row_ids = [row["row_id"] for row in bundle["rows"]]
        sides = []
        if row_ids and row_ids[0] in boundary_ids:
            sides.append("lower")
        if row_ids and row_ids[-1] in boundary_ids:
            sides.append("upper")
        if not sides:
            raise ValueError("boundary_high decision has no recognizable boundary side")
        boundary_side = "both" if len(sides) == 2 else sides[0]
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
            "boundary_side": boundary_side,
        }
    return {
        "classification": classification,
        "measurement_state": "not_measured",
        "peak_wavelength_m": None,
        "peak_response_value": None,
        "fwhm_m": None,
        "quality_factor": None,
        "boundary_side": boundary_side,
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
    candidate = _extract_candidate(decision, characterization, bundle)
    spectral_rows = [
        {
            "raw_row_sha256": row["raw_row_sha256"],
            "requested_wavelength_m": row["requested_wavelength_m"],
        }
        for row in bundle["rows"]
    ]
    requested_wavelengths = [
        row["requested_wavelength_m"] for row in spectral_rows
    ]
    if (
        search_window["lower_m"] != min(requested_wavelengths)
        or search_window["upper_m"] != max(requested_wavelengths)
    ):
        raise ValueError(
            f"{label}.search_window_m must exactly match the tested requested-wavelength domain"
        )
    candidate_peak = candidate["peak_wavelength_m"]
    if candidate_peak is not None and not (
        search_window["lower_m"] <= candidate_peak <= search_window["upper_m"]
    ):
        raise ValueError(f"{label} measured candidate lies outside its tested search window")
    candidate_row_ids = set(decision["candidate_row_ids"])
    candidate_row_sha256s = [
        row["raw_row_sha256"]
        for row in bundle["rows"]
        if row["row_id"] in candidate_row_ids
    ]
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
            "raw_rows": spectral_rows,
            "candidate_row_sha256s": candidate_row_sha256s,
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
            "raw_rows", "candidate_row_sha256s",
        },
        f"{label}.spectral_artifacts",
    )
    raw_rows = artifacts["raw_rows"]
    if not isinstance(raw_rows, list) or not 3 <= len(raw_rows) <= 1024:
        raise ValueError(f"{label}.spectral_artifacts.raw_rows is invalid")
    normalized_raw_rows = []
    for index, raw_row in enumerate(raw_rows):
        raw_label = f"{label}.spectral_artifacts.raw_rows[{index}]"
        raw_item = _exact_fields(raw_row, _SPECTRAL_ROW_BINDING_FIELDS, raw_label)
        normalized_raw_rows.append({
            "raw_row_sha256": _hash(
                raw_item["raw_row_sha256"], f"{raw_label}.raw_row_sha256"
            ),
            "requested_wavelength_m": _finite(
                raw_item["requested_wavelength_m"],
                f"{raw_label}.requested_wavelength_m",
            ),
        })
    raw_hashes = [row["raw_row_sha256"] for row in normalized_raw_rows]
    if len(raw_hashes) != len(set(raw_hashes)):
        raise ValueError(f"{label}.spectral_artifacts contains duplicate raw row hashes")
    requested_wavelengths = [
        row["requested_wavelength_m"] for row in normalized_raw_rows
    ]
    if len(requested_wavelengths) != len(set(requested_wavelengths)):
        raise ValueError(
            f"{label}.spectral_artifacts contains duplicate requested wavelengths"
        )
    normalized_artifacts = {
        name: _hash(artifacts[name], f"{label}.spectral_artifacts.{name}")
        for name in (
            "bundle_sha256", "decision_sha256", "characterization_sha256",
            "analysis_policy_sha256", "measurement_configuration_sha256",
        )
    }
    normalized_artifacts["raw_rows"] = normalized_raw_rows
    candidate_hashes = artifacts["candidate_row_sha256s"]
    if not isinstance(candidate_hashes, list):
        raise ValueError(
            f"{label}.spectral_artifacts.candidate_row_sha256s must be a list"
        )
    normalized_candidate_hashes = [
        _hash(
            digest,
            f"{label}.spectral_artifacts.candidate_row_sha256s[{index}]",
        )
        for index, digest in enumerate(candidate_hashes)
    ]
    if (
        len(normalized_candidate_hashes) != len(set(normalized_candidate_hashes))
        or not set(normalized_candidate_hashes).issubset(raw_hashes)
    ):
        raise ValueError(f"{label}.spectral_artifacts candidate row hashes are invalid")
    normalized_artifacts["candidate_row_sha256s"] = normalized_candidate_hashes
    candidate = _exact_fields(item["candidate"], _CANDIDATE_FIELDS, f"{label}.candidate")
    classification = candidate["classification"]
    if classification not in _VALID_CLASSIFICATIONS:
        raise ValueError(f"{label}.candidate.classification is unsupported")
    if classification == "multi_candidate" and len(normalized_candidate_hashes) < 2:
        raise ValueError(f"{label} multi-candidate state lacks candidate row bindings")
    if classification == "interior_candidate" and len(normalized_candidate_hashes) != 1:
        raise ValueError(f"{label} interior candidate row binding is invalid")
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
        "boundary_side": candidate["boundary_side"],
    }
    if (
        normalized_candidate["boundary_side"] is not None
        and normalized_candidate["boundary_side"] not in ("lower", "upper", "both")
    ):
        raise ValueError(f"{label}.candidate.boundary_side is invalid")
    if (
        normalized_candidate["boundary_side"] is not None
        and classification != "boundary_high"
    ):
        raise ValueError(
            f"{label}.candidate.boundary_side is only valid for boundary_high"
        )
    if (
        classification == "boundary_high"
        and normalized_candidate["boundary_side"] is None
    ):
        raise ValueError(
            f"{label}.candidate.boundary_side is required for boundary_high"
        )
    if complete and normalized_candidate["peak_wavelength_m"] is None:
        raise ValueError(f"{label}.candidate measured state requires a peak wavelength")
    if (
        search_window["lower_m"] != min(requested_wavelengths)
        or search_window["upper_m"] != max(requested_wavelengths)
    ):
        raise ValueError(
            f"{label}.search_window_m must exactly match the tested requested-wavelength domain"
        )
    candidate_peak = normalized_candidate["peak_wavelength_m"]
    if candidate_peak is not None and not (
        search_window["lower_m"] <= candidate_peak <= search_window["upper_m"]
    ):
        raise ValueError(f"{label} measured candidate lies outside its tested search window")
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


_POLICY_FIELDS = {
    "policy_id", "guard_window_m", "absolute_bounds_m",
    "max_expansions", "max_total_window_m", "point_budget",
    "request_grid", "stop_policy", "continuity_evidence",
    "declared_cap_reached",
}
_BOUNDARY_SIDES = {"lower", "upper", "both"}
_STOP_POLICIES = {"stop_at_first_unresolved", "continue_all_declared"}
_CONTINUITY_EVIDENCE_FIELDS = {
    "transition_index", "selected_candidate_wavelength_m",
    "supporting_raw_row_sha256", "metric_name", "measured_value",
    "tolerance", "evidence_sha256",
}
_CONTINUITY_METRICS = {"absolute_wavelength_shift_m"}
_REQUEST_GRID_FIELDS = {"point_count", "spacing_rule"}
_REQUEST_GRID_RULES = {"uniform_inclusive"}
_TRANSITION_FIELDS = {
    "transition_index", "previous_state_id", "current_state_id",
    "declared_adjacent", "previous_peak_wavelength_m",
    "current_peak_wavelength_m", "current_candidate_classification",
    "peak_within_guard", "peak_at_search_boundary",
    "boundary_side", "ambiguous_candidates",
    "expansion_required", "expansion_requested", "expansion_window_m",
    "expansion_exhausted", "expansion_count_exceeded",
    "branch_followed", "branch_recovered",
    "selected_candidate_wavelength_m", "continuity_evidence_sha256",
    "measured_continuity_verified",
    "next_request_window_m", "requested_point_count",
    "requested_wavelengths_m", "point_budget_exhausted",
    "cumulative_planned_point_count", "transition_sha256",
}
_PLAN_FIELDS = {
    "schema_name", "schema_version", "states_id", "states_sha256",
    "continuation_policy", "continuation_policy_sha256",
    "coordinate_transitions", "total_expansions_proposed",
    "ambiguous_transition_count", "branch_followed_transition_count",
    "processed_transition_count", "skipped_state_ids",
    "planned_point_count", "point_budget_exhausted",
    "scientific_disposition", "reason_code",
    "branch_disappearance_claimed", "undeclared_coordinate_started",
    "plan_sha256",
}


def _normalize_continuation_policy(
    value: Any, *, states: Mapping[str, Any]
) -> dict[str, Any]:
    item = _exact_fields(value, _POLICY_FIELDS, "continuation_policy")
    guard = _finite(item["guard_window_m"], "continuation_policy.guard_window_m")
    if guard <= 0.0:
        raise ValueError("continuation_policy.guard_window_m must be positive")
    bounds = _exact_fields(
        item["absolute_bounds_m"], _WINDOW_FIELDS, "continuation_policy.absolute_bounds_m"
    )
    lower = _finite(bounds["lower_m"], "continuation_policy.absolute_bounds_m.lower_m")
    upper = _finite(bounds["upper_m"], "continuation_policy.absolute_bounds_m.upper_m")
    if not (0.0 < lower < upper):
        raise ValueError("continuation_policy.absolute_bounds_m must have 0 < lower_m < upper_m")
    max_expansions = item["max_expansions"]
    if isinstance(max_expansions, bool) or not isinstance(max_expansions, int) or max_expansions < 0:
        raise ValueError("continuation_policy.max_expansions must be a nonnegative integer")
    max_total = _finite(
        item["max_total_window_m"], "continuation_policy.max_total_window_m"
    )
    if max_total <= 0.0:
        raise ValueError("continuation_policy.max_total_window_m must be positive")
    point_budget = item["point_budget"]
    if isinstance(point_budget, bool) or not isinstance(point_budget, int) or point_budget <= 0:
        raise ValueError("continuation_policy.point_budget must be a positive integer")
    request_grid = _exact_fields(
        item["request_grid"], _REQUEST_GRID_FIELDS,
        "continuation_policy.request_grid",
    )
    request_point_count = request_grid["point_count"]
    if (
        isinstance(request_point_count, bool)
        or not isinstance(request_point_count, int)
        or not 2 <= request_point_count <= 1024
    ):
        raise ValueError(
            "continuation_policy.request_grid.point_count must be an integer from 2 to 1024"
        )
    if request_grid["spacing_rule"] not in _REQUEST_GRID_RULES:
        raise ValueError("continuation_policy.request_grid.spacing_rule is unsupported")
    if item["stop_policy"] not in _STOP_POLICIES:
        raise ValueError("continuation_policy.stop_policy is unsupported")
    if not isinstance(item["declared_cap_reached"], bool):
        raise ValueError("continuation_policy.declared_cap_reached must be boolean")
    for state in states["states"]:
        window = state["search_window_m"]
        if window["lower_m"] < lower or window["upper_m"] > upper:
            raise ValueError(
                "continuation_policy.absolute_bounds_m must contain every state search window"
            )
    continuity_evidence = item["continuity_evidence"]
    if not isinstance(continuity_evidence, list):
        raise ValueError("continuation_policy.continuity_evidence must be a list")
    if len(continuity_evidence) > len(states["states"]) - 1:
        raise ValueError("continuation_policy.continuity_evidence exceeds transition count")
    normalized_evidence = []
    evidence_indexes = set()
    for evidence_position, evidence_value in enumerate(continuity_evidence):
        evidence_label = (
            f"continuation_policy.continuity_evidence[{evidence_position}]"
        )
        evidence = _exact_fields(
            evidence_value, _CONTINUITY_EVIDENCE_FIELDS, evidence_label
        )
        transition_index = evidence["transition_index"]
        if (
            isinstance(transition_index, bool)
            or not isinstance(transition_index, int)
            or not 0 <= transition_index < len(states["states"]) - 1
        ):
            raise ValueError(f"{evidence_label}.transition_index is invalid")
        if transition_index in evidence_indexes:
            raise ValueError("continuation_policy contains duplicate continuity evidence")
        evidence_indexes.add(transition_index)
        previous = states["states"][transition_index]
        current = states["states"][transition_index + 1]
        if current["candidate"]["classification"] != "multi_candidate":
            raise ValueError(f"{evidence_label} is only valid for a multi-candidate state")
        previous_peak = previous["candidate"]["peak_wavelength_m"]
        if previous_peak is None:
            raise ValueError(f"{evidence_label} requires a preceding measured peak")
        selected_wavelength = _finite(
            evidence["selected_candidate_wavelength_m"],
            f"{evidence_label}.selected_candidate_wavelength_m",
        )
        supporting_hash = _hash(
            evidence["supporting_raw_row_sha256"],
            f"{evidence_label}.supporting_raw_row_sha256",
        )
        if supporting_hash not in current["spectral_artifacts"]["candidate_row_sha256s"]:
            raise ValueError(
                f"{evidence_label} supporting raw row is not a measured candidate"
            )
        matching_rows = [
            row for row in current["spectral_artifacts"]["raw_rows"]
            if row["raw_row_sha256"] == supporting_hash
        ]
        if (
            len(matching_rows) != 1
            or matching_rows[0]["requested_wavelength_m"] != selected_wavelength
        ):
            raise ValueError(
                f"{evidence_label} selected candidate is not bound to its raw spectral row"
            )
        metric_name = evidence["metric_name"]
        if metric_name not in _CONTINUITY_METRICS:
            raise ValueError(f"{evidence_label}.metric_name is unsupported")
        measured_value = _finite(
            evidence["measured_value"], f"{evidence_label}.measured_value"
        )
        tolerance = _finite(evidence["tolerance"], f"{evidence_label}.tolerance")
        if measured_value < 0.0 or tolerance <= 0.0:
            raise ValueError(f"{evidence_label} metric values are invalid")
        expected_value = abs(selected_wavelength - previous_peak)
        if measured_value != expected_value:
            raise ValueError(
                f"{evidence_label}.measured_value does not match the bound measurements"
            )
        if measured_value > tolerance:
            raise ValueError(f"{evidence_label} exceeds its declared tolerance")
        evidence_body = {
            "transition_index": transition_index,
            "selected_candidate_wavelength_m": selected_wavelength,
            "supporting_raw_row_sha256": supporting_hash,
            "metric_name": metric_name,
            "measured_value": measured_value,
            "tolerance": tolerance,
        }
        supplied_hash = _hash(
            evidence["evidence_sha256"], f"{evidence_label}.evidence_sha256"
        )
        if _sha256(evidence_body) != supplied_hash:
            raise ValueError(f"{evidence_label} hash does not match")
        normalized_evidence.append({**evidence_body, "evidence_sha256": supplied_hash})
    normalized_evidence.sort(key=lambda entry: entry["transition_index"])
    return {
        "policy_id": _identifier(item["policy_id"], "continuation_policy.policy_id"),
        "guard_window_m": guard,
        "absolute_bounds_m": {"lower_m": lower, "upper_m": upper},
        "max_expansions": max_expansions,
        "max_total_window_m": max_total,
        "point_budget": point_budget,
        "request_grid": {
            "point_count": request_point_count,
            "spacing_rule": request_grid["spacing_rule"],
        },
        "stop_policy": item["stop_policy"],
        "continuity_evidence": normalized_evidence,
        "declared_cap_reached": item["declared_cap_reached"],
    }


def _compute_next_request(
    seed_peak: float,
    guard: float,
    bounds: Mapping[str, float],
) -> dict[str, float]:
    lower = max(seed_peak - guard, bounds["lower_m"])
    upper = min(seed_peak + guard, bounds["upper_m"])
    return {"lower_m": lower, "upper_m": upper}


def _request_wavelengths(
    window: Mapping[str, float], point_count: int
) -> list[float]:
    lower = window["lower_m"]
    upper = window["upper_m"]
    step = (upper - lower) / (point_count - 1)
    wavelengths = [lower + index * step for index in range(point_count)]
    wavelengths[-1] = upper
    return wavelengths


def _compute_expansion(
    search: Mapping[str, float],
    boundary_side: str,
    guard: float,
    bounds: Mapping[str, float],
    max_total: float,
) -> tuple[dict[str, float] | None, bool]:
    new_lower = search["lower_m"]
    new_upper = search["upper_m"]
    if boundary_side in ("lower", "both"):
        new_lower = max(search["lower_m"] - guard, bounds["lower_m"])
    if boundary_side in ("upper", "both"):
        new_upper = min(search["upper_m"] + guard, bounds["upper_m"])
    width = new_upper - new_lower
    changed = new_lower < search["lower_m"] or new_upper > search["upper_m"]
    within_bounds = (
        changed
        and new_lower < new_upper
        and width > 0.0
        and width <= max_total
        and new_lower >= bounds["lower_m"]
        and new_upper <= bounds["upper_m"]
    )
    if not within_bounds:
        return None, False
    return {"lower_m": new_lower, "upper_m": new_upper}, True


def _one_transition(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    policy: Mapping[str, Any],
    total_expansions: int,
    planned_points: int,
) -> tuple[dict[str, Any], int, int]:
    previous_peak = previous["candidate"]["peak_wavelength_m"]
    current_candidate = current["candidate"]
    current_peak = current_candidate["peak_wavelength_m"]
    classification = current_candidate["classification"]
    boundary_side = current_candidate["boundary_side"]
    guard = policy["guard_window_m"]
    bounds = policy["absolute_bounds_m"]

    peak_within_guard = False
    peak_at_boundary = False
    ambiguous = False
    expansion_required = False
    expansion_requested = False
    expansion_window = None
    expansion_exhausted = False
    expansion_count_exceeded = False
    branch_followed = False
    branch_recovered = False
    selected_candidate = None
    continuity_evidence_sha256 = None
    measured_continuity_verified = False
    expansion_available = False

    if classification == "multi_candidate":
        ambiguous = True
        continuity_evidence = next(
            (
                evidence for evidence in policy["continuity_evidence"]
                if evidence["transition_index"] == current["ordinal"] - 1
            ),
            None,
        )
        if continuity_evidence is not None:
            branch_followed = True
            selected_candidate = continuity_evidence[
                "selected_candidate_wavelength_m"
            ]
            continuity_evidence_sha256 = continuity_evidence["evidence_sha256"]
            measured_continuity_verified = True
    elif classification == "boundary_high":
        peak_at_boundary = True
        expansion_required = True
        if total_expansions < policy["max_expansions"]:
            expansion_window, expansion_available = _compute_expansion(
                current["search_window_m"],
                boundary_side,
                guard,
                bounds,
                policy["max_total_window_m"],
            )
            if not expansion_available:
                expansion_exhausted = True
        else:
            expansion_count_exceeded = True
    elif current_peak is not None:
        if previous["candidate"]["classification"] == "boundary_high":
            expected_window, expansion_available = _compute_expansion(
                previous["search_window_m"],
                previous["candidate"]["boundary_side"],
                guard,
                bounds,
                policy["max_total_window_m"],
            )
            previous_boundary_side = previous["candidate"]["boundary_side"]
            boundary_reference = (
                previous["search_window_m"][f"{previous_boundary_side}_m"]
                if previous_boundary_side in ("lower", "upper")
                else None
            )
            if (
                expansion_available
                and boundary_reference is not None
                and current["search_window_m"] == expected_window
            ):
                peak_within_guard = abs(current_peak - boundary_reference) <= guard
                branch_recovered = peak_within_guard
                branch_followed = branch_recovered
        elif previous_peak is not None:
            peak_within_guard = abs(current_peak - previous_peak) <= guard
            branch_followed = peak_within_guard
    else:
        branch_followed = False

    if expansion_required:
        candidate_request = expansion_window
    else:
        seed_peak = (
            current_peak
            if current_peak is not None
            else selected_candidate
            if selected_candidate is not None
            else previous_peak
        )
        candidate_request = (
            _compute_next_request(seed_peak, guard, bounds)
            if seed_peak is not None else None
        )
    request_point_count = policy["request_grid"]["point_count"]
    point_budget_exhausted = (
        candidate_request is not None
        and planned_points + request_point_count > policy["point_budget"]
    )
    if candidate_request is not None and not point_budget_exhausted:
        next_request = candidate_request
        requested_point_count = request_point_count
        requested_wavelengths = _request_wavelengths(
            next_request, requested_point_count
        )
        planned_points += requested_point_count
        if expansion_required:
            expansion_requested = True
            total_expansions += 1
    else:
        next_request = None
        requested_point_count = 0
        requested_wavelengths = []

    body = {
        "transition_index": current["ordinal"] - 1,
        "previous_state_id": previous["state_id"],
        "current_state_id": current["state_id"],
        "declared_adjacent": (
            current["declared_predecessor_state_id"] == previous["state_id"]
        ),
        "previous_peak_wavelength_m": previous_peak,
        "current_peak_wavelength_m": current_peak,
        "current_candidate_classification": classification,
        "peak_within_guard": peak_within_guard,
        "peak_at_search_boundary": peak_at_boundary,
        "boundary_side": boundary_side,
        "ambiguous_candidates": ambiguous,
        "expansion_required": expansion_required,
        "expansion_requested": expansion_requested,
        "expansion_window_m": expansion_window,
        "expansion_exhausted": expansion_exhausted,
        "expansion_count_exceeded": expansion_count_exceeded,
        "branch_followed": branch_followed,
        "branch_recovered": branch_recovered,
        "selected_candidate_wavelength_m": selected_candidate,
        "continuity_evidence_sha256": continuity_evidence_sha256,
        "measured_continuity_verified": measured_continuity_verified,
        "next_request_window_m": next_request,
        "requested_point_count": requested_point_count,
        "requested_wavelengths_m": requested_wavelengths,
        "point_budget_exhausted": point_budget_exhausted,
        "cumulative_planned_point_count": planned_points,
    }
    return (
        {**body, "transition_sha256": _sha256(body)},
        total_expansions,
        planned_points,
    )


def plan_branch_continuation(
    states: Mapping[str, Any],
    continuation_policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Plan bounded per-coordinate continuation windows from ordered spectral evidence."""
    normalized_states = validate_continuation_states(states)
    policy = _normalize_continuation_policy(
        continuation_policy, states=normalized_states
    )
    all_states = normalized_states["states"]
    transitions = []
    total_expansions = 0
    planned_points = 0
    stopped_after_state_index = None
    for index in range(1, len(all_states)):
        transition, total_expansions, planned_points = _one_transition(
            all_states[index - 1], all_states[index], policy,
            total_expansions, planned_points,
        )
        transitions.append(transition)
        if (
            policy["stop_policy"] == "stop_at_first_unresolved"
            and not transition["branch_followed"]
        ):
            stopped_after_state_index = index
            break

    skipped_state_ids = (
        [state["state_id"] for state in all_states[stopped_after_state_index + 1:]]
        if stopped_after_state_index is not None
        else []
    )

    ambiguous_count = sum(1 for t in transitions if t["ambiguous_candidates"])
    followed_count = sum(1 for t in transitions if t["branch_followed"])
    cap_exceeded = any(t["expansion_count_exceeded"] for t in transitions)
    expansion_exhausted = any(t["expansion_exhausted"] for t in transitions)
    unresolved_ambiguity = any(
        t["ambiguous_candidates"] and not t["measured_continuity_verified"]
        for t in transitions
    )
    required_request_budget_exhausted = any(
        transition["expansion_required"]
        and transition["point_budget_exhausted"]
        for transition in transitions
    )

    recovered_expansion_indexes = {
        index - 1
        for index, transition in enumerate(transitions)
        if index > 0 and transition["branch_recovered"]
    }
    pending_expansion = any(
        transition["expansion_requested"] and index not in recovered_expansion_indexes
        for index, transition in enumerate(transitions)
    )
    transitions_resolved = all(
        transition["branch_followed"]
        or (
            transition["expansion_requested"]
            and index in recovered_expansion_indexes
        )
        for index, transition in enumerate(transitions)
    )

    if (
        cap_exceeded
        or expansion_exhausted
        or required_request_budget_exhausted
        or unresolved_ambiguity
    ):
        disposition = "unresolved_at_declared_cap"
        if cap_exceeded:
            reason_code = "expansion_count_exceeded_at_declared_cap"
        elif expansion_exhausted:
            reason_code = "boundary_expansion_exhausted_at_declared_cap"
        elif required_request_budget_exhausted:
            reason_code = "point_budget_exhausted_at_declared_cap"
        else:
            reason_code = "ambiguous_candidates_without_measured_evidence"
    elif transitions_resolved:
        disposition = "accepted"
        reason_code = (
            "all_transitions_branch_followed"
            if followed_count == len(transitions)
            else "all_boundary_expansions_recovered"
        )
    elif pending_expansion:
        disposition = "residual"
        reason_code = "boundary_expansion_requires_measured_evidence"
    elif policy["declared_cap_reached"]:
        disposition = "unresolved_at_declared_cap"
        reason_code = "branch_not_followed_at_declared_cap"
    else:
        disposition = "residual"
        reason_code = "branch_not_followed"

    body = {
        "schema_name": BRANCH_CONTINUATION_PLAN_SCHEMA,
        "schema_version": BRANCH_CONTINUATION_SCHEMA_VERSION,
        "states_id": normalized_states["states_id"],
        "states_sha256": normalized_states["states_sha256"],
        "continuation_policy": policy,
        "continuation_policy_sha256": _sha256(policy),
        "coordinate_transitions": transitions,
        "total_expansions_proposed": total_expansions,
        "ambiguous_transition_count": ambiguous_count,
        "branch_followed_transition_count": followed_count,
        "processed_transition_count": len(transitions),
        "skipped_state_ids": skipped_state_ids,
        "planned_point_count": planned_points,
        "point_budget_exhausted": any(
            transition["point_budget_exhausted"] for transition in transitions
        ),
        "scientific_disposition": disposition,
        "reason_code": reason_code,
        "branch_disappearance_claimed": False,
        "undeclared_coordinate_started": False,
    }
    return {**body, "plan_sha256": _sha256(body)}


def _validate_transition(value: Any, expected_index: int) -> dict[str, Any]:
    label = f"coordinate_transitions[{expected_index}]"
    item = _exact_fields(value, _TRANSITION_FIELDS, label)
    if item["transition_index"] != expected_index:
        raise ValueError(f"{label}.transition_index must match list order")
    for field in ("previous_state_id", "current_state_id"):
        _identifier(item[field], f"{label}.{field}")
    if not isinstance(item["declared_adjacent"], bool):
        raise ValueError(f"{label}.declared_adjacent must be boolean")
    for field in ("previous_peak_wavelength_m", "current_peak_wavelength_m"):
        value = item[field]
        if value is not None:
            _finite(value, f"{label}.{field}")
    classification = item["current_candidate_classification"]
    if classification not in _VALID_CLASSIFICATIONS:
        raise ValueError(f"{label}.current_candidate_classification is unsupported")
    for field in (
        "peak_within_guard", "peak_at_search_boundary", "ambiguous_candidates",
        "expansion_required", "expansion_requested", "expansion_exhausted",
        "expansion_count_exceeded", "branch_followed", "branch_recovered",
        "measured_continuity_verified", "point_budget_exhausted",
    ):
        if not isinstance(item[field], bool):
            raise ValueError(f"{label}.{field} must be boolean")
    side = item["boundary_side"]
    if side is not None and side not in _BOUNDARY_SIDES:
        raise ValueError(f"{label}.boundary_side is invalid")
    expansion_window = item["expansion_window_m"]
    if expansion_window is not None:
        _normalize_search_window(expansion_window, f"{label}.expansion_window_m")
    next_request = item["next_request_window_m"]
    if next_request is not None:
        _normalize_search_window(next_request, f"{label}.next_request_window_m")
    requested_point_count = item["requested_point_count"]
    if (
        isinstance(requested_point_count, bool)
        or not isinstance(requested_point_count, int)
        or requested_point_count < 0
    ):
        raise ValueError(f"{label}.requested_point_count is invalid")
    requested_wavelengths = item["requested_wavelengths_m"]
    if not isinstance(requested_wavelengths, list):
        raise ValueError(f"{label}.requested_wavelengths_m must be a list")
    normalized_wavelengths = [
        _finite(wavelength, f"{label}.requested_wavelengths_m[{index}]")
        for index, wavelength in enumerate(requested_wavelengths)
    ]
    if len(normalized_wavelengths) != requested_point_count:
        raise ValueError(f"{label} requested point count does not match its grid")
    if next_request is None:
        if requested_point_count != 0:
            raise ValueError(f"{label} cannot contain points without a request window")
    elif (
        requested_point_count < 2
        or normalized_wavelengths[0] != next_request["lower_m"]
        or normalized_wavelengths[-1] != next_request["upper_m"]
    ):
        raise ValueError(f"{label} request grid does not match its window")
    cumulative_points = item["cumulative_planned_point_count"]
    if (
        isinstance(cumulative_points, bool)
        or not isinstance(cumulative_points, int)
        or cumulative_points < 0
    ):
        raise ValueError(f"{label}.cumulative_planned_point_count is invalid")
    selected_candidate = item["selected_candidate_wavelength_m"]
    if selected_candidate is not None:
        _finite(selected_candidate, f"{label}.selected_candidate_wavelength_m")
    evidence_hash = item["continuity_evidence_sha256"]
    if evidence_hash is not None:
        _hash(evidence_hash, f"{label}.continuity_evidence_sha256")
    supplied_hash = _hash(item["transition_sha256"], f"{label}.transition_sha256")
    rebuilt = dict(item)
    rebuilt.pop("transition_sha256")
    if _sha256(rebuilt) != supplied_hash:
        raise ValueError(f"{label} is noncanonical or its hash does not match")
    return deepcopy(item)


def validate_branch_continuation_plan(
    value: Any, *, states: Mapping[str, Any]
) -> dict[str, Any]:
    """Recompute one continuation plan and reject hash tampering."""
    item = _mapping(value, "branch_continuation_plan")
    if set(item) != _PLAN_FIELDS:
        raise ValueError("branch continuation plan fields are invalid")
    rebuilt = plan_branch_continuation(states, item["continuation_policy"])
    if item != rebuilt:
        raise ValueError(
            "branch continuation plan is noncanonical or its hash does not match"
        )
    return deepcopy(rebuilt)


__all__ = [
    "BRANCH_CONTINUATION_PLAN_SCHEMA",
    "BRANCH_CONTINUATION_SCHEMA_VERSION",
    "BRANCH_CONTINUATION_STATES_SCHEMA",
    "MAX_CONTINUATION_STATES",
    "build_continuation_states",
    "plan_branch_continuation",
    "validate_branch_continuation_plan",
    "validate_continuation_states",
]
