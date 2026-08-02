"""Typed solver-free simulation configuration contracts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

BoundedName = Annotated[str, Field(min_length=1, max_length=256)]
BoundedLabel = Annotated[str, Field(min_length=1, max_length=1024)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
QuantityDimension = Literal[
    "length",
    "angle",
    "temperature",
    "dimensionless",
    "frequency",
    "energy",
]


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DeclaredQuantity(_ClosedModel):
    status: Literal["known", "unknown"] = "known"
    dimension: QuantityDimension
    value: float | None = None
    unit: BoundedName | None = None

    @model_validator(mode="after")
    def validate_availability(self) -> DeclaredQuantity:
        if self.status == "known" and (self.value is None or self.unit is None):
            raise ValueError("known quantities require value and unit")
        if self.status == "unknown" and (self.value is not None or self.unit is not None):
            raise ValueError("unknown quantities cannot declare value or unit")
        return self


class SourceIdentity(_ClosedModel):
    relative_identity: Annotated[str, Field(min_length=1, max_length=1024)]
    content_sha256: Sha256
    format: Literal["mph", "json", "yaml", "toml", "other"]


class ProducerIdentity(_ClosedModel):
    tool: BoundedName
    version: BoundedName
    contract_sha256: Sha256


class GeometryDimension(_ClosedModel):
    dimension_id: BoundedName
    semantic: Literal[
        "period_x",
        "period_y",
        "full_width",
        "half_width",
        "radius",
        "diameter",
        "thickness",
        "height",
        "gap",
        "offset",
        "other",
    ]
    quantity: DeclaredQuantity
    label: BoundedLabel | None = None


class MaterialState(_ClosedModel):
    material_id: BoundedName
    region_id: BoundedName
    model_identity_sha256: Sha256
    temperature: DeclaredQuantity
    loss_sign_convention: Literal["positive_imaginary_loss", "negative_imaginary_loss", "unknown"]
    label: BoundedLabel | None = None


class LayerDeclaration(_ClosedModel):
    layer_id: BoundedName
    material_id: BoundedName
    order: Annotated[int, Field(ge=0, le=2047)]
    thickness: DeclaredQuantity
    label: BoundedLabel | None = None


class IncidenceDeclaration(_ClosedModel):
    theta: DeclaredQuantity
    phi: DeclaredQuantity
    propagation_direction: Literal["positive_z", "negative_z", "other", "unknown"]
    polarization_basis: Literal["sp", "xy", "circular", "jones", "unknown"]
    polarization_state: BoundedName
    handedness_convention: Literal[
        "ieee_observer",
        "optics_source_view",
        "explicit_jones",
        "not_applicable",
        "unknown",
    ]


class WavelengthControlDeclaration(_ClosedModel):
    driver: Literal["parameter", "frequency", "study_expression", "dataset", "unknown"]
    parameter_name: BoundedName | None = None
    requested: DeclaredQuantity
    evaluated: DeclaredQuantity

    @model_validator(mode="after")
    def validate_wavelength_dimensions(self) -> WavelengthControlDeclaration:
        if self.requested.dimension != "length" or self.evaluated.dimension != "length":
            raise ValueError("wavelength controls must use length quantities")
        if self.driver == "parameter" and self.parameter_name is None:
            raise ValueError("parameter wavelength control requires parameter_name")
        return self


class MeshDeclaration(_ClosedModel):
    dependency_keys: Annotated[list[BoundedName], Field(max_length=256)] = []
    characteristic_lengths: Annotated[list[GeometryDimension], Field(max_length=256)] = []


class ModelTreeIdentity(_ClosedModel):
    physics: Annotated[list[BoundedName], Field(max_length=256)] = []
    studies: Annotated[list[BoundedName], Field(max_length=256)] = []
    solvers: Annotated[list[BoundedName], Field(max_length=256)] = []
    datasets: Annotated[list[BoundedName], Field(max_length=256)] = []
    selections: Annotated[list[BoundedName], Field(max_length=256)] = []


class SolverDeclaration(_ClosedModel):
    formulation: BoundedName
    termination_condition: Literal[
        "fixed_iterations",
        "relative_tolerance",
        "absolute_tolerance",
        "residual_tolerance",
        "other",
        "unknown",
    ]
    boundary_termination: Literal[
        "periodic",
        "port",
        "pml",
        "scattering",
        "pec",
        "pmc",
        "other",
        "unknown",
    ]


class UnitContract(_ClosedModel):
    quantity: BoundedName
    requested_unit: BoundedName
    evaluated_unit: BoundedName


class ArtifactChainReference(_ClosedModel):
    chain_sha256: Sha256
    role: BoundedName


class SimulationConfigurationInput(_ClosedModel):
    schema_name: Literal["comsol_mcp.simulation_configuration"] = (
        "comsol_mcp.simulation_configuration"
    )
    schema_version: Literal["1.0.0"] = "1.0.0"
    method: Literal["fem", "rcwa", "hybrid", "other"]
    source: SourceIdentity
    producer: ProducerIdentity
    geometry: Annotated[list[GeometryDimension], Field(max_length=512)] = []
    materials: Annotated[list[MaterialState], Field(max_length=512)] = []
    layers: Annotated[list[LayerDeclaration], Field(max_length=512)] = []
    incidence: IncidenceDeclaration
    wavelength_control: WavelengthControlDeclaration
    mesh: MeshDeclaration
    model_tree: ModelTreeIdentity
    solver: SolverDeclaration
    unit_contracts: Annotated[list[UnitContract], Field(max_length=256)] = []
    artifact_chains: Annotated[list[ArtifactChainReference], Field(max_length=256)] = []
    configuration_sha256: Sha256 | None = None


class ConfigurationDiffTolerance(_ClosedModel):
    length_m: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    angle_rad: Annotated[float, Field(ge=0.0, le=6.283185307179586)] = 0.0
    temperature_K: Annotated[float, Field(ge=0.0, le=1.0e6)] = 0.0
    frequency_Hz: Annotated[float, Field(ge=0.0, le=1.0e30)] = 0.0
    energy_J: Annotated[float, Field(ge=0.0, le=1.0e6)] = 0.0
    dimensionless: Annotated[float, Field(ge=0.0, le=1.0e6)] = 0.0


class ConfigurationDiffPolicy(_ClosedModel):
    tolerances: ConfigurationDiffTolerance = ConfigurationDiffTolerance()


__all__ = [
    "ConfigurationDiffPolicy",
    "SimulationConfigurationInput",
]
