"""Typed solver-free input contracts for offline MPH archive inspection."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class MphInspectionLimits(_ClosedModel):
    """Caller-overridable bounded reader limits within fixed hard caps."""

    max_archive_bytes: Annotated[int, Field(ge=1, le=68_719_476_736)] = 4_294_967_296
    max_entries: Annotated[int, Field(ge=1, le=65_536)] = 4_096
    max_entry_uncompressed_bytes: Annotated[int, Field(ge=1, le=8_589_934_592)] = 536_870_912
    max_total_uncompressed_bytes: Annotated[int, Field(ge=1, le=17_179_869_184)] = 2_147_483_648
    max_compression_ratio: Annotated[int, Field(ge=1, le=10_000)] = 500
    max_parameters: Annotated[int, Field(ge=1, le=2_048)] = 512
    max_tag_entries: Annotated[int, Field(ge=1, le=1_024)] = 256


class MphInspectionInput(_ClosedModel):
    """One bounded offline `.mph` inspection request."""

    file_path: Annotated[str, Field(min_length=1, max_length=4096)]
    limits: MphInspectionLimits | None = None


__all__ = ["MphInspectionInput", "MphInspectionLimits"]
