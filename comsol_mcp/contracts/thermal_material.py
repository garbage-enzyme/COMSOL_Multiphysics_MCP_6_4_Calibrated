"""Typed temperature/state-dependent material ledger contracts."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
BoundedName = Annotated[str, Field(min_length=1, max_length=256)]
ExactIdentifier = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_]*$"),
]
BoundedText = Annotated[str, Field(min_length=1, max_length=2048)]
PositiveFloat = Annotated[float, Field(gt=0.0)]
NonnegativeFloat = Annotated[float, Field(ge=0.0)]


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class SourceRecord(_ClosedModel):
    source_kind: Literal["citation", "file", "database_export"]
    citation: BoundedText | None = None
    file_sha256: Sha256 | None = None
    source_state_description: BoundedText

    @model_validator(mode="after")
    def validate_source_identity(self) -> SourceRecord:
        if self.source_kind == "citation" and self.citation is None:
            raise ValueError("citation sources require citation text")
        if self.source_kind != "citation" and self.file_sha256 is None:
            raise ValueError("file-backed sources require file_sha256")
        return self


class ValidityDomain(_ClosedModel):
    wavelength_min_m: PositiveFloat
    wavelength_max_m: PositiveFloat
    temperature_min_K: PositiveFloat
    temperature_max_K: PositiveFloat

    @model_validator(mode="after")
    def validate_ranges(self) -> ValidityDomain:
        if self.wavelength_max_m <= self.wavelength_min_m:
            raise ValueError("wavelength validity maximum must exceed minimum")
        if self.temperature_max_K < self.temperature_min_K:
            raise ValueError("temperature validity maximum must not be below minimum")
        return self


class MeasurementConditions(_ClosedModel):
    method: BoundedName
    ambient: BoundedName
    pressure_Pa: NonnegativeFloat | None = None
    crystallographic_orientation: BoundedName | None = None


class UncertaintyModel(_ClosedModel):
    kind: Literal["nk_absolute", "epsilon_absolute", "relative"]
    n_abs: NonnegativeFloat = 0.0
    k_abs: NonnegativeFloat = 0.0
    epsilon_real_abs: NonnegativeFloat = 0.0
    epsilon_imag_abs: NonnegativeFloat = 0.0
    relative_fraction: Annotated[float, Field(ge=0.0, le=10.0)] = 0.0

    @model_validator(mode="after")
    def validate_kind_fields(self) -> UncertaintyModel:
        nk = (self.n_abs, self.k_abs)
        epsilon = (self.epsilon_real_abs, self.epsilon_imag_abs)
        if self.kind == "nk_absolute":
            if not any(value > 0.0 for value in nk) or any(value != 0.0 for value in epsilon):
                raise ValueError("nk_absolute uncertainty requires only nonzero n/k bounds")
            if self.relative_fraction != 0.0:
                raise ValueError("absolute uncertainty cannot declare relative_fraction")
        elif self.kind == "epsilon_absolute":
            if not any(value > 0.0 for value in epsilon) or any(value != 0.0 for value in nk):
                raise ValueError("epsilon_absolute uncertainty requires only epsilon bounds")
            if self.relative_fraction != 0.0:
                raise ValueError("absolute uncertainty cannot declare relative_fraction")
        elif self.relative_fraction <= 0.0 or any(value != 0.0 for value in (*nk, *epsilon)):
            raise ValueError("relative uncertainty requires only a positive relative_fraction")
        return self


class ExtrapolationPolicy(_ClosedModel):
    mode: Literal["none", "source_backed_linear"] = "none"
    policy_source_sha256: Sha256 | None = None
    maximum_fraction_outside_domain: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    uncertainty_growth_per_fraction: Annotated[float, Field(ge=0.0, le=100.0)] = 0.0

    @model_validator(mode="after")
    def validate_source_backing(self) -> ExtrapolationPolicy:
        if self.mode == "source_backed_linear":
            if self.policy_source_sha256 is None:
                raise ValueError("source-backed extrapolation requires policy_source_sha256")
            if self.maximum_fraction_outside_domain <= 0.0:
                raise ValueError("source-backed extrapolation requires a positive range")
        elif (
            self.policy_source_sha256 is not None
            or self.maximum_fraction_outside_domain != 0.0
            or self.uncertainty_growth_per_fraction != 0.0
        ):
            raise ValueError("disabled extrapolation cannot declare source-backed fields")
        return self


class InterpolationPolicy(_ClosedModel):
    wavelength_method: Literal["linear", "nearest", "piecewise_constant"]
    temperature_method: Literal["linear", "nearest", "piecewise_constant"]
    wavelength_discontinuities_m: Annotated[list[PositiveFloat], Field(max_length=64)]
    temperature_discontinuities_K: Annotated[list[PositiveFloat], Field(max_length=64)]
    extrapolation: ExtrapolationPolicy = Field(default_factory=ExtrapolationPolicy)


class CarrierState(_ClosedModel):
    density_per_cubic_metre: NonnegativeFloat
    mobility_square_metre_per_V_s: NonnegativeFloat
    effective_mass_electron: PositiveFloat


class StateVariable(_ClosedModel):
    name: ExactIdentifier
    value: float
    unit: BoundedName
    source_sha256: Sha256 | None = None


class NkTableModel(_ClosedModel):
    model_kind: Literal["nk_table"]
    wavelengths_m: Annotated[list[PositiveFloat], Field(min_length=2, max_length=512)]
    temperatures_K: Annotated[list[PositiveFloat], Field(min_length=1, max_length=64)]
    n_flat: Annotated[list[NonnegativeFloat], Field(min_length=2, max_length=2048)]
    k_flat: Annotated[list[NonnegativeFloat], Field(min_length=2, max_length=2048)]
    interpolation: InterpolationPolicy
    table_sha256: Sha256

    @model_validator(mode="after")
    def validate_table_shape(self) -> NkTableModel:
        expected = len(self.wavelengths_m) * len(self.temperatures_K)
        if len(self.n_flat) != expected or len(self.k_flat) != expected:
            raise ValueError("nk table arrays must match the declared grid shape")
        return self


class PermittivityTableModel(_ClosedModel):
    model_kind: Literal["permittivity_table"]
    wavelengths_m: Annotated[list[PositiveFloat], Field(min_length=2, max_length=512)]
    temperatures_K: Annotated[list[PositiveFloat], Field(min_length=1, max_length=64)]
    epsilon_real_flat: Annotated[list[float], Field(min_length=2, max_length=2048)]
    epsilon_imag_flat: Annotated[list[NonnegativeFloat], Field(min_length=2, max_length=2048)]
    interpolation: InterpolationPolicy
    table_sha256: Sha256

    @model_validator(mode="after")
    def validate_table_shape(self) -> PermittivityTableModel:
        expected = len(self.wavelengths_m) * len(self.temperatures_K)
        if len(self.epsilon_real_flat) != expected or len(self.epsilon_imag_flat) != expected:
            raise ValueError("permittivity table arrays must match the declared grid shape")
        return self


class DrudeModel(_ClosedModel):
    model_kind: Literal["drude"]
    epsilon_infinity: float
    plasma_angular_frequency_rad_s: PositiveFloat
    damping_angular_frequency_rad_s: NonnegativeFloat
    extrapolation: ExtrapolationPolicy = Field(default_factory=ExtrapolationPolicy)


class LorentzOscillator(_ClosedModel):
    oscillator_strength: NonnegativeFloat
    resonance_angular_frequency_rad_s: PositiveFloat
    damping_angular_frequency_rad_s: NonnegativeFloat


class LorentzModel(_ClosedModel):
    model_kind: Literal["lorentz"]
    epsilon_infinity: float
    oscillators: Annotated[list[LorentzOscillator], Field(min_length=1, max_length=32)]
    extrapolation: ExtrapolationPolicy = Field(default_factory=ExtrapolationPolicy)


class TOLOMode(_ClosedModel):
    transverse_angular_frequency_rad_s: PositiveFloat
    longitudinal_angular_frequency_rad_s: PositiveFloat
    transverse_damping_rad_s: NonnegativeFloat
    longitudinal_damping_rad_s: NonnegativeFloat


class TOLOModel(_ClosedModel):
    model_kind: Literal["tolo"]
    epsilon_infinity: PositiveFloat
    modes: Annotated[list[TOLOMode], Field(min_length=1, max_length=32)]
    extrapolation: ExtrapolationPolicy = Field(default_factory=ExtrapolationPolicy)


class ThermoOpticModel(_ClosedModel):
    model_kind: Literal["thermo_optic"]
    reference_temperature_K: PositiveFloat
    refractive_index_at_reference: NonnegativeFloat
    extinction_coefficient_at_reference: NonnegativeFloat
    dn_dT_per_K: float
    dk_dT_per_K: float
    extrapolation: ExtrapolationPolicy = Field(default_factory=ExtrapolationPolicy)


OpticalModel: TypeAlias = Annotated[
    NkTableModel
    | PermittivityTableModel
    | DrudeModel
    | LorentzModel
    | TOLOModel
    | ThermoOpticModel,
    Field(discriminator="model_kind"),
]


class MaterialStateEntry(_ClosedModel):
    state_id: ExactIdentifier
    phase_id: BoundedName
    fabrication_state: BoundedText
    classification: Literal["measured", "fitted", "assumed"]
    source: SourceRecord
    validity: ValidityDomain
    uncertainty: UncertaintyModel
    measurement_conditions: MeasurementConditions
    carrier_state: CarrierState | None = None
    state_variables: Annotated[list[StateVariable], Field(max_length=64)] = Field(
        default_factory=list
    )
    phase_fraction: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    optical_model: OpticalModel


class PhaseBoundary(_ClosedModel):
    lower_state_id: ExactIdentifier
    upper_state_id: ExactIdentifier
    boundary_temperature_K: PositiveFloat
    smoothing_allowed: Literal[False] = False
    source_sha256: Sha256


class ComsolMaterialTarget(_ClosedModel):
    component_tag: ExactIdentifier
    material_tag: ExactIdentifier
    property_group_tag: ExactIdentifier
    relative_permittivity_property_key: ExactIdentifier
    function_tag_prefix: ExactIdentifier


class ThermalMaterialLedger(_ClosedModel):
    schema_name: Literal["comsol_mcp.thermal_material_ledger"] = (
        "comsol_mcp.thermal_material_ledger"
    )
    schema_version: Literal["1.0.0"] = "1.0.0"
    material_identity_sha256: Sha256
    sample_identity_sha256: Sha256
    internal_phasor_convention: Literal["exp(-i*omega*t)"] = "exp(-i*omega*t)"
    internal_passive_loss_convention: Literal["positive_imaginary_permittivity"] = (
        "positive_imaginary_permittivity"
    )
    states: Annotated[list[MaterialStateEntry], Field(min_length=1, max_length=64)]
    phase_boundaries: Annotated[list[PhaseBoundary], Field(max_length=64)] = Field(
        default_factory=list
    )
    comsol_target: ComsolMaterialTarget
    ledger_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def validate_phase_boundaries(self) -> ThermalMaterialLedger:
        state_ids = [state.state_id for state in self.states]
        if len(state_ids) != len(set(state_ids)):
            raise ValueError("material state_id values must be unique")
        by_state = {state.state_id: state for state in self.states}
        for boundary in self.phase_boundaries:
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
                raise ValueError(
                    "phase boundary temperature must be covered by both adjacent states"
                )
        return self


class ThermalMaterialEvaluationRequest(_ClosedModel):
    ledger: ThermalMaterialLedger
    state_id: ExactIdentifier
    wavelength_m: PositiveFloat
    temperature_K: PositiveFloat


__all__ = [
    "ThermalMaterialEvaluationRequest",
    "ThermalMaterialLedger",
]
