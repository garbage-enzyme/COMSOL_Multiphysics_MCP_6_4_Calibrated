"""Closed public input contract for durable thermo-optomechanical replay."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

_Text = Annotated[str, Field(min_length=1, max_length=4096)]
_Path = Annotated[str, Field(min_length=1, max_length=1024)]
_Identifier = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,127}$")]
_Sha256 = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{64}$")]
_Positive = Annotated[float, Field(gt=0.0, le=1.0e300)]


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ThermoOpticalEvidenceExpressions(_ClosedModel):
    temperature_min: _Text
    temperature_max: _Text
    displacement_max: _Text
    stress_max: _Text
    heat_source_integral: _Text
    boundary_loss_integral: _Text
    delta_length: _Text
    minimum_mesh_quality: _Text
    mesh_element_count: _Text
    mesh_vertex_count: _Text
    reflectance: _Text
    transmittance: _Text
    absorptance: _Text


class ThermoOpticalModelContract(_ClosedModel):
    component_tag: _Identifier
    heat_transfer_tag: _Identifier
    solid_mechanics_tag: _Identifier
    moving_mesh_tag: _Identifier
    wave_optics_tag: _Identifier
    thermal_structure_study_tag: _Identifier
    transfer_study_tag: _Identifier
    optical_study_tag: _Identifier
    initial_temperature_parameter: _Identifier
    ambient_temperature_parameter: _Identifier
    applied_temperature_parameter: _Identifier
    cte_parameter: _Identifier
    reference_temperature_parameter: _Identifier
    wavelength_parameter: _Identifier
    polarization_parameter: _Identifier
    deformation_scale_parameter: _Identifier
    heated_domain_selection: _Identifier
    structural_domain_selection: _Identifier
    fixed_boundary_selection: _Identifier
    thermal_boundary_selection: _Identifier
    optical_domain_selection: _Identifier
    mesh_tag: _Identifier
    expressions: ThermoOpticalEvidenceExpressions


class ThermoOpticalMaterialValidity(_ClosedModel):
    wavelength_min_m: _Positive
    wavelength_max_m: _Positive
    temperature_min_K: _Positive
    temperature_max_K: _Positive


class ThermoOpticalMaterialTarget(_ClosedModel):
    component_tag: _Identifier
    material_tag: _Identifier
    property_group_tag: _Identifier
    property_key: _Identifier


class ThermoOpticalMaterialStateReference(_ClosedModel):
    schema_name: Literal["comsol_mcp.thermal_material_state_reference"]
    schema_version: Literal["1.0.0"] = "1.0.0"
    ledger_sha256: _Sha256
    material_identity_sha256: _Sha256
    sample_identity_sha256: _Sha256
    state_id: _Identifier
    classification: Literal["measured", "fitted", "assumed"]
    validity: ThermoOpticalMaterialValidity
    target: ThermoOpticalMaterialTarget
    source_model_sha256: _Sha256
    expected_property_values: Annotated[list[_Text], Field(min_length=1, max_length=9)]
    expected_function_tags: Annotated[list[_Identifier], Field(max_length=16, default_factory=list)]
    application_receipt_sha256: _Sha256


class ThermoOpticalThermalLoad(_ClosedModel):
    temperature_unit: Literal["K"] = "K"
    heat_source_unit: Literal["W/m^3"] = "W/m^3"
    convection_coefficient_unit: Literal["W/(m^2*K)"] = "W/(m^2*K)"
    initial_temperature_K: _Positive
    ambient_temperature_K: _Positive
    applied_temperature_K: _Positive
    volumetric_heat_source_W_per_m3: Annotated[
        float,
        Field(ge=0.0, le=1.0e300, title="Volumetric heat source"),
    ]
    convection_coefficient_W_per_m2_K: Annotated[
        float,
        Field(gt=0.0, le=1.0e300, title="Convection coefficient"),
    ]


class ThermoOpticalExpansion(_ClosedModel):
    coefficient_input_type: Literal["secant_coefficient", "tangent_coefficient"]
    coefficient_per_K: Annotated[float, Field(ge=-1.0, le=1.0)]
    reference_temperature_K: _Positive
    reference_length_m: _Positive
    measurement_axis: Literal["x", "y", "z"]


class ThermoOpticalTransfer(_ClosedModel):
    method: Literal["moving_mesh_spatial_frame", "materialized_remesh"]
    displacement_frame: Literal["spatial"]
    topology_change_allowed: Literal[False] = False
    deformation_scale: Annotated[float, Field(gt=0.0, le=1000.0)] = 1.0


class ThermoOpticalReplay(_ClosedModel):
    wavelengths_m: Annotated[list[_Positive], Field(min_length=1, max_length=64)]
    branches: Annotated[
        list[Literal["TE", "TM", "S", "P"]],
        Field(min_length=1, max_length=4),
    ]
    wavelength_coordinate: Literal["vacuum_wavelength_m"] = "vacuum_wavelength_m"


class ThermoOpticalAcceptancePolicy(_ClosedModel):
    expansion_relative_tolerance: Annotated[float, Field(ge=0.0, le=1.0)]
    zero_control_absolute_tolerance_m: Annotated[float, Field(ge=0.0, le=1.0)]
    energy_relative_tolerance: Annotated[float, Field(ge=0.0, le=1.0)]
    rta_closure_absolute_tolerance: Annotated[float, Field(ge=0.0, le=1.0)]
    minimum_mesh_quality: Annotated[float, Field(gt=0.0, le=1.0)]
    maximum_displacement_to_length: Annotated[float, Field(gt=0.0, le=1.0)]


ThermoOpticalControl = Literal[
    "positive_expansion",
    "zero_cte",
    "zero_temperature_rise",
    "fixed_boundary",
    "convection",
    "wrong_selection",
    "temperature_unit",
    "missing_material_state",
    "bad_mesh",
    "rollback",
]


class ThermoOptomechanicalReplayManifest(_ClosedModel):
    job_type: Literal["thermo_optomechanical_replay"]
    source_model_path: _Path
    source_model_relative_identity: _Path
    optical_configuration_sha256: _Sha256
    material_state: ThermoOpticalMaterialStateReference
    model_contract: ThermoOpticalModelContract
    thermal_load: ThermoOpticalThermalLoad
    thermal_expansion: ThermoOpticalExpansion
    deformation_transfer: ThermoOpticalTransfer
    optical_replay: ThermoOpticalReplay
    validation_controls: Annotated[list[ThermoOpticalControl], Field(min_length=10, max_length=10)]
    acceptance_policy: ThermoOpticalAcceptancePolicy
    resource_policy: Annotated[dict[str, object], Field(max_length=256)]
    cores: Annotated[int, Field(ge=1, le=1024)]
    wall_time_budget_seconds: Annotated[int, Field(ge=1, le=31_536_000)]
    version: _Text | None = None
    max_retries: Annotated[int, Field(ge=0, le=3)] | None = None
    continue_on_error: bool | None = None


class ThermoOptomechanicalReplayInput(_ClosedModel):
    job_type: Literal["thermo_optomechanical_replay"]
    specification_path: _Path
    specification_sha256: _Sha256


__all__ = [
    "ThermoOptomechanicalReplayInput",
    "ThermoOptomechanicalReplayManifest",
]
