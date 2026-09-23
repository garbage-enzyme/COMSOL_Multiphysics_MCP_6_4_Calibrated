"""Typed solver-free input contracts for the bounded public surrogate tools.

These contracts cover only validation, preview, inspection, and verification of
surrogate artifacts.  They never start COMSOL, never acquire a solver lease, and
never promote a surrogate prediction to FEM evidence.  Training and evaluation
stay inside durable jobs.

Every filesystem path is a **flat argument name** on the tool signature.  That is
deliberate: contained-path enforcement is driven by per-tool argument-name tables
in :mod:`comsol_mcp.path_policy`, so a path nested inside an object would bypass
containment entirely.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_HEX64 = r"^[0-9a-fA-F]{64}$"

# The only source kinds a surrogate artifact may declare.  ``dbmodel`` is frozen
# as an offline URI/schema/evidence-validated kind: live Model Manager
# operations are deliberately not part of 0.7.5.
SOURCE_KINDS = ("file", "directory", "dbmodel")

# A frozen ``dbmodel://`` URI, validated as syntax only.  The shape is
# ``dbmodel://<authority>/<path>`` with optional ``?sha256=<hex>``.  The resource
# pattern deliberately permits ``.`` so a traversal attempt reaches the explicit
# traversal check below and is refused with an accurate message, rather than
# being reported as a generic syntax mismatch.
_DBMODEL_URI = re.compile(
    r"^dbmodel://(?P<authority>[A-Za-z0-9][A-Za-z0-9._-]{0,63})"
    r"/(?P<resource>[A-Za-z0-9.][A-Za-z0-9._/-]{0,512})"
    r"(?:\?sha256=(?P<sha256>[0-9a-fA-F]{64}))?$"
)

MAX_URI_LENGTH = 1024


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class SurrogateReadLimits(_ClosedModel):
    """Caller-overridable bounded reader limits within fixed hard caps.

    Only limits this surface actually enforces are declared.  A caller-supplied
    bound that nothing applies would be worse than an absent one, because it
    would read as a guarantee.
    """

    max_document_bytes: Annotated[int, Field(ge=1, le=33_554_432)] = 4_194_304
    max_rows: Annotated[int, Field(ge=1, le=4096)] = 4096
    max_columns: Annotated[int, Field(ge=1, le=256)] = 64


def parse_dbmodel_uri(value: str) -> dict[str, str | None]:
    """Validate one frozen ``dbmodel://`` URI and return its components.

    This is pure syntax and evidence validation.  It performs **no** Model
    Manager lookup, no network access, and no filesystem access, so freezing the
    source kind cannot silently acquire a live dependency.
    """
    if not isinstance(value, str):
        raise ValueError("dbmodel uri must be a string")
    if not value or len(value) > MAX_URI_LENGTH:
        raise ValueError("dbmodel uri length is outside the allowed range")
    if not value.isascii():
        raise ValueError("dbmodel uri must be ASCII")
    match = _DBMODEL_URI.match(value)
    if match is None:
        raise ValueError("dbmodel uri does not match dbmodel://authority/resource")
    resource = match.group("resource")
    if ".." in resource.split("/") or resource.startswith("/") or resource.endswith("/"):
        raise ValueError("dbmodel uri resource must be a relative path without traversal")
    digest = match.group("sha256")
    return {
        "authority": match.group("authority"),
        "resource": resource,
        "sha256": digest.lower() if digest else None,
    }


def validate_source_reference(
    *,
    source_kind: str,
    source_path: str | None,
    source_uri: str | None,
) -> dict[str, object]:
    """Validate one source reference and report whether it touches a filesystem.

    ``dbmodel`` is refused unless it supplies a syntactically valid URI and no
    local path, so the two representations can never be conflated.
    """
    if source_kind not in SOURCE_KINDS:
        raise ValueError(f"source_kind must be one of: {', '.join(SOURCE_KINDS)}")
    if source_kind == "dbmodel":
        if source_path is not None:
            raise ValueError("a dbmodel source must not declare a local path")
        if source_uri is None:
            raise ValueError("a dbmodel source requires a source_uri")
        components = parse_dbmodel_uri(source_uri)
        return {
            "source_kind": "dbmodel",
            "readable": False,
            "filesystem_access": False,
            "live_model_manager_access": False,
            "components": components,
        }
    if source_uri is not None:
        raise ValueError("only a dbmodel source may declare a source_uri")
    if source_path is None:
        raise ValueError("a file or directory source requires a source_path")
    return {
        "source_kind": source_kind,
        "readable": True,
        "filesystem_access": True,
        "live_model_manager_access": False,
        "components": None,
    }


def _require_unique_names(value: list[str] | None, field: str) -> list[str] | None:
    if value is None:
        return None
    if not value:
        raise ValueError(f"{field} must be non-empty when declared")
    for name in value:
        if not name or not name.strip():
            raise ValueError(f"{field} entries must be non-empty strings")
        if not name.isascii():
            raise ValueError(f"{field} entries must be ASCII")
    if len(set(value)) != len(value):
        raise ValueError(f"{field} entries must be unique")
    return list(value)


class SurrogateDatasetValidateInput(_ClosedModel):
    """Validate one surrogate dataset file against its declared field schema.

    The dataset is read only far enough to bind its header, row count, and
    content hash.  It is refused rather than truncated when it exceeds a
    declared bound.

    ``source_kind`` selects the source representation.  A ``file`` source is read
    through ``dataset_path``.  A ``dbmodel`` source is a frozen Model Manager URI:
    it is validated as syntax and evidence only, is never read, and never touches
    the filesystem or the live Model Manager.  The two representations are
    mutually exclusive so a local path can never be presented as a remote model.
    """

    dataset_path: Annotated[str | None, Field(min_length=1, max_length=4096)] = None
    source_kind: Literal["file", "directory", "dbmodel"] = "file"
    source_uri: Annotated[str | None, Field(min_length=1, max_length=MAX_URI_LENGTH)] = None
    field_schema_path: Annotated[str | None, Field(min_length=1, max_length=4096)] = None
    expected_row_count: Annotated[int | None, Field(ge=0, le=4096)] = None
    expected_feature_names: Annotated[list[str] | None, Field(max_length=64)] = None
    expected_target_names: Annotated[list[str] | None, Field(max_length=64)] = None
    limits: SurrogateReadLimits | None = None

    @model_validator(mode="after")
    def _check_names(self) -> "SurrogateDatasetValidateInput":
        _require_unique_names(self.expected_feature_names, "expected_feature_names")
        _require_unique_names(self.expected_target_names, "expected_target_names")
        overlap = set(self.expected_feature_names or ()) & set(self.expected_target_names or ())
        if overlap:
            raise ValueError(
                "a name cannot be both a feature and a target: " + ", ".join(sorted(overlap))
            )
        # Raises with the contract's own reason text when the reference is
        # inconsistent, so dispatch can rely on one enforcement point.
        validate_source_reference(
            source_kind=self.source_kind,
            source_path=self.dataset_path,
            source_uri=self.source_uri,
        )
        return self


class SurrogateTrainingPreviewInput(_ClosedModel):
    """Preview a bounded surrogate training configuration without training.

    Returns the validated configuration, the derived typed write plan, and the
    deferred writes that require a bound data source.  It never creates a COMSOL
    node, never starts a solver, and never trains.
    """

    configuration: Annotated[dict[str, Any], Field(min_length=1)]
    dataset_path: Annotated[str | None, Field(min_length=1, max_length=4096)] = None
    declared_fem_row_count: Annotated[int | None, Field(ge=0, le=4096)] = None
    limits: SurrogateReadLimits | None = None


class SurrogateModelInspectInput(_ClosedModel):
    """Inspect one export manifest, model card, or registry entry.

    ``document_kind='auto'`` detects the document schema from its own declared
    ``schema`` field and refuses an unrecognized document rather than guessing.
    """

    document_path: Annotated[str, Field(min_length=1, max_length=4096)]
    document_kind: Literal["export_manifest", "model_card", "registry_entry", "auto"] = "auto"
    limits: SurrogateReadLimits | None = None


class SurrogateModelVerifyInput(_ClosedModel):
    """Verify a surrogate model document against declared expectations.

    Every expectation is optional; only the declared ones are checked, and an
    undeclared expectation is never reported as satisfied.
    """

    document_path: Annotated[str, Field(min_length=1, max_length=4096)]
    expected_trained_chksum: Annotated[str | None, Field(min_length=1, max_length=64)] = None
    expected_artifact_sha256: Annotated[str | None, Field(pattern=_HEX64)] = None
    expected_architecture_sha256: Annotated[str | None, Field(pattern=_HEX64)] = None
    expected_dataset_manifest_sha256: Annotated[str | None, Field(pattern=_HEX64)] = None
    expected_split_manifest_sha256: Annotated[str | None, Field(pattern=_HEX64)] = None
    require_consistent_export: bool = False
    limits: SurrogateReadLimits | None = None


class SurrogatePredictionValidateInput(_ClosedModel):
    """Validate one prediction row set without promoting it to FEM evidence.

    A prediction row set must declare no FEM evidence field; any row claiming
    FEM evidence is refused, and the tool always reports that the result remains
    a prediction.
    """

    prediction_path: Annotated[str, Field(min_length=1, max_length=4096)]
    absolute_tolerance: Annotated[float, Field(ge=0.0, le=1_000_000.0)] = 0.0
    relative_tolerance: Annotated[float, Field(ge=0.0, le=1_000_000.0)] = 0.0
    ood_state: Literal["in_domain", "edge", "out_of_domain", "uncalibrated"] | None = None
    limits: SurrogateReadLimits | None = None


__all__ = [
    "MAX_URI_LENGTH",
    "SOURCE_KINDS",
    "SurrogateDatasetValidateInput",
    "SurrogateModelInspectInput",
    "SurrogateModelVerifyInput",
    "SurrogatePredictionValidateInput",
    "SurrogateReadLimits",
    "SurrogateTrainingPreviewInput",
    "parse_dbmodel_uri",
    "validate_source_reference",
]
