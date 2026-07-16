"""material expressions gates for solver-free dispersive material-expression previews."""

from __future__ import annotations

import math
from pathlib import Path
import subprocess
import sys

import pytest
from mcp.server.fastmcp import FastMCP

from src.evidence.material_expressions import preview_material_expression
from src.tools.material_expressions import register_material_expression_tools


COMMON = {
    "test_wavelengths": [4.0, 5.0],
    "wavelength_unit": "um",
    "harmonic_convention": "exp(+i*omega*t)",
    "formulation": "volumetric_material",
}


def _drude(sign: str, **overrides):
    arguments = {
        **COMMON,
        "model_kind": "drude",
        "parameters": {
            "epsilon_inf": 1.0,
            "plasma_angular_frequency": 1.37e16,
            "damping_angular_frequency": 4.08e13,
        },
        "parameter_units": {
            "epsilon_inf": "1",
            "plasma_angular_frequency": "rad/s",
            "damping_angular_frequency": "rad/s",
        },
        "imaginary_sign": sign,
    }
    arguments.update(overrides)
    return preview_material_expression(**arguments)


def test_documented_drude_signs_are_distinct_and_link_to_c_const_over_wl():
    positive = _drude("positive")
    negative = _drude("negative")

    assert "2*pi*c_const/wl" in positive["expression"]
    assert "+i*" in positive["expression"]
    assert "-i*" in negative["expression"]
    assert positive["preview"][0]["epsilon"]["imag"] > 0
    assert negative["preview"][0]["epsilon"]["imag"] < 0
    assert positive["configuration_sha256"] != negative["configuration_sha256"]
    assert positive["assessment"]["physical_passivity"] == "unknown"
    assert positive["assessment"]["kind"] == "sign_diagnostic"


def test_parameter_names_are_used_verbatim_and_values_remain_in_ledger():
    result = _drude(
        "negative",
        parameter_names={
            "epsilon_inf": "eps_inf",
            "plasma_angular_frequency": "wp",
            "damping_angular_frequency": "gamma",
        },
    )

    assert result["expression"].startswith("eps_inf-(wp)^2/")
    assert "gamma" in result["expression"]
    assert result["convention_ledger"]["parameter_values"]["plasma_angular_frequency"] == 1.37e16


def test_constant_and_nk_forms_preserve_declared_imaginary_sign():
    constant = preview_material_expression(
        **COMMON,
        model_kind="constant",
        parameters={"epsilon_real": 3.0, "epsilon_imag": 0.25},
        imaginary_sign="negative",
    )
    nk = preview_material_expression(
        **COMMON,
        model_kind="n_k",
        parameters={"refractive_index": 4.17, "extinction_coefficient": 0.001},
        imaginary_sign="positive",
    )

    assert constant["expression"] == "(3-i*0.25)"
    assert constant["preview"][0]["epsilon"] == {"real": 3.0, "imag": -0.25}
    expected = complex(4.17, 0.001) ** 2
    assert nk["expression"] == "(4.17+i*0.001)^2"
    assert nk["preview"][0]["epsilon"]["real"] == pytest.approx(expected.real)
    assert nk["preview"][0]["epsilon"]["imag"] == pytest.approx(expected.imag)


def test_lorentz_preview_supports_both_conventions_without_silent_sign_flip():
    base = {
        **COMMON,
        "model_kind": "lorentz",
        "parameters": {
            "epsilon_inf": 2.0,
            "oscillator_strength": 0.5,
            "resonance_angular_frequency": 4.0e14,
            "damping_angular_frequency": 1.0e13,
        },
        "parameter_units": {
            "resonance_angular_frequency": "1/s",
            "damping_angular_frequency": "1/s",
        },
        "imaginary_sign": "positive",
    }
    first = preview_material_expression(**base)
    second = preview_material_expression(
        **{**base, "harmonic_convention": "exp(-i*omega*t)"}
    )

    assert first["expression"] == second["expression"]
    assert first["configuration_sha256"] != second["configuration_sha256"]
    assert first["convention_ledger"]["harmonic_convention"] != second["convention_ledger"]["harmonic_convention"]
    assert first["preview"][0]["epsilon"]["imag"] > 0


def test_missing_units_zero_damping_and_frozen_frequency_are_explicit_warnings():
    result = preview_material_expression(
        **COMMON,
        model_kind="drude",
        parameters={
            "epsilon_inf": 1.0,
            "plasma_angular_frequency": 1.0e15,
            "damping_angular_frequency": 0.0,
        },
        imaginary_sign="positive",
        frequency_source="fixed_angular_frequency",
        fixed_angular_frequency=3.0e14,
    )
    codes = {warning["code"] for warning in result["warnings"]}

    assert {
        "missing_parameter_unit",
        "zero_loss_parameter",
        "frozen_frequency",
        "missing_fixed_frequency_unit",
        "sign_is_diagnostic_only",
    } <= codes
    assert all(item["angular_frequency_rad_s"] == 3.0e14 for item in result["preview"])
    assert all(item["sign_diagnostic"] == "indeterminate_zero_imaginary" for item in result["preview"])


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"parameters": {"epsilon_real": 1.0}}, "must exactly contain"),
        ({"test_wavelengths": [float("nan")]}, "finite"),
        ({"test_wavelengths": [0.0]}, "positive"),
        ({"imaginary_sign": "automatic"}, "positive or negative"),
        ({"harmonic_convention": "COMSOL default"}, "explicitly"),
        ({"parameter_names": {"epsilon_real": "bad name"}}, "identifier"),
    ],
)
def test_malformed_ambiguous_and_nonfinite_inputs_fail_closed(changes, match):
    arguments = {
        **COMMON,
        "model_kind": "constant",
        "parameters": {"epsilon_real": 2.0, "epsilon_imag": 0.1},
        "imaginary_sign": "positive",
        **changes,
    }
    with pytest.raises(ValueError, match=match):
        preview_material_expression(**arguments)


def test_preview_is_deterministic_and_import_does_not_load_solver_stack():
    first = _drude("negative")
    second = _drude("negative")

    assert first == second
    code = """
import sys
from src.evidence.material_expressions import preview_material_expression
assert 'mph' not in sys.modules
assert not any(name.startswith('jpype') for name in sys.modules)
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_public_tool_returns_bounded_validation_error_without_starting_solver():
    server = FastMCP("material-expression-test")
    register_material_expression_tools(server)
    tool = server._tool_manager._tools["wave_optics_material_expression_preview"]

    result = tool.fn(
        model_kind="constant",
        parameters={"epsilon_real": math.inf, "epsilon_imag": 0.0},
        test_wavelengths=[1.0],
        wavelength_unit="um",
        harmonic_convention="exp(+i*omega*t)",
        imaginary_sign="positive",
        formulation="volumetric_material",
    )

    assert result["success"] is False
    assert "finite" in result["error"]
