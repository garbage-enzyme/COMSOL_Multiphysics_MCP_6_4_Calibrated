"""Bounded public surrogate tools: validate, preview, inspect, and verify only.

These tools are solver-free.  They never start COMSOL, never import MPh or JPype,
never acquire a solver lease, and never train or evaluate a model.  Training and
evaluation stay inside durable jobs.

The tool surface is deliberately small and read-only in intent:

* ``surrogate_dataset_validate``   — validate a dataset file's shape and identity
* ``surrogate_training_preview``   — validate a configuration and show its write plan
* ``surrogate_model_inspect``      — inspect an export manifest/card/registry entry
* ``surrogate_model_verify``       — verify a document against declared expectations
* ``surrogate_prediction_validate``— validate predictions without calling them evidence

Every path argument is flat so contained-path enforcement applies to it.  Each
tool returns ``solver_started=False`` and ``filesystem_modified=False``.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from comsol_mcp.contracts.surrogate import (
    MAX_URI_LENGTH,
    SurrogateDatasetValidateInput,
    SurrogateModelInspectInput,
    SurrogateModelVerifyInput,
    SurrogatePredictionValidateInput,
    SurrogateReadLimits,
    SurrogateTrainingPreviewInput,
)
from comsol_mcp.evidence.surrogate_evidence import (
    SurrogateEvidenceError,
    inspect_surrogate_document,
    preview_training_configuration,
    resolve_dbmodel_source,
    validate_dataset_document,
    validate_prediction_document,
    verify_surrogate_document,
)
from comsol_mcp.utils.public_errors import public_error

logger = logging.getLogger(__name__)

SOLVER_FREE_FOOTER: dict[str, Any] = {
    "solver_started": False,
    "filesystem_modified": False,
}


def _rejection(exc: SurrogateEvidenceError | Exception, fallback: str) -> dict[str, Any]:
    """Build one bounded public refusal without serializing an exception.

    A typed evidence refusal keeps its reason code; anything unexpected is
    reported under a generic code so an internal message never leaks.
    """
    if isinstance(exc, SurrogateEvidenceError):
        logger.info("Surrogate tool refused: %s", exc.reason_code)
        return {**public_error(exc.reason_code, str(exc)), **SOLVER_FREE_FOOTER}
    logger.exception("Surrogate tool failed")
    return {**public_error(fallback, "The surrogate request was rejected."), **SOLVER_FREE_FOOTER}


def _required_source_path(value: str | None, field: str) -> str:
    """Return a source path that the contract guarantees, or refuse explicitly.

    The dataset contract already refuses a file/directory source without a path,
    so this is a backstop for the type checker rather than a second policy.  It
    raises instead of using ``assert`` so the guard survives ``python -O``, where
    an assertion would vanish and ``None`` would reach the path reader.
    """
    if value is None:
        raise SurrogateEvidenceError(
            "surrogate_source_requires_path",
            f"A file or directory source requires {field}.",
        )
    return value


def register_surrogate_tools(mcp: MCPServer) -> None:
    """Register the bounded read-only surrogate tools in every profile."""

    @mcp.tool()  # type: ignore[untyped-decorator]
    def surrogate_dataset_validate(
        dataset_path: Annotated[str | None, Field(max_length=4096)] = None,
        source_kind: str = "file",
        source_uri: Annotated[str | None, Field(max_length=MAX_URI_LENGTH)] = None,
        field_schema_path: Annotated[str | None, Field(max_length=4096)] = None,
        expected_row_count: Annotated[int | None, Field(ge=0, le=4096)] = None,
        expected_feature_names: list[str] | None = None,
        expected_target_names: list[str] | None = None,
        limits: SurrogateReadLimits | None = None,
    ) -> dict[str, Any]:
        """Validate one surrogate dataset's shape, names, and content identity.

        The dataset is read only far enough to bind its header, row count, and
        SHA-256. It is refused rather than clipped when it exceeds a bound, and a
        text header is never invented for a headerless file.

        ``source_kind='dbmodel'`` validates a frozen Model Manager URI as syntax
        and evidence only: nothing is read, connected, or authenticated, and the
        live identity is reported ``unavailable``.
        """
        try:
            request = SurrogateDatasetValidateInput(
                dataset_path=dataset_path,
                source_kind=source_kind,
                source_uri=source_uri,
                field_schema_path=field_schema_path,
                expected_row_count=expected_row_count,
                expected_feature_names=expected_feature_names,
                expected_target_names=expected_target_names,
                limits=limits,
            )
            bounds = request.limits or SurrogateReadLimits()
            if request.source_kind == "dbmodel":
                return resolve_dbmodel_source(
                    source_uri=request.source_uri or "",
                    source_path=request.dataset_path,
                )
            return validate_dataset_document(
                _required_source_path(request.dataset_path, "dataset_path"),
                max_bytes=bounds.max_document_bytes,
                max_rows=bounds.max_rows,
                max_columns=bounds.max_columns,
                expected_row_count=request.expected_row_count,
                expected_feature_names=request.expected_feature_names,
                expected_target_names=request.expected_target_names,
            )
        except (SurrogateEvidenceError, TypeError, ValueError, OSError) as exc:
            return _rejection(exc, "surrogate_dataset_rejected")

    @mcp.tool()  # type: ignore[untyped-decorator]
    def surrogate_training_preview(
        configuration: Annotated[dict[str, Any], Field(min_length=1)],
        dataset_path: Annotated[str | None, Field(max_length=4096)] = None,
        declared_fem_row_count: Annotated[int | None, Field(ge=0, le=4096)] = None,
        limits: SurrogateReadLimits | None = None,
    ) -> dict[str, Any]:
        """Validate a surrogate training configuration and show its exact write plan.

        No COMSOL node is created, no solver is started, and nothing is trained.
        The deferred writes that require a bound data source are reported
        explicitly rather than being silently omitted.
        """
        try:
            request = SurrogateTrainingPreviewInput(
                configuration=configuration,
                dataset_path=dataset_path,
                declared_fem_row_count=declared_fem_row_count,
                limits=limits,
            )
            bounds = request.limits or SurrogateReadLimits()
            return preview_training_configuration(
                request.configuration,
                dataset_path=request.dataset_path,
                max_bytes=bounds.max_document_bytes,
                max_rows=bounds.max_rows,
                max_columns=bounds.max_columns,
                declared_fem_row_count=request.declared_fem_row_count,
            )
        except (SurrogateEvidenceError, TypeError, ValueError, OSError) as exc:
            return _rejection(exc, "surrogate_preview_rejected")

    @mcp.tool()  # type: ignore[untyped-decorator]
    def surrogate_model_inspect(
        document_path: Annotated[str, Field(min_length=1, max_length=4096)],
        document_kind: str = "auto",
        limits: SurrogateReadLimits | None = None,
    ) -> dict[str, Any]:
        """Inspect one surrogate export manifest, model card, or registry entry.

        The document is validated against its own declared schema by re-deriving
        its canonical hash, so a tampered document is refused rather than
        summarized. An unsupported schema is refused instead of guessed at.
        """
        try:
            request = SurrogateModelInspectInput(
                document_path=document_path,
                document_kind=document_kind,
                limits=limits,
            )
            bounds = request.limits or SurrogateReadLimits()
            result = inspect_surrogate_document(
                request.document_path, max_bytes=bounds.max_document_bytes
            )
        except (SurrogateEvidenceError, TypeError, ValueError, OSError) as exc:
            return _rejection(exc, "surrogate_document_rejected")
        if (
            request.document_kind != "auto"
            and result["summary"]["document_kind"] != request.document_kind
        ):
            return {
                **public_error(
                    "surrogate_document_kind_mismatch",
                    "The document kind does not match the declared kind.",
                ),
                "declared_kind": request.document_kind,
                "observed_kind": result["summary"]["document_kind"],
                **SOLVER_FREE_FOOTER,
            }
        return result

    @mcp.tool()  # type: ignore[untyped-decorator]
    def surrogate_model_verify(
        document_path: Annotated[str, Field(min_length=1, max_length=4096)],
        expected_trained_chksum: Annotated[str | None, Field(max_length=64)] = None,
        expected_artifact_sha256: Annotated[str | None, Field(max_length=64)] = None,
        expected_architecture_sha256: Annotated[str | None, Field(max_length=64)] = None,
        expected_dataset_manifest_sha256: Annotated[str | None, Field(max_length=64)] = None,
        expected_split_manifest_sha256: Annotated[str | None, Field(max_length=64)] = None,
        require_consistent_export: bool = False,
        limits: SurrogateReadLimits | None = None,
    ) -> dict[str, Any]:
        """Verify a surrogate document against only the declared expectations.

        An undeclared expectation is reported as ``not_checked`` and is never
        reported as satisfied, and an expectation the document cannot supply is
        reported as ``unavailable`` rather than passing.
        """
        try:
            request = SurrogateModelVerifyInput(
                document_path=document_path,
                expected_trained_chksum=expected_trained_chksum,
                expected_artifact_sha256=expected_artifact_sha256,
                expected_architecture_sha256=expected_architecture_sha256,
                expected_dataset_manifest_sha256=expected_dataset_manifest_sha256,
                expected_split_manifest_sha256=expected_split_manifest_sha256,
                require_consistent_export=require_consistent_export,
                limits=limits,
            )
            bounds = request.limits or SurrogateReadLimits()
            return verify_surrogate_document(
                request.document_path,
                max_bytes=bounds.max_document_bytes,
                expected_trained_chksum=request.expected_trained_chksum,
                expected_artifact_sha256=request.expected_artifact_sha256,
                expected_architecture_sha256=request.expected_architecture_sha256,
                expected_dataset_manifest_sha256=request.expected_dataset_manifest_sha256,
                expected_split_manifest_sha256=request.expected_split_manifest_sha256,
                require_consistent_export=request.require_consistent_export,
            )
        except (SurrogateEvidenceError, TypeError, ValueError, OSError) as exc:
            return _rejection(exc, "surrogate_verification_rejected")

    @mcp.tool()  # type: ignore[untyped-decorator]
    def surrogate_prediction_validate(
        prediction_path: Annotated[str, Field(min_length=1, max_length=4096)],
        absolute_tolerance: Annotated[float, Field(ge=0.0)] = 0.0,
        relative_tolerance: Annotated[float, Field(ge=0.0)] = 0.0,
        ood_state: str | None = None,
        limits: SurrogateReadLimits | None = None,
    ) -> dict[str, Any]:
        """Validate a surrogate prediction row set without treating it as evidence.

        A prediction document carrying an FEM evidence field is refused, and the
        result always reports that the rows remain a prediction requiring a fresh
        FEM run.
        """
        try:
            request = SurrogatePredictionValidateInput(
                prediction_path=prediction_path,
                absolute_tolerance=absolute_tolerance,
                relative_tolerance=relative_tolerance,
                ood_state=ood_state,
                limits=limits,
            )
            bounds = request.limits or SurrogateReadLimits()
            return validate_prediction_document(
                request.prediction_path,
                max_bytes=bounds.max_document_bytes,
                max_rows=bounds.max_rows,
                max_columns=bounds.max_columns,
                absolute_tolerance=request.absolute_tolerance,
                relative_tolerance=request.relative_tolerance,
                ood_state=request.ood_state,
            )
        except (SurrogateEvidenceError, TypeError, ValueError, OSError) as exc:
            return _rejection(exc, "surrogate_prediction_rejected")


__all__ = ["SOLVER_FREE_FOOTER", "register_surrogate_tools"]
