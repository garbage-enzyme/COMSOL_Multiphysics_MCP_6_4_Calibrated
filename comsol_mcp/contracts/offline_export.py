"""Typed solver-free input contracts for offline export-manifest validation."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

_HEX64 = r"^[0-9a-fA-F]{64}$"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class OfflineExportLimits(_ClosedModel):
    """Caller-overridable bounded validator limits within fixed hard caps."""

    max_manifest_bytes: Annotated[int, Field(ge=1, le=33_554_432)] = 1_048_576
    max_artifacts: Annotated[int, Field(ge=1, le=512)] = 64
    max_artifact_bytes: Annotated[int, Field(ge=1, le=17_179_869_184)] = 268_435_456
    max_expressions: Annotated[int, Field(ge=1, le=256)] = 32
    max_parameter_entries: Annotated[int, Field(ge=0, le=512)] = 64
    max_time_values: Annotated[int, Field(ge=0, le=65_536)] = 1024


class OfflineExportValidateInput(_ClosedModel):
    """One bounded offline export-manifest validation request.

    ``base_directory`` defaults to the manifest's own directory when omitted.
    ``expected_model_sha256``, when declared, must match the manifest's model
    identity or validation fails closed.
    """

    manifest_path: Annotated[str, Field(min_length=1, max_length=4096)]
    base_directory: Annotated[str | None, Field(min_length=1, max_length=4096)] = None
    expected_model_sha256: Annotated[str | None, Field(pattern=_HEX64)] = None
    limits: OfflineExportLimits | None = None


__all__ = ["OfflineExportLimits", "OfflineExportValidateInput"]
