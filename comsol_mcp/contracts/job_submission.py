"""Bounded discriminated contracts for public durable-job submission."""

from __future__ import annotations

import copy
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from .structural import MAX_PUBLIC_NUMBER_MAGNITUDE, validate_public_structure
from .thermo_optomechanical import ThermoOptomechanicalReplayInput
from .wavelength_policy import StrictWavelengthPolicy

MAX_PUBLIC_TEXT = 4096
MAX_PUBLIC_PATH = 1024
MAX_JOB_COLLECTION = 2048
#: Caller decision 2026-10-30 (B081-017): retain an actually USABLE
#: 2048-point staged-sweep bound. The durable resource journal reaches that
#: through bounded segmentation (four entries per point plus a startup
#: reserve roll across bounded resource-segment-N.jsonl files), so the
#: public bound matches what admission really accepts instead of
#: advertising 2048 while unconditionally refusing beyond 1022 points.
MAX_STAGED_SWEEP_POINTS = 2048
#: The validation matrix is the one job family whose runtime point bound is
#: deliberately narrower than the generic collection bound; the schema and
#: the runtime normalizer share this single constant.
MAX_VALIDATION_MATRIX_POINTS = 32
MAX_JOB_OBJECT_FIELDS = 256

BoundedText: TypeAlias = Annotated[str, Field(min_length=1, max_length=MAX_PUBLIC_TEXT)]
BoundedPath: TypeAlias = Annotated[str, Field(min_length=1, max_length=MAX_PUBLIC_PATH)]
BoundedObject: TypeAlias = Annotated[
    dict[str, Any],
    Field(max_length=MAX_JOB_OBJECT_FIELDS),
]
BoundedObjectList: TypeAlias = Annotated[
    list[BoundedObject],
    Field(min_length=1, max_length=MAX_JOB_COLLECTION),
]
BoundedSweepNumber: TypeAlias = Annotated[
    int | float,
    Field(
        ge=-MAX_PUBLIC_NUMBER_MAGNITUDE,
        le=MAX_PUBLIC_NUMBER_MAGNITUDE,
        allow_inf_nan=False,
    ),
]


class _JobInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class StagedSweepInput(_JobInput):
    job_type: Literal["staged_sweep"]
    source_model_path: BoundedPath
    parameter_name: BoundedText
    parameter_values: Annotated[
        list[BoundedSweepNumber],
        Field(min_length=1, max_length=MAX_STAGED_SWEEP_POINTS),
    ]
    expressions: Annotated[
        list[BoundedText],
        Field(min_length=1, max_length=MAX_JOB_COLLECTION),
    ]
    parameter_unit: BoundedText | None = None
    study_name: BoundedText | None = None
    study_step_tag: BoundedText | None = None
    study_step_property: BoundedText | None = None
    study_step_unit: BoundedText | None = None
    study_step_unit_property: BoundedText | None = None
    physical_bounds: BoundedObject | None = None
    max_retries: Annotated[int, Field(ge=0, le=100)] | None = None
    continue_on_error: bool | None = None
    checkpoint_every: Annotated[int, Field(ge=1, le=MAX_JOB_COLLECTION)] | None = None
    cores: Annotated[int, Field(ge=1, le=1024)] | None = None
    version: BoundedText | None = None
    smoke_points: Literal[1, 2] | None = None
    record_wavelength_controls: bool | None = None
    strict_wavelength_policy: StrictWavelengthPolicy | None = None
    resource_policy: BoundedObject | None = None
    execution_backend: BoundedObject | None = None

    @model_validator(mode="after")
    def validate_cross_field_contract(self) -> StagedSweepInput:
        if self.strict_wavelength_policy is not None:
            from .wavelength_policy import normalize_policy, requested_metres

            normalize_policy(self.strict_wavelength_policy)
            if not self.study_name or not self.study_step_tag:
                raise ValueError("strict wavelength mode requires study and step tags")
            if self.parameter_name not in {"wl", "wavelength"}:
                raise ValueError("strict wavelength mode requires a wavelength parameter")
            if self.record_wavelength_controls is False:
                raise ValueError("strict wavelength mode requires recorded controls")
            for value in self.parameter_values:
                requested_metres(value, self.parameter_unit)
        if self.smoke_points is not None and self.smoke_points > len(self.parameter_values):
            raise ValueError("smoke_points must not exceed the parameter_values count")
        if self.physical_bounds is not None:
            unknown = sorted(set(self.physical_bounds) - set(self.expressions))
            if unknown:
                raise ValueError("physical_bounds keys must be requested expressions")
        return self


class AdjointOptimizationInput(_JobInput):
    job_type: Literal["adjoint_optimization"]
    submission_manifest_path: BoundedPath
    submission_manifest_sha256: Annotated[str, Field(pattern="^[0-9a-fA-F]{64}$")]
    cores: Annotated[int, Field(ge=1, le=1024)]
    version: Annotated[str, Field(min_length=1, max_length=32)]
    resource_policy: BoundedObject
    schema_name: Literal["comsol_mcp.adjoint_optimization_submission"] | None = None
    schema_version: Literal["1.0.0"] | None = None


class ValidationMatrixInput(_JobInput):
    job_type: Literal["validation_matrix"]
    source_model_path: BoundedPath
    points: BoundedObjectList
    point_limit: Annotated[int, Field(ge=1, le=MAX_VALIDATION_MATRIX_POINTS)]
    resource_policy: BoundedObject
    startup_admission: BoundedObject | None = None
    comsol_temporary_directory: BoundedPath | None = None
    cores: Annotated[int, Field(ge=1, le=1024)]
    version: BoundedText | None = None
    max_retries: Annotated[int, Field(ge=0, le=3)] | None = None
    continue_on_error: bool | None = None


JobSubmissionSpec: TypeAlias = Annotated[
    StagedSweepInput
    | ValidationMatrixInput
    | AdjointOptimizationInput
    | ThermoOptomechanicalReplayInput,
    Field(discriminator="job_type"),
]
_JOB_SUBMISSION_ADAPTER: TypeAdapter[JobSubmissionSpec] = TypeAdapter(JobSubmissionSpec)


def job_submission_dict(value: JobSubmissionSpec | dict[str, Any]) -> dict[str, Any]:
    """Return only caller-supplied fields so legacy fingerprints remain stable."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python", exclude_unset=True)
    if isinstance(value, dict):
        # Deep-copy like the BaseModel branch: nested dicts and lists must not
        # alias the caller's object, or later mutation on either side would
        # silently change the fingerprinted snapshot.
        return copy.deepcopy(value)
    raise ValueError("Job specification must be an object")


def validate_job_submission(value: object) -> dict[str, Any]:
    """Validate and normalize caller fields through the discovery contract."""
    normalized = job_submission_dict(_JOB_SUBMISSION_ADAPTER.validate_python(value))
    validate_public_structure(normalized, path="job_submission")
    return normalized


__all__ = [
    "AdjointOptimizationInput",
    "JobSubmissionSpec",
    "StagedSweepInput",
    "ThermoOptomechanicalReplayInput",
    "ValidationMatrixInput",
    "job_submission_dict",
    "validate_job_submission",
]
