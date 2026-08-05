"""Solver-free Kirchhoff and thermal-radiation evidence tests."""

from __future__ import annotations

import asyncio
import math
from copy import deepcopy

import numpy as np
import pytest
from pydantic import ValidationError
from src.server import create_server

from comsol_mcp.contracts.thermal_radiation import ThermalRadiationRequest
from comsol_mcp.evidence.thermal_radiation import (
    build_kirchhoff_assessment,
    evaluate_thermal_radiation,
)
from development_kit.tests.mcp_test_support import decode_tool_result

_C = 299_792_458.0
_SIGMA = 5.670_374_419e-8


def _kirchhoff(**overrides):
    value = {
        "absorptivity_evidence_sha256": "a" * 64,
        "absorptivity_evidence_state": "verified",
        "linearity": "verified",
        "time_invariance": "verified",
        "reciprocity": "verified",
        "local_equilibrium": "verified",
        "direction_channel_match": "verified",
        "frequency_channel_match": "verified",
        "polarization_channel_match": "verified",
        "propagation_direction": "negative_z",
        "polarization_basis": "scalar",
        "handedness_convention": "not_applicable",
        "channel_identity_sha256": "b" * 64,
    }
    value.update(overrides)
    return value


def _request(
    coordinates,
    values,
    *,
    representation="wavelength_m",
    angular=None,
    polarization=None,
    detector_path=None,
    optical_quantity="emissivity",
    assessment=None,
    uncertainty=None,
):
    return {
        "temperature_K": 500.0,
        "axis": {
            "representation": representation,
            "coordinates": [float(item) for item in coordinates],
        },
        "angular_grid": angular or {"mode": "lambertian_pi", "theta_rad": [0.0], "phi_rad": [0.0]},
        "polarization": polarization
        or {
            "mode": "scalar",
            "channels": ["scalar"],
            "weights": [],
            "propagation_direction": "negative_z",
            "source_handedness": "not_applicable",
            "analyzer_handedness": "not_applicable",
        },
        "optical_quantity": optical_quantity,
        "values_flat": [float(item) for item in values],
        "uncertainty_flat": [] if uncertainty is None else uncertainty,
        "kirchhoff_assessment": assessment,
        "detector_path": detector_path or {},
        "configuration_sha256": "c" * 64,
        "source_artifact_sha256s": ["d" * 64],
        "artifact_chain_sha256": "e" * 64,
    }


def _decode(result):
    return decode_tool_result(result)


def test_kirchhoff_strict_conditional_unavailable_and_nonreciprocal():
    strict = build_kirchhoff_assessment(_kirchhoff())
    conditional = build_kirchhoff_assessment(_kirchhoff(local_equilibrium="unknown"))
    unavailable = build_kirchhoff_assessment(_kirchhoff(absorptivity_evidence_state="unknown"))
    nonreciprocal = build_kirchhoff_assessment(_kirchhoff(reciprocity="failed"))

    assert strict["disposition"] == "applicable"
    assert conditional["disposition"] == "conditional"
    assert unavailable["disposition"] == "unavailable"
    assert nonreciprocal["disposition"] == "not_applicable"
    assert "requires_reciprocity" in nonreciprocal["reason_codes"][0]


def test_absorptivity_requires_content_bound_applicable_assessment():
    coordinates = [1.0e-6, 2.0e-6]
    conditional = build_kirchhoff_assessment(_kirchhoff(linearity="unknown"))
    with pytest.raises(ValueError, match="applicable"):
        evaluate_thermal_radiation(
            _request(
                coordinates,
                [1.0, 1.0],
                optical_quantity="absorptivity",
                assessment=conditional,
            )
        )
    strict = build_kirchhoff_assessment(_kirchhoff())
    tampered = deepcopy(strict)
    tampered["channel_identity_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="hash"):
        evaluate_thermal_radiation(
            _request(
                coordinates,
                [1.0, 1.0],
                optical_quantity="absorptivity",
                assessment=tampered,
            )
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_thermal_radiation_contract_rejects_nonfinite_values(value):
    request = _request([1.0e-6, 2.0e-6], [1.0, 1.0])
    request["values_flat"][0] = value

    with pytest.raises(ValidationError):
        ThermalRadiationRequest.model_validate(request)


def test_unit_emissivity_recovers_stefan_boltzmann_on_finite_domain():
    temperature = 500.0
    wavelengths = np.geomspace(0.1e-6, 1000.0e-6, 1800)
    request = _request(wavelengths, np.ones(len(wavelengths)))
    request["temperature_K"] = temperature
    evidence = evaluate_thermal_radiation(request)
    expected = _SIGMA * temperature**4
    assert evidence["radiated_power_W_m2"] == pytest.approx(expected, rel=4e-4)


def test_lambertian_grid_recovers_pi_projected_solid_angle():
    theta = np.linspace(0.0, math.pi / 2.0, 16)
    phi = np.linspace(0.0, 2.0 * math.pi, 32)
    angular = {
        "mode": "grid_trapezoid",
        "theta_rad": [float(item) for item in theta],
        "phi_rad": [float(item) for item in phi],
    }
    values = [1.0] * (2 * len(theta) * len(phi))
    evidence = evaluate_thermal_radiation(_request([1.0e-6, 2.0e-6], values, angular=angular))
    assert evidence["angular"]["projected_solid_angle"] == pytest.approx(math.pi, rel=4e-3)


def test_wavelength_unit_and_axis_jacobians_conserve_integrated_power():
    wavelengths = np.geomspace(1.0e-6, 20.0e-6, 800)
    values = [0.7] * len(wavelengths)
    wavelength_m = evaluate_thermal_radiation(_request(wavelengths, values))
    wavelength_um = evaluate_thermal_radiation(
        _request(wavelengths * 1.0e6, values, representation="wavelength_um")
    )
    frequency = evaluate_thermal_radiation(
        _request(_C / wavelengths, values, representation="frequency_Hz")
    )
    wavenumber = evaluate_thermal_radiation(
        _request(1.0 / wavelengths, values, representation="wavenumber_m_inv")
    )
    reference = wavelength_m["radiated_power_W_m2"]
    assert wavelength_um["radiated_power_W_m2"] == pytest.approx(reference, rel=1e-12)
    assert wavelength_um["axis_exitance_density"][0] == pytest.approx(
        wavelength_m["axis_exitance_density"][0] * 1.0e-6,
        rel=1e-14,
    )
    assert frequency["radiated_power_W_m2"] == pytest.approx(reference, rel=3e-5)
    assert wavenumber["radiated_power_W_m2"] == pytest.approx(reference, rel=3e-5)


def test_equal_te_tm_reproduces_scalar_unpolarized_output():
    coordinates = [2.0e-6, 3.0e-6]
    scalar = evaluate_thermal_radiation(_request(coordinates, [0.4, 0.4]))
    polarization = {
        "mode": "te_tm_incoherent",
        "channels": ["TE", "TM"],
        "weights": [0.5, 0.5],
        "propagation_direction": "negative_z",
        "source_handedness": "not_applicable",
        "analyzer_handedness": "not_applicable",
    }
    te_tm = evaluate_thermal_radiation(
        _request(coordinates, [0.4, 0.4, 0.4, 0.4], polarization=polarization)
    )
    assert te_tm["radiated_power_W_m2"] == pytest.approx(scalar["radiated_power_W_m2"], rel=1e-14)


def test_stokes_rotation_preserves_invariants_and_handedness_mismatch_fails():
    polarization = {
        "mode": "stokes_mueller",
        "channels": ["I", "Q", "U", "V"],
        "weights": [],
        "propagation_direction": "negative_z",
        "source_handedness": "explicit_stokes",
        "analyzer_handedness": "explicit_stokes",
        "analyzer_stokes": [1.0, 0.0, 0.0, 0.0],
        "basis_rotation_rad": 0.7,
    }
    values = [0.8, 0.3, 0.4, 0.1] * 2
    evidence = evaluate_thermal_radiation(
        _request([2.0e-6, 3.0e-6], values, polarization=polarization)
    )
    invariant = evidence["polarization"]["stokes_invariants"][0]
    assert invariant["polarization_magnitude"] == pytest.approx(math.sqrt(0.26))
    mismatch = deepcopy(polarization)
    mismatch["analyzer_handedness"] = "ieee_observer"
    with pytest.raises(ValueError, match="handedness"):
        evaluate_thermal_radiation(_request([2.0e-6, 3.0e-6], values, polarization=mismatch))


def test_detector_gas_and_boxcar_kernels_are_bounded_and_monotonic():
    coordinates = [1.0, 2.0, 3.0, 4.0]
    zero = evaluate_thermal_radiation(
        _request(
            coordinates,
            [1.0] * 4,
            representation="wavelength_um",
            detector_path={
                "kernel_source_sha256s": ["1" * 64],
                "gas_absorption_per_m": [2.0] * 4,
                "gas_path_length_m": 0.0,
                "gas_concentration_scale": 3.0,
                "aperture_weights": [0.0, 1.0, 1.0, 0.0],
            },
        )
    )
    positive = evaluate_thermal_radiation(
        _request(
            coordinates,
            [1.0] * 4,
            representation="wavelength_um",
            detector_path={
                "kernel_source_sha256s": ["1" * 64],
                "gas_absorption_per_m": [2.0] * 4,
                "gas_path_length_m": 0.5,
                "gas_concentration_scale": 3.0,
                "aperture_weights": [0.0, 1.0, 1.0, 0.0],
            },
        )
    )
    assert zero["detector_path"]["gas_transmission"] == [1.0] * 4
    assert positive["detected_signal"] < zero["detected_signal"]
    density = zero["axis_exitance_density"]
    expected_boxcar = density[1] + density[2]
    assert zero["detected_signal"] == pytest.approx(expected_boxcar)
    bad = _request(
        coordinates,
        [1.0] * 4,
        representation="wavelength_um",
        detector_path={
            "kernel_source_sha256s": ["1" * 64],
            "gas_absorption_per_m": [-1.0] * 4,
        },
    )
    with pytest.raises(ValidationError):
        evaluate_thermal_radiation(bad)


@pytest.mark.parametrize(
    "field_name",
    [
        "gas_absorption_per_m",
        "aperture_weights",
        "optics_transmission",
        "analyzer_response",
        "detector_response",
        "reference_response",
        "background_response",
    ],
)
def test_detector_series_shape_fails_at_the_typed_boundary(field_name):
    request = _request([1.0e-6, 2.0e-6], [0.5, 0.5])
    request["detector_path"] = {field_name: [1.0]}
    with pytest.raises(ValidationError, match="spectral axis"):
        ThermalRadiationRequest.model_validate(request)


def test_uncertainty_and_provenance_are_bound_to_evidence():
    evidence = evaluate_thermal_radiation(
        _request([2.0e-6, 3.0e-6], [0.5, 0.5], uncertainty=[0.01, 0.01])
    )
    assert evidence["detected_standard_uncertainty"] > 0.0
    assert evidence["configuration_sha256"] == "c" * 64
    assert evidence["source_artifact_sha256s"] == ["d" * 64]
    assert len(evidence["request_sha256"]) == 64
    assert len(evidence["evidence_sha256"]) == 64
    assert evidence["axis"]["extrapolation"] is False


def test_public_m1_dispatch_is_solver_free():
    server = create_server("m1-public", profile="basic_fem")
    assessment = _decode(
        asyncio.run(server.call_tool("thermal_kirchhoff_assess", {"request": _kirchhoff()}))
    )
    radiation = _decode(
        asyncio.run(
            server.call_tool(
                "thermal_radiation_evaluate",
                {"request": _request([2.0e-6, 3.0e-6], [0.5, 0.5])},
            )
        )
    )
    assert assessment["success"] is True
    assert assessment["solver_started"] is False
    assert radiation["success"] is True
    assert radiation["solver_started"] is False
