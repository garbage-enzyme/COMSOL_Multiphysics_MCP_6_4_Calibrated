"""Solver-free branch-continuation planning regression tests."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
from mcp.server.fastmcp import FastMCP

from src.evidence.branch_continuation import (
    BRANCH_CONTINUATION_SCHEMA_VERSION,
    BRANCH_CONTINUATION_STATES_SCHEMA,
    MAX_CONTINUATION_STATES,
    build_continuation_states,
    plan_branch_continuation,
    validate_branch_continuation_plan,
    validate_continuation_states,
)
from src.evidence.spectral_characterization import (
    build_spectral_analysis_decision,
    build_spectral_characterization,
    build_spectral_point_bundle,
)
from src.tools.branch_continuation import register_branch_continuation_tools


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

    def test_boundary_high_state_records_boundary_side(self):
        monotonically_increasing = [0.1, 0.2, 0.3, 0.5, 0.7, 0.85, 0.95]
        configuration = _hex_id("boundary-config-0")
        wavelengths = [5.0e-6 + offset * 0.05e-6 for offset in range(-3, 4)]
        rows = []
        for row_index, (wavelength, absorption) in enumerate(
            zip(wavelengths, monotonically_increasing)
        ):
            raw = {"state": 0, "row": row_index, "wavelength": wavelength}
            rows.append({
                "row_id": f"boundary-0-point-{row_index}",
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
            bundle_id="spectrum-boundary-0",
            source_model={
                "relative_identity": "fixtures/boundary.mph",
                "sha256": _hex_id("boundary-source-0"),
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
        boundary_state = {
            "state_id": "coord-0",
            "ordinal": 0,
            "declared_predecessor_state_id": None,
            "coordinate_name": "incidence_angle",
            "coordinate_value": 0.0,
            "coordinate_unit": "deg",
            "coordinate_identity_sha256": _hex_id("boundary-coord-0"),
            "polarization": "TM",
            "source_model_sha256": _hex_id("boundary-source-0"),
            "configuration_sha256": configuration,
            "material_identity_sha256": MATERIAL_SHA256,
            "search_window_m": {"lower_m": 4.0e-6, "upper_m": 6.0e-6},
            "spectral_bundle": bundle,
            "analysis_decision": decision,
            "candidate_measurements": characterization,
            "optional_field_metrics": {},
        }
        normal = _state(1, 5.1e-6, "coord-0", coordinate_value=5.0)
        result = build_continuation_states(states_id="boundary-then-peak", states=[boundary_state, normal])
        assert result["states"][0]["candidate"]["classification"] == "boundary_high"
        assert result["states"][0]["candidate"]["boundary_side"] == "upper"


def _continuation_policy(*, guard_window_m=0.5e-6, max_expansions=3,
                         max_total_window_m=4.0e-6, declared_cap_reached=False,
                         continuity_rule="guard_window", stop_policy="continue_all_declared"):
    return {
        "policy_id": "continuation-policy-test",
        "guard_window_m": guard_window_m,
        "absolute_bounds_m": {"lower_m": 3.0e-6, "upper_m": 7.0e-6},
        "max_expansions": max_expansions,
        "max_total_window_m": max_total_window_m,
        "point_budget": 64,
        "stop_policy": stop_policy,
        "continuity_rule": continuity_rule,
        "declared_cap_reached": declared_cap_reached,
    }


class TestBranchContinuationPlanning:
    def test_dispersive_branch_followed_is_accepted(self):
        states_input = _build_dispersive_states(4, shift=0.1e-6)
        states = build_continuation_states(states_id="dispersive", states=states_input)
        plan = plan_branch_continuation(states, _continuation_policy(guard_window_m=0.3e-6))
        assert plan["scientific_disposition"] == "accepted"
        assert plan["reason_code"] == "all_transitions_branch_followed"
        assert plan["branch_followed_transition_count"] == 3
        assert plan["branch_disappearance_claimed"] is False
        assert plan["undeclared_coordinate_started"] is False
        assert plan["total_expansions_proposed"] == 0

    def test_plan_sha256_is_deterministic(self):
        states_input = _build_dispersive_states(4, shift=0.1e-6)
        states = build_continuation_states(states_id="dispersive", states=states_input)
        first = plan_branch_continuation(states, _continuation_policy())
        second = plan_branch_continuation(
            build_continuation_states(states_id="dispersive", states=deepcopy(states_input)),
            _continuation_policy(),
        )
        assert first["plan_sha256"] == second["plan_sha256"]

    def test_validate_branch_continuation_plan_round_trips(self):
        states_input = _build_dispersive_states(4, shift=0.1e-6)
        states = build_continuation_states(states_id="dispersive", states=states_input)
        plan = plan_branch_continuation(states, _continuation_policy())
        validated = validate_branch_continuation_plan(plan, states=states)
        assert validated == plan

    def test_plan_hash_tampering_is_rejected(self):
        states_input = _build_dispersive_states(4, shift=0.1e-6)
        states = build_continuation_states(states_id="dispersive", states=states_input)
        plan = plan_branch_continuation(states, _continuation_policy())
        tampered = deepcopy(plan)
        tampered["plan_sha256"] = "0" * 64
        with pytest.raises(ValueError, match="noncanonical"):
            validate_branch_continuation_plan(tampered, states=states)

    def test_peak_beyond_guard_window_is_not_followed(self):
        states_input = _build_dispersive_states(3, shift=1.0e-6)
        states = build_continuation_states(states_id="wide-shift", states=states_input)
        plan = plan_branch_continuation(states, _continuation_policy(guard_window_m=0.1e-6))
        assert plan["scientific_disposition"] == "residual"
        assert plan["reason_code"] == "branch_not_followed"
        assert plan["branch_followed_transition_count"] == 0

    def test_peak_beyond_guard_at_declared_cap_is_unresolved(self):
        states_input = _build_dispersive_states(3, shift=1.0e-6)
        states = build_continuation_states(states_id="wide-shift", states=states_input)
        plan = plan_branch_continuation(
            states, _continuation_policy(guard_window_m=0.1e-6, declared_cap_reached=True)
        )
        assert plan["scientific_disposition"] == "unresolved_at_declared_cap"
        assert plan["reason_code"] == "branch_not_followed_at_declared_cap"

    def test_branch_disappearance_is_never_claimed(self):
        flat_values = [0.5] * 7
        configuration = _hex_id("flat-config")
        wavelengths = [5.0e-6 + offset * 0.05e-6 for offset in range(-3, 4)]
        rows = []
        for row_index, (wavelength, absorption) in enumerate(zip(wavelengths, flat_values)):
            raw = {"state": 1, "row": row_index, "wavelength": wavelength}
            rows.append({
                "row_id": f"flat-1-{row_index}",
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
        flat_bundle = build_spectral_point_bundle(
            bundle_id="flat-1",
            source_model={
                "relative_identity": "fixtures/flat-1.mph",
                "sha256": _hex_id("flat-source-1"),
            },
            configuration_sha256=configuration,
            parameter_state={"coordinate_index": 1},
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
        flat_policy = {
            "response_quantity": "A",
            "candidate_polarity": "maximum",
            "passivity_abs_tolerance": 1e-12,
            "closure_abs_tolerance": 1e-12,
            "wavelength_sync_abs_m": 1e-15,
            "flat_response_abs_tolerance": 1e-12,
            "minimum_point_count": 5,
        }
        flat_measurement = {
            "peak_method": "measured_grid",
            "baseline_rule": "declared_response",
            "baseline_response_value": 0.1,
            "fwhm_definition": "half_prominence",
            "fit_support_points": None,
            "fit_support_sensitivity_points": [],
            "local_polynomial_degree": None,
            "fit_max_evaluations": None,
        }
        flat_decision = build_spectral_analysis_decision(flat_bundle, flat_policy)
        flat_characterization = build_spectral_characterization(
            flat_bundle, flat_decision, flat_measurement
        )
        flat_state = {
            "state_id": "coord-1",
            "ordinal": 1,
            "declared_predecessor_state_id": "coord-0",
            "coordinate_name": "incidence_angle",
            "coordinate_value": 5.0,
            "coordinate_unit": "deg",
            "coordinate_identity_sha256": _hex_id("flat-coord-1"),
            "polarization": "TM",
            "source_model_sha256": _hex_id("flat-source-1"),
            "configuration_sha256": configuration,
            "material_identity_sha256": MATERIAL_SHA256,
            "search_window_m": {"lower_m": 4.0e-6, "upper_m": 6.0e-6},
            "spectral_bundle": flat_bundle,
            "analysis_decision": flat_decision,
            "candidate_measurements": flat_characterization,
            "optional_field_metrics": {},
        }
        normal_state = _state(0, 5.0e-6, None, coordinate_value=0.0)
        states = build_continuation_states(
            states_id="peak-then-flat", states=[normal_state, flat_state]
        )
        plan = plan_branch_continuation(
            states, _continuation_policy(declared_cap_reached=True)
        )
        assert plan["branch_disappearance_claimed"] is False
        assert plan["scientific_disposition"] == "unresolved_at_declared_cap"
        assert plan["coordinate_transitions"][0]["current_peak_wavelength_m"] is None
        assert plan["coordinate_transitions"][0]["branch_followed"] is False

    def test_next_request_window_uses_current_peak(self):
        states_input = _build_dispersive_states(3, shift=0.1e-6)
        states = build_continuation_states(states_id="dispersive", states=states_input)
        plan = plan_branch_continuation(states, _continuation_policy(guard_window_m=0.3e-6))
        last = plan["coordinate_transitions"][-1]
        assert last["next_request_window_m"] is not None
        current_peak = last["current_peak_wavelength_m"]
        guard = 0.3e-6
        assert last["next_request_window_m"]["lower_m"] == pytest.approx(
            max(current_peak - guard, 3.0e-6)
        )
        assert last["next_request_window_m"]["upper_m"] == pytest.approx(
            min(current_peak + guard, 7.0e-6)
        )

    def test_invalid_policy_guard_window_is_rejected(self):
        states_input = _build_dispersive_states(3)
        states = build_continuation_states(states_id="test", states=states_input)
        with pytest.raises(ValueError, match="guard_window_m"):
            plan_branch_continuation(
                states, _continuation_policy(guard_window_m=-0.1e-6)
            )

    def test_invalid_policy_bounds_not_containing_search_windows(self):
        states_input = _build_dispersive_states(3)
        states = build_continuation_states(states_id="test", states=states_input)
        policy = _continuation_policy()
        policy["absolute_bounds_m"] = {"lower_m": 4.5e-6, "upper_m": 5.5e-6}
        with pytest.raises(ValueError, match="absolute_bounds_m"):
            plan_branch_continuation(states, policy)

    def test_invalid_continuity_rule_is_rejected(self):
        states_input = _build_dispersive_states(3)
        states = build_continuation_states(states_id="test", states=states_input)
        policy = _continuation_policy()
        policy["continuity_rule"] = "auto"
        with pytest.raises(ValueError, match="continuity_rule"):
            plan_branch_continuation(states, policy)


def test_public_tool_returns_separate_states_and_plan_artifacts():
    server = FastMCP("branch-continuation-test")
    register_branch_continuation_tools(server)
    result = server._tool_manager._tools["branch_continuation_plan"].fn(
        states_spec={
            "states_id": "dispersive",
            "states": _build_dispersive_states(3, shift=0.1e-6),
        },
        continuation_policy=_continuation_policy(guard_window_m=0.3e-6),
    )

    assert result["success"] is True
    assert result["scientific_disposition"] == "accepted"
    assert result["artifact_separation"] == {
        "ordered_evidence": "continuation_states",
        "policy_plan": "branch_continuation_plan",
    }
    assert result["branch_continuation_plan"]["states_sha256"] == result[
        "continuation_states"
    ]["states_sha256"]
    assert result["branch_disappearance_claimed"] is False
    assert result["undeclared_coordinate_started"] is False
    assert result["solver_started"] is False
    assert result["filesystem_modified"] is False


def test_public_tool_accepts_canonical_states_and_rejects_ambiguous_input():
    states = build_continuation_states(
        states_id="dispersive", states=_build_dispersive_states(3, shift=0.1e-6)
    )
    server = FastMCP("branch-continuation-input-test")
    register_branch_continuation_tools(server)
    tool = server._tool_manager._tools["branch_continuation_plan"]

    accepted = tool.fn(
        continuation_states=states,
        continuation_policy=_continuation_policy(guard_window_m=0.3e-6),
    )
    rejected = tool.fn(continuation_policy=_continuation_policy())

    assert accepted["success"] is True
    assert rejected["success"] is False
    assert rejected["scientific_disposition"] == "invalid_evidence"
    assert "exactly one" in rejected["error"]
    assert rejected["solver_started"] is False


def test_public_branch_continuation_tool_never_constructs_a_comsol_client():
    code = """
import mph
mph.Client = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('Client called'))
from mcp.server.fastmcp import FastMCP
from src.tools.branch_continuation import register_branch_continuation_tools
server = FastMCP('solver-free-branch-continuation-subprocess')
register_branch_continuation_tools(server)
result = server._tool_manager._tools['branch_continuation_plan'].fn(continuation_policy={})
assert result['success'] is False
assert result['solver_started'] is False
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2], capture_output=True, text=True,
        timeout=20, check=False,
    )
    assert completed.returncode == 0, completed.stderr


def _canonical_hash(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def test_self_rehashed_malformed_state_summary_still_fails_closed():
    states = build_continuation_states(
        states_id="dispersive", states=_build_dispersive_states(3, shift=0.1e-6)
    )
    malformed = deepcopy(states)
    state = malformed["states"][1]
    state["candidate"]["peak_wavelength_m"] = "5.02e-6"
    state_body = dict(state)
    state_body.pop("state_sha256")
    state["state_sha256"] = _canonical_hash(state_body)
    states_body = dict(malformed)
    states_body.pop("states_sha256")
    malformed["states_sha256"] = _canonical_hash(states_body)

    with pytest.raises(ValueError, match="numeric"):
        validate_continuation_states(malformed)


def test_missing_middle_state_and_reordered_states_fail_closed():
    states = _build_dispersive_states(3)
    del states[1]
    with pytest.raises(ValueError, match="ordinal|adjacency"):
        build_continuation_states(states_id="missing-middle", states=states)

    states = _build_dispersive_states(3)
    states[1], states[2] = states[2], states[1]
    with pytest.raises(ValueError, match="ordinal|adjacency"):
        build_continuation_states(states_id="reordered", states=states)
