"""Solver-free dispersive material-expression construction and preview."""

from __future__ import annotations

from copy import deepcopy
import math
import re
from typing import Any

from src.evidence.contracts import canonical_sha256


EXPRESSION_PREVIEW_SCHEMA_VERSION = "1.0.0"
MAX_PREVIEW_WAVELENGTHS = 64
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_EXPRESSION_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.]*$")
_C = 299_792_458.0

_PARAMETERS = {
    "constant": ("epsilon_real", "epsilon_imag"),
    "drude": ("epsilon_inf", "plasma_angular_frequency", "damping_angular_frequency"),
    "lorentz": (
        "epsilon_inf",
        "oscillator_strength",
        "resonance_angular_frequency",
        "damping_angular_frequency",
    ),
    "n_k": ("refractive_index", "extinction_coefficient"),
}
_ANGULAR_PARAMETERS = {
    "plasma_angular_frequency",
    "damping_angular_frequency",
    "resonance_angular_frequency",
}
_WAVELENGTH_FACTORS = {
    "m": 1.0,
    "um": 1e-6,
    "µm": 1e-6,
    "nm": 1e-9,
}
_FREQUENCY_FACTORS = {
    "hz": 1.0,
    "khz": 1e3,
    "mhz": 1e6,
    "ghz": 1e9,
    "thz": 1e12,
}
_ANGULAR_UNITS = {"rad/s", "1/s", "s^-1"}


def _number(value: Any, label: str, *, nonnegative: bool = False, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    if positive and result <= 0:
        raise ValueError(f"{label} must be positive")
    if nonnegative and result < 0:
        raise ValueError(f"{label} must be non-negative")
    return result


def _format_number(value: float) -> str:
    return format(value, ".16g")


def _validate_name(value: str, label: str, *, dotted: bool = False) -> str:
    pattern = _EXPRESSION_NAME if dotted else _NAME
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{label} must be one exact COMSOL identifier")
    return value


def _angular_value(value: float, unit: str | None, label: str) -> float:
    normalized = unit.strip().lower() if isinstance(unit, str) and unit.strip() else None
    if normalized is None or normalized in _ANGULAR_UNITS:
        return value
    if normalized in _FREQUENCY_FACTORS:
        return 2.0 * math.pi * value * _FREQUENCY_FACTORS[normalized]
    raise ValueError(f"{label} unit must be rad/s, 1/s, s^-1, or a supported Hz unit")


def _angular_expression(value: float, unit: str | None, name: str | None) -> str:
    normalized = unit.strip().lower() if isinstance(unit, str) and unit.strip() else None
    base = name if name is not None else _format_number(value)
    if normalized in _FREQUENCY_FACTORS:
        if name is not None:
            factor = _FREQUENCY_FACTORS[normalized]
            return f"(2*pi*{_format_number(factor)}*{base})"
        return f"(2*pi*{base}[{normalized}])"
    if name is not None:
        return base
    return f"({base}[1/s])"


def _dimensionless_expression(value: float, name: str | None) -> str:
    return name if name is not None else _format_number(value)


def _signed_imaginary(real: str, imaginary: str, sign: str) -> str:
    operator = "+" if sign == "positive" else "-"
    return f"({real}{operator}i*{imaginary})"


def _complex_preview(
    model_kind: str,
    values: dict[str, float],
    omega: float,
    imaginary_sign: str,
) -> complex:
    direction = 1.0 if imaginary_sign == "positive" else -1.0
    if model_kind == "constant":
        return complex(values["epsilon_real"], direction * values["epsilon_imag"])
    if model_kind == "n_k":
        return complex(
            values["refractive_index"],
            direction * values["extinction_coefficient"],
        ) ** 2
    if model_kind == "drude":
        eps_inf = values["epsilon_inf"]
        wp = values["plasma_angular_frequency"]
        gamma = values["damping_angular_frequency"]
        denominator = omega * complex(omega, direction * gamma)
        return eps_inf - wp ** 2 / denominator
    eps_inf = values["epsilon_inf"]
    strength = values["oscillator_strength"]
    omega0 = values["resonance_angular_frequency"]
    gamma = values["damping_angular_frequency"]
    denominator = complex(omega0 ** 2 - omega ** 2, -direction * gamma * omega)
    if denominator == 0:
        raise ValueError("Lorentz preview is singular at zero damping and exact resonance")
    return eps_inf + strength * omega0 ** 2 / denominator


def preview_material_expression(
    *,
    model_kind: str,
    parameters: dict[str, float],
    test_wavelengths: list[float],
    wavelength_unit: str,
    harmonic_convention: str,
    imaginary_sign: str,
    formulation: str,
    wavelength_parameter: str = "wl",
    parameter_names: dict[str, str] | None = None,
    parameter_units: dict[str, str] | None = None,
    frequency_source: str = "wavelength_parameter",
    physics_frequency_expression: str = "ewfd.freq",
    fixed_angular_frequency: float | None = None,
    fixed_angular_frequency_unit: str | None = None,
) -> dict[str, Any]:
    """Construct one exact expression and preview it without importing COMSOL."""
    if model_kind not in _PARAMETERS:
        raise ValueError(f"model_kind must be one of {sorted(_PARAMETERS)}")
    if harmonic_convention not in {"exp(+i*omega*t)", "exp(-i*omega*t)"}:
        raise ValueError("harmonic_convention must explicitly be exp(+i*omega*t) or exp(-i*omega*t)")
    if imaginary_sign not in {"positive", "negative"}:
        raise ValueError("imaginary_sign must be positive or negative")
    if formulation not in {"volumetric_material", "layered_boundary_documented"}:
        raise ValueError("formulation must be volumetric_material or layered_boundary_documented")
    if frequency_source not in {"wavelength_parameter", "physics_frequency", "fixed_angular_frequency"}:
        raise ValueError("frequency_source is unsupported")
    wavelength_parameter = _validate_name(wavelength_parameter, "wavelength_parameter")
    physics_frequency_expression = _validate_name(
        physics_frequency_expression, "physics_frequency_expression", dotted=True
    )

    required = _PARAMETERS[model_kind]
    if not isinstance(parameters, dict) or set(parameters) != set(required):
        raise ValueError(f"parameters for {model_kind} must exactly contain {list(required)}")
    names = {} if parameter_names is None else deepcopy(parameter_names)
    units = {} if parameter_units is None else deepcopy(parameter_units)
    if not isinstance(names, dict) or set(names) - set(required):
        raise ValueError("parameter_names contains unknown parameter keys")
    if not isinstance(units, dict) or set(units) - set(required):
        raise ValueError("parameter_units contains unknown parameter keys")
    for key, name in names.items():
        names[key] = _validate_name(name, f"parameter_names.{key}")
    for key, unit in units.items():
        if not isinstance(unit, str) or not unit.strip() or len(unit) > 32:
            raise ValueError(f"parameter_units.{key} must be a bounded non-empty string")

    values = {key: _number(parameters[key], f"parameters.{key}") for key in required}
    if model_kind in {"constant", "n_k"}:
        loss_key = "epsilon_imag" if model_kind == "constant" else "extinction_coefficient"
        values[loss_key] = _number(parameters[loss_key], f"parameters.{loss_key}", nonnegative=True)
    if model_kind == "n_k":
        values["refractive_index"] = _number(parameters["refractive_index"], "parameters.refractive_index", nonnegative=True)
    if model_kind == "drude":
        values["plasma_angular_frequency"] = _number(parameters["plasma_angular_frequency"], "parameters.plasma_angular_frequency", positive=True)
        values["damping_angular_frequency"] = _number(parameters["damping_angular_frequency"], "parameters.damping_angular_frequency", nonnegative=True)
    if model_kind == "lorentz":
        values["resonance_angular_frequency"] = _number(parameters["resonance_angular_frequency"], "parameters.resonance_angular_frequency", positive=True)
        values["damping_angular_frequency"] = _number(parameters["damping_angular_frequency"], "parameters.damping_angular_frequency", nonnegative=True)

    converted_values = dict(values)
    warnings: list[dict[str, Any]] = []
    for key in _ANGULAR_PARAMETERS & set(required):
        if key not in units:
            warnings.append({"code": "missing_parameter_unit", "parameter": key, "assumed_unit": "1/s"})
        converted_values[key] = _angular_value(values[key], units.get(key), f"parameter_units.{key}")
    for key in set(units) - _ANGULAR_PARAMETERS:
        if units[key].strip().lower() not in {"1", "dimensionless"}:
            warnings.append({"code": "dimensionless_parameter_unit_ignored", "parameter": key, "unit": units[key]})

    normalized_wavelength_unit = wavelength_unit.strip().lower() if isinstance(wavelength_unit, str) else ""
    if normalized_wavelength_unit not in _WAVELENGTH_FACTORS:
        raise ValueError("wavelength_unit must be m, um, µm, or nm")
    if not isinstance(test_wavelengths, list) or not test_wavelengths or len(test_wavelengths) > MAX_PREVIEW_WAVELENGTHS:
        raise ValueError(f"test_wavelengths must contain 1..{MAX_PREVIEW_WAVELENGTHS} values")
    wavelengths_m = [
        _number(value, f"test_wavelengths[{index}]", positive=True) * _WAVELENGTH_FACTORS[normalized_wavelength_unit]
        for index, value in enumerate(test_wavelengths)
    ]

    fixed_omega_si = None
    if frequency_source == "fixed_angular_frequency":
        if fixed_angular_frequency is None:
            raise ValueError("fixed_angular_frequency is required for fixed_angular_frequency source")
        fixed_value = _number(fixed_angular_frequency, "fixed_angular_frequency", positive=True)
        fixed_omega_si = _angular_value(
            fixed_value,
            fixed_angular_frequency_unit,
            "fixed_angular_frequency_unit",
        )
        omega_expression = _angular_expression(
            fixed_value, fixed_angular_frequency_unit, None
        )
        warnings.append({"code": "frozen_frequency", "message": "The material expression does not vary with wavelength."})
        if fixed_angular_frequency_unit is None:
            warnings.append({"code": "missing_fixed_frequency_unit", "assumed_unit": "1/s"})
    elif frequency_source == "physics_frequency":
        omega_expression = f"(2*pi*{physics_frequency_expression})"
        warnings.append({
            "code": "physics_frequency_linkage",
            "message": "Preview assumes the physics frequency is synchronized to each test wavelength.",
        })
    else:
        omega_expression = f"(2*pi*c_const/{wavelength_parameter})"

    parameter_expressions: dict[str, str] = {}
    for key in required:
        if key in _ANGULAR_PARAMETERS:
            parameter_expressions[key] = _angular_expression(values[key], units.get(key), names.get(key))
        else:
            parameter_expressions[key] = _dimensionless_expression(values[key], names.get(key))

    if model_kind == "constant":
        expression = _signed_imaginary(
            parameter_expressions["epsilon_real"],
            parameter_expressions["epsilon_imag"],
            imaginary_sign,
        )
    elif model_kind == "n_k":
        expression = f"{_signed_imaginary(parameter_expressions['refractive_index'], parameter_expressions['extinction_coefficient'], imaginary_sign)}^2"
    elif model_kind == "drude":
        damping_operator = "+" if imaginary_sign == "positive" else "-"
        expression = (
            f"{parameter_expressions['epsilon_inf']}-"
            f"({parameter_expressions['plasma_angular_frequency']})^2/"
            f"({omega_expression}*({omega_expression}{damping_operator}i*{parameter_expressions['damping_angular_frequency']}))"
        )
    else:
        damping_operator = "-" if imaginary_sign == "positive" else "+"
        expression = (
            f"{parameter_expressions['epsilon_inf']}+"
            f"{parameter_expressions['oscillator_strength']}*({parameter_expressions['resonance_angular_frequency']})^2/"
            f"(({parameter_expressions['resonance_angular_frequency']})^2-({omega_expression})^2"
            f"{damping_operator}i*{parameter_expressions['damping_angular_frequency']}*{omega_expression})"
        )

    zero_loss = (
        (model_kind == "constant" and converted_values["epsilon_imag"] == 0)
        or (model_kind == "n_k" and converted_values["extinction_coefficient"] == 0)
        or (model_kind in {"drude", "lorentz"} and converted_values["damping_angular_frequency"] == 0)
    )
    if zero_loss:
        warnings.append({"code": "zero_loss_parameter", "message": "The sign diagnostic may be indeterminate because the declared loss parameter is zero."})
    warnings.append({
        "code": "sign_is_diagnostic_only",
        "message": "The declared complex sign is not proof of physical passivity; use solved Qh and power closure.",
    })

    preview = []
    for requested, wavelength_m in zip(test_wavelengths, wavelengths_m):
        omega = fixed_omega_si if fixed_omega_si is not None else 2.0 * math.pi * _C / wavelength_m
        epsilon = _complex_preview(model_kind, converted_values, omega, imaginary_sign)
        if not math.isfinite(epsilon.real) or not math.isfinite(epsilon.imag):
            raise ValueError("expression preview produced non-finite permittivity")
        expected_direction = 1 if imaginary_sign == "positive" else -1
        diagnostic = (
            "indeterminate_zero_imaginary"
            if epsilon.imag == 0
            else ("matches_declared_sign" if math.copysign(1.0, epsilon.imag) == expected_direction else "opposes_declared_sign")
        )
        preview.append({
            "requested_wavelength": float(requested),
            "wavelength_unit": wavelength_unit,
            "wavelength_m": wavelength_m,
            "angular_frequency_rad_s": omega,
            "epsilon": {"real": float(epsilon.real), "imag": float(epsilon.imag)},
            "sign_diagnostic": diagnostic,
        })

    ledger = {
        "harmonic_convention": harmonic_convention,
        "declared_imaginary_sign": imaginary_sign,
        "formulation": formulation,
        "frequency_source": frequency_source,
        "frequency_expression": omega_expression,
        "wavelength_parameter": wavelength_parameter if frequency_source == "wavelength_parameter" else None,
        "parameter_names": names,
        "parameter_values": values,
        "parameter_units": {key: units.get(key, "1/s" if key in _ANGULAR_PARAMETERS else "1") for key in required},
        "physical_claim": "sign_diagnostic_not_physical_passivity",
    }
    configuration = {
        "schema_version": EXPRESSION_PREVIEW_SCHEMA_VERSION,
        "model_kind": model_kind,
        "expression": expression,
        "convention_ledger": ledger,
        "test_wavelengths": [float(value) for value in test_wavelengths],
        "wavelength_unit": wavelength_unit,
    }
    return {
        "success": True,
        **configuration,
        "configuration_sha256": canonical_sha256(configuration),
        "preview": preview,
        "warnings": warnings,
        "assessment": {
            "kind": "sign_diagnostic",
            "physical_passivity": "unknown",
            "requires_solved_evidence": ["per-domain Qh", "passive R/T/A", "declared physical power closure"],
        },
    }


__all__ = [
    "EXPRESSION_PREVIEW_SCHEMA_VERSION",
    "preview_material_expression",
]
