"""Typed solver-free input contracts for offline model identity."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from comsol_mcp.contracts.mph_inspection import MphInspectionLimits


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


_HEX64 = r"^[0-9a-fA-F]{64}$"


class ModelIdentityInput(_ClosedModel):
    """One bounded read-only model-identity request.

    ``file_path`` is always required and is inspected offline. The optional
    source and checkpoint paths are caller-declared confirmations; declaring
    one asks the builder to prove bytes and hashes instead of guessing.
    ``request_session_identity`` only ever consults passively readable
    in-process session state; it can never attach, start, or own COMSOL.
    """

    file_path: Annotated[str, Field(min_length=1, max_length=4096)]
    source_path: Annotated[str | None, Field(min_length=1, max_length=4096)] = None
    checkpoint_path: Annotated[str | None, Field(min_length=1, max_length=4096)] = None
    expected_file_sha256: Annotated[str | None, Field(pattern=_HEX64)] = None
    expected_source_sha256: Annotated[str | None, Field(pattern=_HEX64)] = None
    expected_checkpoint_sha256: Annotated[str | None, Field(pattern=_HEX64)] = None
    request_session_identity: bool = False
    limits: MphInspectionLimits | None = None


__all__ = ["ModelIdentityInput"]
