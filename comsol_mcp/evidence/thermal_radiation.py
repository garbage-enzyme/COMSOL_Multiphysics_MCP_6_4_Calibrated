"""Solver-free Kirchhoff assessment and channel-resolved thermal radiation."""

from __future__ import annotations

import math
from typing import Any

from comsol_mcp.contracts.thermal_radiation import (
    KirchhoffAssessmentReceipt,
    KirchhoffAssessmentRequest,
    ThermalRadiationRequest,
)
from comsol_mcp.durable import canonical_json_v1, domain_sha256_v2

_C = 299_792_458.0
_H = 6.626_070_15e-34
_K_B = 1.380_649e-23


def build_kirchhoff_assessment(
    value: KirchhoffAssessmentRequest | dict[str, Any],
) -> dict[str, Any]:
    """Assess whether directional absorptivity may be used as emissivity."""
    request = KirchhoffAssessmentRequest.model_validate(value)
    checks = {
        "linearity": request.linearity,
        "time_invariance": request.time_invariance,
        "reciprocity": request.reciprocity,
        "local_equilibrium": request.local_equilibrium,
        "direction_channel_match": request.direction_channel_match,
        "frequency_channel_match": request.frequency_channel_match,
        "polarization_channel_match": request.polarization_channel_match,
    }
    if request.absorptivity_evidence_state == "unknown":
        disposition = "unavailable"
        reasons = ["absorptivity_evidence_unavailable"]
    elif request.reciprocity == "failed":
        disposition = "not_applicable"
        reasons = ["ordinary_directional_kirchhoff_requires_reciprocity"]
    elif any(state == "failed" for state in checks.values()):
        disposition = "not_applicable"
        reasons = [f"{name}_failed" for name, state in checks.items() if state == "failed"]
    elif any(state == "unknown" for state in checks.values()):
        disposition = "conditional"
        reasons = [f"{name}_unknown" for name, state in checks.items() if state == "unknown"]
    else:
        disposition = "applicable"
        reasons = ["exact_channel_requirements_verified"]
    body = {
        "schema_name": "comsol_mcp.kirchhoff_assessment",
        "schema_version": "1.0.0",
        "disposition": disposition,
        "reason_codes": reasons,
        "absorptivity_evidence_sha256": request.absorptivity_evidence_sha256,
        "absorptivity_evidence_state": request.absorptivity_evidence_state,
        "channel_identity_sha256": request.channel_identity_sha256,
        "checks": checks,
        "propagation_direction": request.propagation_direction,
        "polarization_basis": request.polarization_basis,
        "handedness_convention": request.handedness_convention,
    }
    return {
        **body,
        "assessment_sha256": domain_sha256_v2("kirchhoff_assessment/1.0.0", body),
    }


def _validate_assessment(value: KirchhoffAssessmentReceipt) -> dict[str, Any]:
    body = value.model_dump(mode="python", exclude={"assessment_sha256"})
    expected = domain_sha256_v2("kirchhoff_assessment/1.0.0", body)
    if value.assessment_sha256 != expected:
        raise ValueError("Kirchhoff assessment hash does not match its content")
    checks = value.checks.model_dump(mode="python")
    if value.disposition == "applicable" and (
        value.absorptivity_evidence_state != "verified"
        or any(state != "verified" for state in checks.values())
    ):
        raise ValueError("applicable Kirchhoff assessment contains unverified checks")
    normalized = value.model_dump(mode="python")
    if not isinstance(normalized, dict):
        raise ValueError("Kirchhoff assessment must normalize to an object")
    return normalized


def _planck_wavelength(wavelength_m: float, temperature_K: float) -> float:
    exponent = _H * _C / (wavelength_m * _K_B * temperature_K)
    numerator = 2.0 * _H * _C * _C / wavelength_m**5
    if exponent > 700.0:
        return numerator * math.exp(-exponent)
    return numerator / math.expm1(exponent)


def _axis_wavelength_and_jacobian(representation: str, coordinate: float) -> tuple[float, float]:
    if representation == "wavelength_m":
        return coordinate, 1.0
    if representation == "wavelength_um":
        return coordinate * 1.0e-6, 1.0e-6
    if representation == "frequency_Hz":
        wavelength = _C / coordinate
        return wavelength, wavelength * wavelength / _C
    wavelength = 1.0 / coordinate
    return wavelength, wavelength * wavelength


def _trapezoid_weights(coordinates: list[float]) -> list[float]:
    if len(coordinates) < 2 or any(
        right <= left for left, right in zip(coordinates, coordinates[1:])
    ):
        raise ValueError("integration coordinates must be strictly increasing")
    weights = [0.0] * len(coordinates)
    weights[0] = (coordinates[1] - coordinates[0]) / 2.0
    weights[-1] = (coordinates[-1] - coordinates[-2]) / 2.0
    for index in range(1, len(coordinates) - 1):
        weights[index] = (coordinates[index + 1] - coordinates[index - 1]) / 2.0
    return weights


def _series(values: list[float], count: int, default: float, label: str) -> list[float]:
    if not values:
        return [default] * count
    if len(values) != count:
        raise ValueError(f"{label} length must match the spectral axis")
    return list(values)


def _reorder_spectral_flat(values: list[float], order: list[int], per_spectral: int) -> list[float]:
    return [
        values[source * per_spectral + offset] for source in order for offset in range(per_spectral)
    ]


def _polarization_value(
    values: list[float],
    uncertainty: list[float],
    mode: str,
    weights: list[float],
    analyzer: list[float],
    rotation: float,
) -> tuple[float, float, dict[str, float] | None]:
    if mode == "scalar":
        if not 0.0 <= values[0] <= 1.0:
            raise ValueError("scalar emissivity must lie in [0, 1]")
        return values[0], uncertainty[0], None
    if mode == "te_tm_incoherent":
        if any(not 0.0 <= item <= 1.0 for item in values):
            raise ValueError("TE/TM emissivity must lie in [0, 1]")
        combined = sum(weight * item for weight, item in zip(weights, values, strict=True))
        sigma = math.sqrt(
            sum((weight * item) ** 2 for weight, item in zip(weights, uncertainty, strict=True))
        )
        return combined, sigma, None
    intensity, q_value, u_value, v_value = values
    if not 0.0 <= intensity <= 1.0:
        raise ValueError("Stokes I emissivity must lie in [0, 1]")
    polarized = math.sqrt(q_value * q_value + u_value * u_value + v_value * v_value)
    if polarized > intensity + 1.0e-12:
        raise ValueError("Stokes polarization magnitude cannot exceed I")
    cosine = math.cos(2.0 * rotation)
    sine = math.sin(2.0 * rotation)
    q_rotated = q_value * cosine + u_value * sine
    u_rotated = -q_value * sine + u_value * cosine
    rotated = [intensity, q_rotated, u_rotated, v_value]
    response = 0.5 * sum(
        analyzer_item * stokes_item
        for analyzer_item, stokes_item in zip(analyzer, rotated, strict=True)
    )
    if response < -1.0e-12:
        raise ValueError("Stokes analyzer produced negative emissivity")
    sigma = 0.5 * math.sqrt(
        sum(
            (analyzer_item * uncertainty_item) ** 2
            for analyzer_item, uncertainty_item in zip(analyzer, uncertainty, strict=True)
        )
    )
    return (
        max(0.0, response),
        sigma,
        {
            "I": intensity,
            "polarization_magnitude": polarized,
            "degree_of_polarization": 0.0 if intensity == 0.0 else polarized / intensity,
        },
    )


def evaluate_thermal_radiation(
    value: ThermalRadiationRequest | dict[str, Any],
) -> dict[str, Any]:
    """Evaluate bounded channel-resolved thermal radiation without COMSOL."""
    request = ThermalRadiationRequest.model_validate(value)
    assessment: dict[str, Any] | None
    if request.optical_quantity == "absorptivity":
        if request.kirchhoff_assessment is None:
            raise ValueError("absorptivity requires a strict Kirchhoff assessment")
        assessment = _validate_assessment(request.kirchhoff_assessment)
        if assessment["disposition"] != "applicable":
            raise ValueError("absorptivity cannot be used without an applicable assessment")
    else:
        assessment = (
            None
            if request.kirchhoff_assessment is None
            else _validate_assessment(request.kirchhoff_assessment)
        )
    polarization = request.polarization
    if (
        polarization.mode == "stokes_mueller"
        and polarization.source_handedness != polarization.analyzer_handedness
    ):
        raise ValueError("Stokes source and analyzer handedness conventions must match")
    spectral_count = len(request.axis.coordinates)
    theta_count = len(request.angular_grid.theta_rad)
    phi_count = len(request.angular_grid.phi_rad)
    channel_count = len(polarization.channels)
    per_spectral = theta_count * phi_count * channel_count
    coordinate_order = sorted(
        range(spectral_count), key=lambda index: request.axis.coordinates[index]
    )
    coordinates = [request.axis.coordinates[index] for index in coordinate_order]
    _trapezoid_weights(coordinates)
    values = _reorder_spectral_flat(request.values_flat, coordinate_order, per_spectral)
    raw_uncertainty = request.uncertainty_flat or [0.0] * len(request.values_flat)
    uncertainty = _reorder_spectral_flat(raw_uncertainty, coordinate_order, per_spectral)
    theta = list(request.angular_grid.theta_rad)
    phi = list(request.angular_grid.phi_rad)
    if request.angular_grid.mode == "lambertian_pi":
        angular_weights = [math.pi]
    else:
        theta_weights = _trapezoid_weights(theta)
        phi_weights = _trapezoid_weights(phi)
        angular_weights = [
            theta_weights[t_index]
            * phi_weights[p_index]
            * math.cos(theta_value)
            * math.sin(theta_value)
            for t_index, theta_value in enumerate(theta)
            for p_index, _phi_value in enumerate(phi)
        ]
    detector = request.detector_path
    gas_absorption = _series(
        detector.gas_absorption_per_m, spectral_count, 0.0, "gas_absorption_per_m"
    )
    aperture = _series(detector.aperture_weights, spectral_count, 1.0, "aperture_weights")
    optics = _series(detector.optics_transmission, spectral_count, 1.0, "optics_transmission")
    analyzer_response = _series(
        detector.analyzer_response, spectral_count, 1.0, "analyzer_response"
    )
    detector_response = _series(
        detector.detector_response, spectral_count, 1.0, "detector_response"
    )
    reference_response = _series(
        detector.reference_response, spectral_count, 0.0, "reference_response"
    )
    background_response = _series(
        detector.background_response, spectral_count, 0.0, "background_response"
    )
    for series in (
        gas_absorption,
        aperture,
        optics,
        analyzer_response,
        detector_response,
        reference_response,
        background_response,
    ):
        reordered = [series[index] for index in coordinate_order]
        series[:] = reordered
    axis_density: list[float] = []
    detected_density: list[float] = []
    reference_density: list[float] = []
    background_density: list[float] = []
    detected_sigma_density: list[float] = []
    stokes_invariants: list[dict[str, float]] = []
    gas_transmission: list[float] = []
    for spectral_index, coordinate in enumerate(coordinates):
        wavelength_m, jacobian = _axis_wavelength_and_jacobian(
            request.axis.representation, coordinate
        )
        blackbody = _planck_wavelength(wavelength_m, request.temperature_K) * jacobian
        angular_sum = 0.0
        angular_variance = 0.0
        for angular_index, angular_weight in enumerate(angular_weights):
            start = spectral_index * per_spectral + angular_index * channel_count
            channel_values = values[start : start + channel_count]
            channel_uncertainty = uncertainty[start : start + channel_count]
            combined, combined_sigma, invariants = _polarization_value(
                channel_values,
                channel_uncertainty,
                polarization.mode,
                polarization.weights,
                polarization.analyzer_stokes,
                polarization.basis_rotation_rad,
            )
            angular_sum += angular_weight * combined
            angular_variance += (angular_weight * combined_sigma) ** 2
            if invariants is not None:
                stokes_invariants.append(invariants)
        exitance_density = blackbody * angular_sum
        exitance_sigma = blackbody * math.sqrt(angular_variance)
        gas = math.exp(
            -gas_absorption[spectral_index]
            * detector.gas_path_length_m
            * detector.gas_concentration_scale
        )
        gas_transmission.append(gas)
        kernel = (
            gas
            * aperture[spectral_index]
            * optics[spectral_index]
            * analyzer_response[spectral_index]
            * detector_response[spectral_index]
        )
        axis_density.append(exitance_density)
        detected_density.append(exitance_density * kernel)
        reference_density.append(exitance_density * reference_response[spectral_index])
        background_density.append(exitance_density * background_response[spectral_index])
        detected_sigma_density.append(exitance_sigma * kernel)
    spectral_weights = _trapezoid_weights(coordinates)

    def integrate(series: list[float]) -> float:
        return sum(weight * item for weight, item in zip(spectral_weights, series, strict=True))

    radiated = integrate(axis_density)
    detected = integrate(detected_density)
    uncertainty_total = math.sqrt(
        sum(
            (weight * sigma) ** 2
            for weight, sigma in zip(spectral_weights, detected_sigma_density, strict=True)
        )
    )
    request_body = request.model_dump(mode="python")
    request_sha = domain_sha256_v2("thermal_radiation_request/1.0.0", request_body)
    body = {
        "schema_name": "comsol_mcp.thermal_radiation_evidence",
        "schema_version": "1.0.0",
        "request_sha256": request_sha,
        "configuration_sha256": request.configuration_sha256,
        "source_artifact_sha256s": sorted(request.source_artifact_sha256s),
        "artifact_chain_sha256": request.artifact_chain_sha256,
        "kirchhoff_assessment_sha256": (
            None if assessment is None else assessment["assessment_sha256"]
        ),
        "optical_quantity": request.optical_quantity,
        "temperature_K": request.temperature_K,
        "axis": {
            "representation": request.axis.representation,
            "coordinate_min": coordinates[0],
            "coordinate_max": coordinates[-1],
            "point_count": spectral_count,
            "integration_method": request.integration_method,
            "interpolation": request.interpolation,
            "extrapolation": request.extrapolation,
            "density_unit": {
                "wavelength_m": "W m^-2 m^-1",
                "wavelength_um": "W m^-2 um^-1",
                "frequency_Hz": "W m^-2 Hz^-1",
                "wavenumber_m_inv": "W m^-2 per_(m^-1)",
            }[request.axis.representation],
        },
        "angular": {
            "mode": request.angular_grid.mode,
            "theta_coverage_rad": [min(theta), max(theta)],
            "phi_coverage_rad": [min(phi), max(phi)],
            "projected_solid_angle": sum(angular_weights),
        },
        "polarization": {
            "mode": polarization.mode,
            "channels": polarization.channels,
            "weights": polarization.weights,
            "propagation_direction": polarization.propagation_direction,
            "handedness": polarization.source_handedness,
            "basis_rotation_rad": polarization.basis_rotation_rad,
            "stokes_invariants": stokes_invariants[:64],
        },
        "detector_path": {
            "kernel_source_sha256s": sorted(detector.kernel_source_sha256s),
            "gas_transmission": gas_transmission,
            "zero_gas_path_or_concentration": (
                detector.gas_path_length_m == 0.0 or detector.gas_concentration_scale == 0.0
            ),
            "kernel_extrapolated": False,
        },
        "axis_exitance_density": axis_density,
        "detected_density": detected_density,
        "radiated_power_W_m2": radiated,
        "detected_signal": detected,
        "reference_signal": integrate(reference_density),
        "background_signal": integrate(background_density),
        "detected_standard_uncertainty": uncertainty_total,
        "uncertainty_method": "independent_linear_quadrature",
        "solver_started": False,
        "filesystem_modified": False,
    }
    canonical_json_v1(body)
    return {
        **body,
        "evidence_sha256": domain_sha256_v2("thermal_radiation_evidence/1.0.0", body),
    }


__all__ = ["build_kirchhoff_assessment", "evaluate_thermal_radiation"]
