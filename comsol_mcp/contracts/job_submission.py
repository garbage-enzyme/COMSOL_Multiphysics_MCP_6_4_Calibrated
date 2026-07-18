"""Bounded discriminated contracts for public durable-job submission."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


MAX_PUBLIC_TEXT = 4096
MAX_PUBLIC_PATH = 1024
MAX_JOB_COLLECTION = 2048
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


class _JobInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StagedSweepInput(_JobInput):
    job_type: Literal["staged_sweep"]
    source_model_path: BoundedPath
    parameter_name: BoundedText
    parameter_values: Annotated[
        list[int | float],
        Field(min_length=1, max_length=MAX_JOB_COLLECTION),
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
    resource_policy: BoundedObject | None = None
    execution_backend: BoundedObject | None = None


class ValidationMatrixInput(_JobInput):
    job_type: Literal["validation_matrix"]
    source_model_path: BoundedPath
    points: BoundedObjectList
    point_limit: Annotated[int, Field(ge=1, le=MAX_JOB_COLLECTION)]
    resource_policy: BoundedObject
    cores: Annotated[int, Field(ge=1, le=1024)]
    version: BoundedText | None = None
    max_retries: Annotated[int, Field(ge=0, le=3)] | None = None
    continue_on_error: bool | None = None


class SpectralCharacterizationInput(_JobInput):
    job_type: Literal["spectral_characterization"]
    source_model_path: BoundedPath
    source_model_relative_identity: BoundedPath
    configuration_sha256: Annotated[str, Field(pattern=r"^[0-9a-fA-F]{64}$")]
    parameter_state: BoundedObject
    wavelength_parameter: BoundedText
    initial_grid: BoundedObject
    refinement_policy: BoundedObject
    expansion_policy: BoundedObject
    maximum_points: Annotated[int, Field(ge=3, le=MAX_JOB_COLLECTION)]
    collector: BoundedObject
    analysis_policy: BoundedObject
    measurement_configuration: BoundedObject
    resource_policy: BoundedObject
    cores: Annotated[int, Field(ge=1, le=1024)]
    version: BoundedText | None = None
    max_retries: Annotated[int, Field(ge=0, le=3)] | None = None
    continue_on_error: bool | None = None


class ConvergenceCampaignInput(_JobInput):
    job_type: Literal["convergence_campaign"]
    campaign_id: BoundedText
    levels: BoundedObjectList
    convergence_policy: BoundedObject
    stop_policy: BoundedObject
    maximum_total_points: Annotated[int, Field(ge=1, le=MAX_JOB_COLLECTION)]
    wall_time_budget_seconds: Annotated[int, Field(ge=1, le=31_536_000)]


class BranchContinuationCampaignInput(_JobInput):
    job_type: Literal["branch_continuation_campaign"]
    campaign_id: BoundedText
    states: BoundedObjectList
    continuation_policy: BoundedObject
    maximum_total_points: Annotated[int, Field(ge=1, le=MAX_JOB_COLLECTION)]
    wall_time_budget_seconds: Annotated[int, Field(ge=1, le=31_536_000)]


JobSubmissionSpec: TypeAlias = Annotated[
    StagedSweepInput
    | ValidationMatrixInput
    | SpectralCharacterizationInput
    | ConvergenceCampaignInput
    | BranchContinuationCampaignInput,
    Field(discriminator="job_type"),
]
_JOB_SUBMISSION_ADAPTER = TypeAdapter(JobSubmissionSpec)


def job_submission_dict(value: JobSubmissionSpec | dict[str, Any]) -> dict[str, Any]:
    """Return only caller-supplied fields so legacy fingerprints remain stable."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python", exclude_unset=True)
    if isinstance(value, dict):
        return dict(value)
    raise ValueError("Job specification must be an object")


def validate_job_submission(value: object) -> dict[str, Any]:
    """Validate and normalize caller fields through the discovery contract."""
    return job_submission_dict(_JOB_SUBMISSION_ADAPTER.validate_python(value))


__all__ = [
    "BranchContinuationCampaignInput",
    "ConvergenceCampaignInput",
    "JobSubmissionSpec",
    "SpectralCharacterizationInput",
    "StagedSweepInput",
    "ValidationMatrixInput",
    "job_submission_dict",
    "validate_job_submission",
]
