"""Solver-free validation and evaluation of thermal material ledgers."""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

from comsol_mcp.contracts.thermal_material import (
    ThermalMaterialEvaluationRequest,
    ThermalMaterialLedger,
)
from comsol_mcp.durable import canonical_json_v1, domain_sha256_v2

_C = 299_792_458.0


class DeclaredDiscontinuityError(ValueError):
    """Interpolation crossed a declared material discontinuity."""


def _strictly_increasing(values: list[float], label: str) -> None:
    if any(right <= left for left, right in zip(values, values[1:])):
        raise ValueError(f"{label} must be strictly increasing")


def _model_policy(model: Any) -> Any:
    if model.model_kind in {"nk_table", "permittivity_table"}:
        return model.interpolation.extrapolation
    return model.extrapolation


def _validate_state_model(state: Any) -> None:
    model = state.optical_model
    if model.model_kind not in {"nk_table", "permittivity_table"}:
        return
    wavelengths = list(model.wavelengths_m)
    temperatures = list(model.temperatures_K)
    _strictly_increasing(wavelengths, f"{state.state_id} wavelengths_m")
    _strictly_increasing(temperatures, f"{state.state_id} temperatures_K")
    expected = len(wavelengths) * len(temperatures)
    value_fields = (
        (model.n_flat, model.k_flat)
        if model.model_kind == "nk_table"
        else (model.epsilon_real_flat, model.epsilon_imag_flat)
    )
    if any(len(values) != expected for values in value_fields):
        raise ValueError(f"{state.state_id} table arrays do not match their declared grid")
    interpolation = model.interpolation
    if any(
        not state.validity.wavelength_min_m <= value <= state.validity.wavelength_max_m
        for value in interpolation.wavelength_discontinuities_m
    ):
        raise ValueError("wavelength discontinuities must lie inside the state validity domain")
    if any(
        not state.validity.temperature_min_K <= value <= state.validity.temperature_max_K
        for value in interpolation.temperature_discontinuities_K
    ):
        raise ValueError("temperature discontinuities must lie inside the state validity domain")


def _symbolic_preview(state: Any, target: Any) -> dict[str, Any]:
    model = state.optical_model
    prefix = f"{target.function_tag_prefix}_{state.state_id}"
    if model.model_kind == "nk_table":
        expression = f"({prefix}_n(lambda0,T)-i*{prefix}_k(lambda0,T))^2"
        functions = [f"{prefix}_n", f"{prefix}_k"]
        table_sha256 = model.table_sha256
        interpolation = model.interpolation.model_dump(mode="python")
    elif model.model_kind == "permittivity_table":
        expression = f"{prefix}_eps_re(lambda0,T)-i*{prefix}_eps_loss(lambda0,T)"
        functions = [f"{prefix}_eps_re", f"{prefix}_eps_loss"]
        table_sha256 = model.table_sha256
        interpolation = model.interpolation.model_dump(mode="python")
    else:
        expression = f"{prefix}_eps_re(lambda0,T)-i*{prefix}_eps_loss(lambda0,T)"
        functions = [f"{prefix}_eps_re", f"{prefix}_eps_loss"]
        table_sha256 = None
        interpolation = {
            "analytic_model": model.model_kind,
            "extrapolation": model.extrapolation.model_dump(mode="python"),
        }
    return {
        "state_id": state.state_id,
        "model_kind": model.model_kind,
        "function_type": "interpolation" if table_sha256 is not None else "analytic",
        "function_tags": functions,
        "argument_names": ["lambda0", "T"],
        "argument_units": ["m", "K"],
        "function_unit": "1",
        "internal_to_comsol_conversion": {
            "internal": "exp(-i*omega*t), passive Im(epsilon)>=0",
            "comsol_target": "exp(+i*omega*t), epsilon_comsol=epsilon_real-i*epsilon_loss",
        },
        "relative_permittivity_expression": expression,
        "interpolation_and_extrapolation": interpolation,
        "discontinuity_or_phase_boundaries": {
            "wavelength_m": (
                []
                if table_sha256 is None
                else list(model.interpolation.wavelength_discontinuities_m)
            ),
            "temperature_K": (
                []
                if table_sha256 is None
                else list(model.interpolation.temperature_discontinuities_K)
            ),
        },
        "table_sha256": table_sha256,
        "target_property": {
            "component_tag": target.component_tag,
            "material_tag": target.material_tag,
            "property_group_tag": target.property_group_tag,
            "property_key": target.relative_permittivity_property_key,
        },
        "post_apply_readback": {
            "expected_expression": expression,
            "expected_function_tags": functions,
            "exact_match_required": True,
        },
        "rollback": {
            "snapshot_property_before_apply": True,
            "remove_created_functions_on_failure": True,
            "solve_required": False,
        },
    }


def normalize_thermal_material_ledger(
    value: ThermalMaterialLedger | dict[str, Any],
) -> dict[str, Any]:
    """Validate and fingerprint one typed material ledger without COMSOL."""
    raw = (
        value.model_dump(mode="python") if isinstance(value, ThermalMaterialLedger) else dict(value)
    )
    raw.pop("comsol_conversion_preview", None)
    raw.pop("application_contract", None)
    supplied_hash = raw.pop("ledger_sha256", None)
    ledger = ThermalMaterialLedger.model_validate(raw)
    state_ids = [state.state_id for state in ledger.states]
    if len(state_ids) != len(set(state_ids)):
        raise ValueError("material state_id values must be unique")
    for state in ledger.states:
        _validate_state_model(state)
    by_state = {state.state_id: state for state in ledger.states}
    for boundary in ledger.phase_boundaries:
        if boundary.lower_state_id not in by_state or boundary.upper_state_id not in by_state:
            raise ValueError("phase boundaries must reference declared states")
        if boundary.lower_state_id == boundary.upper_state_id:
            raise ValueError("phase boundary states must be distinct")
        lower = by_state[boundary.lower_state_id]
        upper = by_state[boundary.upper_state_id]
        if lower.phase_id == upper.phase_id:
            raise ValueError("phase boundaries must connect distinct phase_id values")
        if not (
            lower.validity.temperature_min_K
            <= boundary.boundary_temperature_K
            <= lower.validity.temperature_max_K
            and upper.validity.temperature_min_K
            <= boundary.boundary_temperature_K
            <= upper.validity.temperature_max_K
        ):
            raise ValueError("phase boundary temperature must be covered by both adjacent states")
    body = ledger.model_dump(mode="python", exclude={"ledger_sha256"})
    body["states"] = sorted(body["states"], key=lambda item: item["state_id"])
    body["phase_boundaries"] = sorted(
        body["phase_boundaries"],
        key=lambda item: (
            item["boundary_temperature_K"],
            item["lower_state_id"],
            item["upper_state_id"],
        ),
    )
    canonical_json_v1(body)
    calculated = domain_sha256_v2("thermal_material_ledger/1.0.0", body)
    if supplied_hash is not None and supplied_hash != calculated:
        raise ValueError("ledger_sha256 does not match the normalized ledger")
    preview = [
        _symbolic_preview(state, ledger.comsol_target)
        for state in sorted(ledger.states, key=lambda item: item.state_id)
    ]
    return deepcopy(
        {
            **body,
            "ledger_sha256": calculated,
            "comsol_conversion_preview": preview,
            "application_contract": {
                "derived_model_required": True,
                "exact_readback_required": True,
                "rollback_required": True,
                "mutation_performed": False,
                "solver_started": False,
            },
        }
    )


def _outside_fraction(value: float, minimum: float, maximum: float) -> float:
    span = maximum - minimum
    if span == 0.0:
        return 0.0 if value == minimum else math.inf
    if value < minimum:
        return (minimum - value) / span
    if value > maximum:
        return (value - maximum) / span
    return 0.0


def _bracket(values: list[float], target: float) -> tuple[int, int, float]:
    if len(values) == 1:
        return 0, 0, 0.0
    if target <= values[0]:
        lower, upper = 0, 1
    elif target >= values[-1]:
        lower, upper = len(values) - 2, len(values) - 1
    else:
        upper = next(index for index, value in enumerate(values) if value >= target)
        lower = upper - 1
    fraction = (target - values[lower]) / (values[upper] - values[lower])
    return lower, upper, fraction


def _crosses_boundary(lower: float, upper: float, boundaries: list[float]) -> bool:
    return any(lower < boundary < upper for boundary in boundaries)


def _interpolate_pair(left: float, right: float, fraction: float, method: str) -> float:
    if method == "nearest":
        return left if fraction < 0.5 else right
    if method == "piecewise_constant":
        return left
    return left + fraction * (right - left)


def _table_value(
    flat: list[float],
    wavelengths: list[float],
    temperatures: list[float],
    wavelength: float,
    temperature: float,
    policy: Any,
) -> float:
    w0, w1, wf = _bracket(wavelengths, wavelength)
    t0, t1, tf = _bracket(temperatures, temperature)
    if _crosses_boundary(
        min(wavelength, wavelengths[w0]),
        max(wavelength, wavelengths[w1]),
        list(policy.wavelength_discontinuities_m),
    ) or _crosses_boundary(
        min(temperature, temperatures[t0]),
        max(temperature, temperatures[t1]),
        list(policy.temperature_discontinuities_K),
    ):
        raise DeclaredDiscontinuityError(
            "interpolation would cross a declared discontinuity boundary"
        )
    width = len(wavelengths)

    def at_temperature(index: int) -> float:
        return _interpolate_pair(
            flat[index * width + w0],
            flat[index * width + w1],
            wf,
            policy.wavelength_method,
        )

    return _interpolate_pair(
        at_temperature(t0),
        at_temperature(t1),
        tf,
        policy.temperature_method,
    )


def _nk_to_epsilon(refractive_index: float, extinction: float) -> complex:
    return complex(refractive_index, extinction) ** 2


def _epsilon_to_nk(epsilon: complex) -> tuple[float, float]:
    magnitude = abs(epsilon)
    refractive_index = math.sqrt(max(0.0, (magnitude + epsilon.real) / 2.0))
    if refractive_index == 0.0:
        extinction = math.sqrt(max(0.0, (magnitude - epsilon.real) / 2.0))
    else:
        extinction = epsilon.imag / (2.0 * refractive_index)
    return refractive_index, extinction


def _evaluate_model(state: Any, wavelength_m: float, temperature_K: float) -> tuple[complex, str]:
    model = state.optical_model
    omega = 2.0 * math.pi * _C / wavelength_m
    if model.model_kind == "nk_table":
        n_value = _table_value(
            list(model.n_flat),
            list(model.wavelengths_m),
            list(model.temperatures_K),
            wavelength_m,
            temperature_K,
            model.interpolation,
        )
        k_value = _table_value(
            list(model.k_flat),
            list(model.wavelengths_m),
            list(model.temperatures_K),
            wavelength_m,
            temperature_K,
            model.interpolation,
        )
        return _nk_to_epsilon(n_value, k_value), "nk_table"
    if model.model_kind == "permittivity_table":
        real = _table_value(
            list(model.epsilon_real_flat),
            list(model.wavelengths_m),
            list(model.temperatures_K),
            wavelength_m,
            temperature_K,
            model.interpolation,
        )
        imaginary = _table_value(
            list(model.epsilon_imag_flat),
            list(model.wavelengths_m),
            list(model.temperatures_K),
            wavelength_m,
            temperature_K,
            model.interpolation,
        )
        return complex(real, imaginary), "permittivity_table"
    if model.model_kind == "drude":
        denominator = omega * complex(omega, model.damping_angular_frequency_rad_s)
        return (
            model.epsilon_infinity - model.plasma_angular_frequency_rad_s**2 / denominator,
            "drude",
        )
    if model.model_kind == "lorentz":
        epsilon = complex(model.epsilon_infinity, 0.0)
        for oscillator in model.oscillators:
            denominator = complex(
                oscillator.resonance_angular_frequency_rad_s**2 - omega**2,
                -oscillator.damping_angular_frequency_rad_s * omega,
            )
            if denominator == 0:
                raise ValueError("Lorentz model is singular at exact zero-damping resonance")
            epsilon += (
                oscillator.oscillator_strength
                * oscillator.resonance_angular_frequency_rad_s**2
                / denominator
            )
        return epsilon, "lorentz"
    if model.model_kind == "tolo":
        epsilon = complex(model.epsilon_infinity, 0.0)
        for mode in model.modes:
            numerator = complex(
                mode.longitudinal_angular_frequency_rad_s**2 - omega**2,
                -mode.longitudinal_damping_rad_s * omega,
            )
            denominator = complex(
                mode.transverse_angular_frequency_rad_s**2 - omega**2,
                -mode.transverse_damping_rad_s * omega,
            )
            if denominator == 0:
                raise ValueError("TOLO model is singular at exact zero-damping resonance")
            epsilon *= numerator / denominator
        return epsilon, "tolo"
    delta_temperature = temperature_K - model.reference_temperature_K
    refractive_index = model.refractive_index_at_reference + model.dn_dT_per_K * delta_temperature
    extinction = model.extinction_coefficient_at_reference + model.dk_dT_per_K * delta_temperature
    if refractive_index < 0.0 or extinction < 0.0:
        raise ValueError("thermo-optic evaluation produced negative n or k")
    return _nk_to_epsilon(refractive_index, extinction), "thermo_optic"


def _uncertainty(
    state: Any, epsilon: complex, n_value: float, k_value: float, factor: float
) -> dict[str, float]:
    uncertainty = state.uncertainty
    if uncertainty.kind == "nk_absolute":
        real_sigma = math.hypot(
            2.0 * n_value * uncertainty.n_abs, 2.0 * k_value * uncertainty.k_abs
        )
        imag_sigma = math.hypot(
            2.0 * k_value * uncertainty.n_abs, 2.0 * n_value * uncertainty.k_abs
        )
    elif uncertainty.kind == "epsilon_absolute":
        real_sigma = uncertainty.epsilon_real_abs
        imag_sigma = uncertainty.epsilon_imag_abs
    else:
        real_sigma = abs(epsilon.real) * uncertainty.relative_fraction
        imag_sigma = abs(epsilon.imag) * uncertainty.relative_fraction
    return {
        "epsilon_real_abs": real_sigma * factor,
        "epsilon_imag_abs": imag_sigma * factor,
        "expansion_factor": factor,
    }


def evaluate_thermal_material(
    value: ThermalMaterialEvaluationRequest | dict[str, Any],
) -> dict[str, Any]:
    """Evaluate one exact material state and return a COMSOL conversion preview."""
    request = ThermalMaterialEvaluationRequest.model_validate(value)
    normalized = normalize_thermal_material_ledger(request.ledger)
    ledger = request.ledger
    by_state = {state.state_id: state for state in ledger.states}
    state = by_state.get(request.state_id)
    if state is None:
        raise ValueError("state_id is not declared by the material ledger")
    validity = state.validity
    wavelength_fraction = _outside_fraction(
        request.wavelength_m, validity.wavelength_min_m, validity.wavelength_max_m
    )
    temperature_fraction = _outside_fraction(
        request.temperature_K, validity.temperature_min_K, validity.temperature_max_K
    )
    outside_fraction = max(wavelength_fraction, temperature_fraction)
    policy = _model_policy(state.optical_model)
    base = {
        "schema_name": "comsol_mcp.thermal_material_evaluation",
        "schema_version": "1.0.0",
        "ledger_sha256": normalized["ledger_sha256"],
        "state_id": state.state_id,
        "state_identity_sha256": domain_sha256_v2(
            "thermal_material_state/1.0.0", state.model_dump(mode="python")
        ),
        "wavelength_m": request.wavelength_m,
        "temperature_K": request.temperature_K,
        "internal_phasor_convention": ledger.internal_phasor_convention,
        "phase_id": state.phase_id,
        "fabrication_state": state.fabrication_state,
        "carrier_state": (
            None if state.carrier_state is None else state.carrier_state.model_dump(mode="python")
        ),
        "state_variables": [item.model_dump(mode="python") for item in state.state_variables],
        "phase_fraction": state.phase_fraction,
        "source": state.source.model_dump(mode="python"),
    }
    if outside_fraction > 0.0 and (
        policy.mode == "none" or outside_fraction > policy.maximum_fraction_outside_domain
    ):
        body = {
            **base,
            "available": False,
            "reason_code": "outside_declared_validity_domain",
            "extrapolated": False,
            "solver_started": False,
            "filesystem_modified": False,
        }
        return {
            **body,
            "evaluation_sha256": domain_sha256_v2("thermal_material_evaluation/1.0.0", body),
        }
    try:
        epsilon, model_kind = _evaluate_model(state, request.wavelength_m, request.temperature_K)
    except DeclaredDiscontinuityError:
        body = {
            **base,
            "available": False,
            "reason_code": "declared_discontinuity_requires_explicit_state",
            "extrapolated": False,
            "solver_started": False,
            "filesystem_modified": False,
        }
        return {
            **body,
            "evaluation_sha256": domain_sha256_v2("thermal_material_evaluation/1.0.0", body),
        }
    if not math.isfinite(epsilon.real) or not math.isfinite(epsilon.imag):
        raise ValueError("material evaluation produced non-finite permittivity")
    if epsilon.imag < -1.0e-12:
        raise ValueError("material evaluation violates the declared passive loss convention")
    epsilon = complex(epsilon.real, max(0.0, epsilon.imag))
    refractive_index, extinction = _epsilon_to_nk(epsilon)
    expansion_factor = 1.0 + policy.uncertainty_growth_per_fraction * outside_fraction
    target = ledger.comsol_target
    comsol_expression = f"({_format(epsilon.real)}-i*{_format(epsilon.imag)})"
    body = {
        **base,
        "available": True,
        "reason_code": "evaluated",
        "model_kind": model_kind,
        "epsilon_internal": {"real": epsilon.real, "imag": epsilon.imag},
        "refractive_index_internal": {"n": refractive_index, "k": extinction},
        "passive": epsilon.imag >= 0.0 and extinction >= 0.0,
        "extrapolated": outside_fraction > 0.0,
        "extrapolation_fraction": outside_fraction,
        "extrapolation_policy_sha256": (
            None
            if policy.mode == "none"
            else domain_sha256_v2(
                "thermal_material_extrapolation/1.0.0", policy.model_dump(mode="python")
            )
        ),
        "uncertainty": _uncertainty(state, epsilon, refractive_index, extinction, expansion_factor),
        "comsol_conversion_preview": {
            "harmonic_convention": "exp(+i*omega*t)",
            "property_key": target.relative_permittivity_property_key,
            "property_expression": comsol_expression,
            "expected_readback_expression": comsol_expression,
            "exact_readback_required": True,
            "rollback_required": True,
            "mutation_performed": False,
        },
        "solver_started": False,
        "filesystem_modified": False,
    }
    canonical_json_v1(body)
    return {
        **body,
        "evaluation_sha256": domain_sha256_v2("thermal_material_evaluation/1.0.0", body),
    }


def _format(value: float) -> str:
    return format(value, ".16g")


__all__ = ["evaluate_thermal_material", "normalize_thermal_material_ledger"]
