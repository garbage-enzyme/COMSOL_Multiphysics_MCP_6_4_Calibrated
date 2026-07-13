"""MCP adapter for the solver-free material-expression preview."""

from __future__ import annotations

from typing import Any, Literal, Optional

from mcp.server.fastmcp import FastMCP

from src.evidence.material_expressions import preview_material_expression


def register_material_expression_tools(mcp: FastMCP) -> None:
    """Register the solver-free Wave Optics material-expression preview."""

    @mcp.tool()
    def wave_optics_material_expression_preview(
        model_kind: Literal["constant", "drude", "lorentz", "n_k"],
        parameters: dict[str, float],
        test_wavelengths: list[float],
        wavelength_unit: str,
        harmonic_convention: Literal["exp(+i*omega*t)", "exp(-i*omega*t)"],
        imaginary_sign: Literal["positive", "negative"],
        formulation: Literal["volumetric_material", "layered_boundary_documented"],
        wavelength_parameter: str = "wl",
        parameter_names: Optional[dict[str, str]] = None,
        parameter_units: Optional[dict[str, str]] = None,
        frequency_source: Literal[
            "wavelength_parameter", "physics_frequency", "fixed_angular_frequency"
        ] = "wavelength_parameter",
        physics_frequency_expression: str = "ewfd.freq",
        fixed_angular_frequency: Optional[float] = None,
        fixed_angular_frequency_unit: Optional[str] = None,
    ) -> dict[str, Any]:
        """Construct and numerically preview a declared material expression without COMSOL."""
        try:
            return preview_material_expression(
                model_kind=model_kind,
                parameters=parameters,
                test_wavelengths=test_wavelengths,
                wavelength_unit=wavelength_unit,
                harmonic_convention=harmonic_convention,
                imaginary_sign=imaginary_sign,
                formulation=formulation,
                wavelength_parameter=wavelength_parameter,
                parameter_names=parameter_names,
                parameter_units=parameter_units,
                frequency_source=frequency_source,
                physics_frequency_expression=physics_frequency_expression,
                fixed_angular_frequency=fixed_angular_frequency,
                fixed_angular_frequency_unit=fixed_angular_frequency_unit,
            )
        except (TypeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}


__all__ = ["register_material_expression_tools"]
