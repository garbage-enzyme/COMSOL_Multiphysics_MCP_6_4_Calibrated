"""Typed solver-free thermal radiation evidence contracts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
BoundedName = Annotated[str, Field(min_length=1, max_length=256)]
EvidenceState = Literal["verified", "failed", "unknown"]
MAX_SPECTRAL_POINTS = 2048
MAX_DATA_VALUES = 2048


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class KirchhoffAssessmentRequest(_ClosedModel):
    absorptivity_evidence_sha256: Sha256
    absorptivity_evidence_state: Literal["verified", "unknown"]
    linearity: EvidenceState
    time_invariance: EvidenceState
    reciprocity: EvidenceState
    local_equilibrium: EvidenceState
    direction_channel_match: EvidenceState
    frequency_channel_match: EvidenceState
    polarization_channel_match: EvidenceState
    propagation_direction: Literal["positive_z", "negative_z", "other", "unknown"]
    polarization_basis: Literal["scalar", "te_tm", "stokes_mueller", "unknown"]
    handedness_convention: Literal[
        "ieee_observer",
        "optics_source_view",
        "explicit_stokes",
        "not_applicable",
        "unknown",
    ]
    channel_identity_sha256: Sha256


class KirchhoffChecks(_ClosedModel):
    linearity: EvidenceState
    time_invariance: EvidenceState
    reciprocity: EvidenceState
    local_equilibrium: EvidenceState
    direction_channel_match: EvidenceState
    frequency_channel_match: EvidenceState
    polarization_channel_match: EvidenceState


class KirchhoffAssessmentReceipt(_ClosedModel):
    schema_name: Literal["comsol_mcp.kirchhoff_assessment"]
    schema_version: Literal["1.0.0"]
    disposition: Literal["applicable", "conditional", "not_applicable", "unavailable"]
    reason_codes: Annotated[list[BoundedName], Field(max_length=16)]
    absorptivity_evidence_sha256: Sha256
    absorptivity_evidence_state: Literal["verified", "unknown"]
    channel_identity_sha256: Sha256
    checks: KirchhoffChecks
    propagation_direction: Literal["positive_z", "negative_z", "other", "unknown"]
    polarization_basis: Literal["scalar", "te_tm", "stokes_mueller", "unknown"]
    handedness_convention: Literal[
        "ieee_observer",
        "optics_source_view",
        "explicit_stokes",
        "not_applicable",
        "unknown",
    ]
    assessment_sha256: Sha256


class SpectralAxis(_ClosedModel):
    representation: Literal[
        "wavelength_m",
        "wavelength_um",
        "frequency_Hz",
        "wavenumber_m_inv",
    ]
    coordinates: Annotated[
        list[Annotated[float, Field(gt=0.0)]],
        Field(min_length=2, max_length=MAX_SPECTRAL_POINTS),
    ]


class AngularGrid(_ClosedModel):
    mode: Literal["lambertian_pi", "grid_trapezoid"]
    theta_rad: Annotated[
        list[Annotated[float, Field(ge=0.0, le=1.5707963267948966)]],
        Field(min_length=1, max_length=128),
    ]
    phi_rad: Annotated[
        list[Annotated[float, Field(ge=0.0, le=6.283185307179586)]],
        Field(min_length=1, max_length=128),
    ]


class PolarizationContract(_ClosedModel):
    mode: Literal["scalar", "te_tm_incoherent", "stokes_mueller"]
    channels: Annotated[list[BoundedName], Field(min_length=1, max_length=4)]
    weights: Annotated[list[Annotated[float, Field(ge=0.0, le=1.0)]], Field(max_length=4)] = Field(
        default_factory=list
    )
    propagation_direction: Literal["positive_z", "negative_z", "other"]
    source_handedness: Literal[
        "ieee_observer", "optics_source_view", "explicit_stokes", "not_applicable"
    ]
    analyzer_handedness: Literal[
        "ieee_observer", "optics_source_view", "explicit_stokes", "not_applicable"
    ]
    analyzer_stokes: Annotated[list[float], Field(min_length=4, max_length=4)] = Field(
        default_factory=lambda: [1.0, 0.0, 0.0, 0.0]
    )
    basis_rotation_rad: Annotated[float, Field(ge=-6.283185307179586, le=6.283185307179586)] = 0.0


class DetectorPath(_ClosedModel):
    kernel_source_sha256s: Annotated[list[Sha256], Field(max_length=64)] = Field(
        default_factory=list
    )
    gas_absorption_per_m: Annotated[
        list[Annotated[float, Field(ge=0.0)]], Field(max_length=MAX_SPECTRAL_POINTS)
    ] = Field(default_factory=list)
    gas_path_length_m: Annotated[float, Field(ge=0.0, le=1.0e9)] = 0.0
    gas_concentration_scale: Annotated[float, Field(ge=0.0, le=1.0e9)] = 0.0
    aperture_weights: Annotated[
        list[Annotated[float, Field(ge=0.0)]], Field(max_length=MAX_SPECTRAL_POINTS)
    ] = Field(default_factory=list)
    optics_transmission: Annotated[
        list[Annotated[float, Field(ge=0.0, le=1.0)]], Field(max_length=MAX_SPECTRAL_POINTS)
    ] = Field(default_factory=list)
    analyzer_response: Annotated[
        list[Annotated[float, Field(ge=0.0, le=1.0)]], Field(max_length=MAX_SPECTRAL_POINTS)
    ] = Field(default_factory=list)
    detector_response: Annotated[
        list[Annotated[float, Field(ge=0.0)]], Field(max_length=MAX_SPECTRAL_POINTS)
    ] = Field(default_factory=list)
    reference_response: Annotated[
        list[Annotated[float, Field(ge=0.0)]], Field(max_length=MAX_SPECTRAL_POINTS)
    ] = Field(default_factory=list)
    background_response: Annotated[
        list[Annotated[float, Field(ge=0.0)]], Field(max_length=MAX_SPECTRAL_POINTS)
    ] = Field(default_factory=list)


class ThermalRadiationRequest(_ClosedModel):
    schema_name: Literal["comsol_mcp.thermal_radiation_request"] = (
        "comsol_mcp.thermal_radiation_request"
    )
    schema_version: Literal["1.0.0"] = "1.0.0"
    temperature_K: Annotated[float, Field(gt=0.0, le=1.0e9)]
    axis: SpectralAxis
    angular_grid: AngularGrid
    polarization: PolarizationContract
    optical_quantity: Literal["emissivity", "absorptivity"]
    values_flat: Annotated[list[float], Field(min_length=1, max_length=MAX_DATA_VALUES)]
    uncertainty_flat: Annotated[
        list[Annotated[float, Field(ge=0.0)]], Field(max_length=MAX_DATA_VALUES)
    ] = Field(default_factory=list)
    kirchhoff_assessment: KirchhoffAssessmentReceipt | None = None
    detector_path: DetectorPath = Field(default_factory=DetectorPath)
    integration_method: Literal["trapezoid"] = "trapezoid"
    interpolation: Literal["none", "linear"] = "none"
    extrapolation: bool = False
    configuration_sha256: Sha256
    source_artifact_sha256s: Annotated[list[Sha256], Field(min_length=1, max_length=64)]
    artifact_chain_sha256: Sha256

    @model_validator(mode="after")
    def validate_declared_shape(self) -> ThermalRadiationRequest:
        if self.extrapolation:
            raise ValueError("thermal radiation extrapolation is disabled")
        dimensions = (
            len(self.axis.coordinates),
            len(self.angular_grid.theta_rad),
            len(self.angular_grid.phi_rad),
            len(self.polarization.channels),
        )
        expected = math_product(dimensions)
        if len(self.values_flat) != expected:
            raise ValueError("values_flat length does not match the declared grid shape")
        if self.uncertainty_flat and len(self.uncertainty_flat) != expected:
            raise ValueError("uncertainty_flat length does not match values_flat")
        if self.angular_grid.mode == "lambertian_pi" and dimensions[1:3] != (1, 1):
            raise ValueError("lambertian_pi requires one declared angular sample")
        if self.polarization.mode == "scalar":
            if self.polarization.channels != ["scalar"]:
                raise ValueError("scalar polarization requires the scalar channel")
        elif self.polarization.mode == "te_tm_incoherent":
            if self.polarization.channels != ["TE", "TM"]:
                raise ValueError("TE/TM polarization requires channels ['TE', 'TM']")
            if len(self.polarization.weights) != 2 or not math_close(
                sum(self.polarization.weights), 1.0
            ):
                raise ValueError("TE/TM incoherent weights must contain two values summing to one")
        elif self.polarization.channels != ["I", "Q", "U", "V"]:
            raise ValueError("Stokes polarization requires channels ['I', 'Q', 'U', 'V']")
        if self.polarization.mode == "stokes_mueller":
            analyzer = self.polarization.analyzer_stokes
            if (
                analyzer[0] < 0.0
                or (analyzer[1] ** 2 + analyzer[2] ** 2 + analyzer[3] ** 2)
                > analyzer[0] ** 2 + 1.0e-12
            ):
                raise ValueError("analyzer Stokes vector is not physically admissible")
        detector_arrays = (
            self.detector_path.gas_absorption_per_m,
            self.detector_path.aperture_weights,
            self.detector_path.optics_transmission,
            self.detector_path.analyzer_response,
            self.detector_path.detector_response,
            self.detector_path.reference_response,
            self.detector_path.background_response,
        )
        if any(detector_arrays) and not self.detector_path.kernel_source_sha256s:
            raise ValueError("detector-path arrays require kernel_source_sha256s provenance")
        return self


def math_product(values: tuple[int, ...]) -> int:
    result = 1
    for value in values:
        result *= value
    return result


def math_close(left: float, right: float) -> bool:
    return abs(left - right) <= 1.0e-12


__all__ = [
    "KirchhoffAssessmentRequest",
    "KirchhoffAssessmentReceipt",
    "ThermalRadiationRequest",
]
