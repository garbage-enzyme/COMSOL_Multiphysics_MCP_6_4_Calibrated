"""Solver-free branch-continuation planning regression tests."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import pytest

from src.evidence.branch_continuation import (
    BRANCH_CONTINUATION_SCHEMA_VERSION,
    BRANCH_CONTINUATION_STATES_SCHEMA,
    MAX_CONTINUATION_STATES,
    build_continuation_states,
    validate_continuation_states,
)
from src.evidence.spectral_characterization import (
    build_spectral_analysis_decision,
    build_spectral_characterization,
    build_spectral_point_bundle,
)


MATERIAL_SHA256 = "a" * 64
COORDINATE_IDENTITY = "b" * 64
SOURCE_SHA = "c" * 64


def _hex_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _spectral_artifacts(index: int, center: float, amplitude: float = 0.9):
    configuration = _hex_id(f"config-{index}")
    wavelengths = [center + offset * 0.05e-6 for offset in range(-3, 4)]
    values = [0.1, 0.3, 0.5, amplitude, 0.5, 0.3, 0.1]
    rows = []
    for row_index, (wavelength, absorption) in enumerate(zip(wavelengths, values)):
        raw = {"state": index, "row": row_index, "wavelength": wavelength}
        rows.append({
            "row_id": f"state-{index}-point-{row_index}",
            "raw_row_sha256": hashlib.sha256(
                json.dumps(raw, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "configuration_sha256": configuration,
            "requested_wavelength_m": wavelength,
            "evaluated_wavelength_m": wavelength,
            "frequency_wavelength_m": wavelength,
            "R": 0.95 - absorption,
            "T": 0.05,
            "A": absorption,
        })
    bundle = build_spectral_point_bundle(
        bundle_id=f"spectrum-state-{index}",
        source_model={
            "relative_identity": f"fixtures/source-{index}.mph",
            "sha256": _hex_id(f"source-{index}"),
        },
        configuration_sha256=configuration,
        parameter_state={"coordinate_index": index},
        wavelength_convention={
            "unit": "m",
            "requested_field": "requested_wavelength_m",
            "evaluated_field": "evaluated_wavelength_m",
            "frequency_derived_field": "frequency_wavelength_m",
            "frequency_relation": "c_const/frequency",
        },
        expressions={"R": "R", "T": "T", "A": "1-R-T"},
        rows=rows,
    )
    policy = {
        "response_quantity": "A",
        "candidate_polarity": "maximum",
        "passivity_abs_tolerance": 1e-12,
        "closure_abs_tolerance": 1e-12,
        "wavelength_sync_abs_m": 1e-15,
        "flat_response_abs_tolerance": 1e-12,
        "minimum_point_count": 5,
    }
    measurement = {
        "peak_method": "measured_grid",
        "baseline_rule": "declared_response",
        "baseline_response_value": 0.1,
        "fwhm_definition": "half_prominence",
        "fit_support_points": None,
        "fit_support_sensitivity_points": [],
        "local_polynomial_degree": None,
        "fit_max_evaluations": None,
    }
    decision = build_spectral_analysis_decision(bundle, policy)
    characterization = build_spectral_characterization(bundle, decision, measurement)
    return bundle, decision, characterization


def _state(
    index: int,
    center: float,
    predecessor: str | None,
    *,
    coordinate_value: float,
    coordinate_identity: str | None = None,
    search_lower: float = 4.0e-6,
    search_upper: float = 6.0e-6,
    amplitude: float = 0.9,
):
    bundle, decision, characterization = _spectral_artifacts(
        index, center, amplitude=amplitude
    )
    return {
        "state_id": f"coord-{index}",
        "ordinal": index,
        "declared_predecessor_state_id": predecessor,
        "coordinate_name": "incidence_angle",
        "coordinate_value": coordinate_value,
        "coordinate_unit": "deg",
        "coordinate_identity_sha256": coordinate_identity or _hex_id(f"coord-id-{index}"),
        "polarization": "TM",
        "source_model_sha256": bundle["source_model"]["sha256"],
        "configuration_sha256": bundle["configuration_sha256"],
        "material_identity_sha256": MATERIAL_SHA256,
        "search_window_m": {
            "lower_m": search_lower,
            "upper_m": search_upper,
        },
        "spectral_bundle": bundle,
        "analysis_decision": decision,
        "candidate_measurements": characterization,
        "optional_field_metrics": {
            "field_overlap": {
                "value": 0.8 + 0.01 * index,
                "unit": "1",
                "evidence_artifact_sha256": _hex_id(f"field-artifact-{index}"),
            }
        },
    }


def _build_dispersive_states(count: int = 3, shift: float = 0.1e-6):
    centers = [5.0e-6 + shift * index for index in range(count)]
    angles = [5.0 * index for index in range(count)]
    states = []
    for index in range(count):
        predecessor = None if index == 0 else f"coord-{index - 1}"
        states.append(_state(index, centers[index], predecessor, coordinate_value=angles[index]))
    return states


class TestContinuationStateBinding:
    def test_build_continuation_states_produces_canonical_collection(self):
        states = _build_dispersive_states(3)
        result = build_continuation_states(states_id="angle-sweep", states=states)
        assert result["schema_name"] == BRANCH_CONTINUATION_STATES_SCHEMA
        assert result["schema_version"] == BRANCH_CONTINUATION_SCHEMA_VERSION
        assert result["states_id"] == "angle-sweep"
        assert result["state_count"] == 3
        assert result["coordinate_name"] == "incidence_angle"
        assert result["coordinate_unit"] == "deg"
        assert result["polarization"] == "TM"
        assert result["material_identity_sha256"] == MATERIAL_SHA256
        assert "states_sha256" in result
        for index, state in enumerate(result["states"]):
            assert state["ordinal"] == index
            assert state["state_sha256"]
            assert state["candidate"]["classification"] == "interior_candidate"
            assert state["candidate"]["measurement_state"] == "measured"
            assert state["candidate"]["peak_wavelength_m"] is not None
            assert state["candidate"]["peak_response_value"] is not None

    def test_validate_continuation_states_round_trips(self):
        states = _build_dispersive_states(3)
        built = build_continuation_states(states_id="angle-sweep", states=states)
        validated = validate_continuation_states(built)
        assert validated == built

    def test_states_sha256_is_deterministic(self):
        states = _build_dispersive_states(3)
        first = build_continuation_states(states_id="angle-sweep", states=states)
        second = build_continuation_states(states_id="angle-sweep", states=deepcopy(states))
        assert first["states_sha256"] == second["states_sha256"]

    def test_hash_tampering_in_states_sha256_is_rejected(self):
        states = _build_dispersive_states(3)
        built = build_continuation_states(states_id="angle-sweep", states=states)
        tampered = deepcopy(built)
        tampered["states_sha256"] = "0" * 64
        with pytest.raises(ValueError, match="hash does not match"):
            validate_continuation_states(tampered)

    def test_hash_tampering_in_state_sha256_is_rejected(self):
        states = _build_dispersive_states(3)
        built = build_continuation_states(states_id="angle-sweep", states=states)
        tampered = deepcopy(built)
        tampered["states"][1]["state_sha256"] = "0" * 64
        with pytest.raises(ValueError, match="noncanonical"):
            validate_continuation_states(tampered)

    def test_noncanonical_state_is_rejected(self):
        states = _build_dispersive_states(3)
        built = build_continuation_states(states_id="angle-sweep", states=states)
        tampered = deepcopy(built)
        tampered["states"][0]["candidate"]["peak_wavelength_m"] = 3.0e-6
        tampered["states"][0]["state_sha256"] = "0" * 64
        with pytest.raises(ValueError, match="noncanonical"):
            validate_continuation_states(tampered)

    def test_wrong_ordinal_is_rejected(self):
        states = _build_dispersive_states(3)
        states[1]["ordinal"] = 0
        with pytest.raises(ValueError, match="ordinal"):
            build_continuation_states(states_id="angle-sweep", states=states)

    def test_wrong_adjacency_is_rejected(self):
        states = _build_dispersive_states(3)
        states[1]["declared_predecessor_state_id"] = "coord-99"
        with pytest.raises(ValueError, match="adjacency"):
            build_continuation_states(states_id="angle-sweep", states=states)

    def test_duplicate_state_ids_are_rejected(self):
        states = _build_dispersive_states(3)
        states[1]["state_id"] = "coord-0"
        with pytest.raises(ValueError, match="duplicate state IDs"):
            build_continuation_states(states_id="angle-sweep", states=states)

    def test_duplicate_configuration_hashes_are_rejected(self):
        states = _build_dispersive_states(3)
        states[1]["spectral_bundle"] = deepcopy(states[0]["spectral_bundle"])
        states[1]["analysis_decision"] = deepcopy(states[0]["analysis_decision"])
        states[1]["candidate_measurements"] = deepcopy(states[0]["candidate_measurements"])
        states[1]["source_model_sha256"] = states[0]["source_model_sha256"]
        states[1]["configuration_sha256"] = states[0]["configuration_sha256"]
        with pytest.raises(ValueError, match="duplicate configuration hashes"):
            build_continuation_states(states_id="angle-sweep", states=states)

    def test_duplicate_coordinate_identity_is_rejected(self):
        states = _build_dispersive_states(3)
        states[1]["coordinate_identity_sha256"] = states[0]["coordinate_identity_sha256"]
        with pytest.raises(ValueError, match="duplicate coordinate identity"):
            build_continuation_states(states_id="angle-sweep", states=states)

    def test_duplicate_coordinate_values_are_rejected(self):
        states = _build_dispersive_states(3)
        states[1]["coordinate_value"] = states[0]["coordinate_value"]
        with pytest.raises(ValueError, match="duplicate coordinate values"):
            build_continuation_states(states_id="angle-sweep", states=states)

    def test_inconsistent_coordinate_name_is_rejected(self):
        states = _build_dispersive_states(3)
        states[1]["coordinate_name"] = "azimuth_angle"
        with pytest.raises(ValueError, match="coordinate_name"):
            build_continuation_states(states_id="angle-sweep", states=states)

    def test_inconsistent_polarization_is_rejected(self):
        states = _build_dispersive_states(3)
        states[1]["polarization"] = "TE"
        with pytest.raises(ValueError, match="polarization"):
            build_continuation_states(states_id="angle-sweep", states=states)

    def test_inconsistent_material_identity_is_rejected(self):
        states = _build_dispersive_states(3)
        states[1]["material_identity_sha256"] = "f" * 64
        with pytest.raises(ValueError, match="material_identity_sha256"):
            build_continuation_states(states_id="angle-sweep", states=states)

    def test_too_few_states_are_rejected(self):
        states = _build_dispersive_states(2)
        states = [states[0]]
        with pytest.raises(ValueError, match="2.."):
            build_continuation_states(states_id="angle-sweep", states=states)

    def test_too_many_states_are_rejected(self):
        states = []
        for index in range(MAX_CONTINUATION_STATES + 1):
            predecessor = None if index == 0 else f"coord-{index - 1}"
            states.append(
                _state(index, 5.0e-6, predecessor, coordinate_value=float(index))
            )
        with pytest.raises(ValueError, match="2.."):
            build_continuation_states(states_id="angle-sweep", states=states)

    def test_invalid_search_window_is_rejected(self):
        states = _build_dispersive_states(3)
        states[0]["search_window_m"]["lower_m"] = 6.0e-6
        states[0]["search_window_m"]["upper_m"] = 4.0e-6
        with pytest.raises(ValueError, match="search_window"):
            build_continuation_states(states_id="angle-sweep", states=states)

    def test_unrecognized_polarization_is_rejected(self):
        states = _build_dispersive_states(3)
        states[0]["polarization"] = "circular"
        with pytest.raises(ValueError, match="polarization"):
            build_continuation_states(states_id="angle-sweep", states=states)

    def test_source_model_hash_mismatch_is_rejected(self):
        states = _build_dispersive_states(3)
        states[0]["source_model_sha256"] = "0" * 64
        with pytest.raises(ValueError, match="source model hash"):
            build_continuation_states(states_id="angle-sweep", states=states)

    def test_configuration_hash_mismatch_is_rejected(self):
        states = _build_dispersive_states(3)
        states[0]["configuration_sha256"] = "0" * 64
        with pytest.raises(ValueError, match="configuration hash"):
            build_continuation_states(states_id="angle-sweep", states=states)

    def test_no_candidate_state_is_accepted(self):
        flat_values = [0.5] * 7
        configuration = "9" * 64
        wavelengths = [5.0e-6 + offset * 0.05e-6 for offset in range(-3, 4)]
        rows = []
        for row_index, (wavelength, absorption) in enumerate(zip(wavelengths, flat_values)):
            raw = {"state": 0, "row": row_index, "wavelength": wavelength}
            rows.append({
                "row_id": f"state-0-flat-{row_index}",
                "raw_row_sha256": hashlib.sha256(
                    json.dumps(raw, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                "configuration_sha256": configuration,
                "requested_wavelength_m": wavelength,
                "evaluated_wavelength_m": wavelength,
                "frequency_wavelength_m": wavelength,
                "R": 0.95 - absorption,
                "T": 0.05,
                "A": absorption,
            })
        bundle = build_spectral_point_bundle(
            bundle_id="spectrum-flat",
            source_model={
                "relative_identity": "fixtures/flat.mph",
                "sha256": SOURCE_SHA,
            },
            configuration_sha256=configuration,
            parameter_state={"coordinate_index": 0},
            wavelength_convention={
                "unit": "m",
                "requested_field": "requested_wavelength_m",
                "evaluated_field": "evaluated_wavelength_m",
                "frequency_derived_field": "frequency_wavelength_m",
                "frequency_relation": "c_const/frequency",
            },
            expressions={"R": "R", "T": "T", "A": "1-R-T"},
            rows=rows,
        )
        policy = {
            "response_quantity": "A",
            "candidate_polarity": "maximum",
            "passivity_abs_tolerance": 1e-12,
            "closure_abs_tolerance": 1e-12,
            "wavelength_sync_abs_m": 1e-15,
            "flat_response_abs_tolerance": 1e-12,
            "minimum_point_count": 5,
        }
        measurement = {
            "peak_method": "measured_grid",
            "baseline_rule": "declared_response",
            "baseline_response_value": 0.1,
            "fwhm_definition": "half_prominence",
            "fit_support_points": None,
            "fit_support_sensitivity_points": [],
            "local_polynomial_degree": None,
            "fit_max_evaluations": None,
        }
        decision = build_spectral_analysis_decision(bundle, policy)
        characterization = build_spectral_characterization(bundle, decision, measurement)
        flat_state = {
            "state_id": "coord-0",
            "ordinal": 0,
            "declared_predecessor_state_id": None,
            "coordinate_name": "incidence_angle",
            "coordinate_value": 0.0,
            "coordinate_unit": "deg",
            "coordinate_identity_sha256": COORDINATE_IDENTITY,
            "polarization": "TM",
            "source_model_sha256": SOURCE_SHA,
            "configuration_sha256": configuration,
            "material_identity_sha256": MATERIAL_SHA256,
            "search_window_m": {"lower_m": 4.0e-6, "upper_m": 6.0e-6},
            "spectral_bundle": bundle,
            "analysis_decision": decision,
            "candidate_measurements": characterization,
            "optional_field_metrics": {},
        }
        normal = _state(1, 5.1e-6, "coord-0", coordinate_value=5.0)
        states = [flat_state, normal]
        result = build_continuation_states(states_id="flat-then-peak", states=states)
        assert result["states"][0]["candidate"]["classification"] == "flat"
        assert result["states"][0]["candidate"]["measurement_state"] == "not_measured"
        assert result["states"][0]["candidate"]["peak_wavelength_m"] is None
        assert result["states"][1]["candidate"]["classification"] == "interior_candidate"

    def test_optional_field_metrics_are_preserved(self):
        states = _build_dispersive_states(3)
        result = build_continuation_states(states_id="angle-sweep", states=states)
        for index, state in enumerate(result["states"]):
            assert "field_overlap" in state["optional_field_metrics"]
            assert state["optional_field_metrics"]["field_overlap"]["value"] == 0.8 + 0.01 * index

    def test_unsupported_schema_version_is_rejected(self):
        states = _build_dispersive_states(3)
        built = build_continuation_states(states_id="angle-sweep", states=states)
        tampered = deepcopy(built)
        tampered["schema_version"] = "2.0.0"
        with pytest.raises(ValueError, match="schema is unsupported"):
            validate_continuation_states(tampered)
