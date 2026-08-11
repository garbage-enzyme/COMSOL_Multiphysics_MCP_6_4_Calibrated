"""Durable background-job primitives used by the MCP control plane."""

from .adjoint_optimization import (
    ADJOINT_MANIFEST_SCHEMA_NAME,
    ADJOINT_MANIFEST_SCHEMA_VERSION,
    ADJOINT_SUBMISSION_SCHEMA_NAME,
    ADJOINT_SUBMISSION_SCHEMA_VERSION,
    expand_adjoint_optimization_manifest,
    normalize_adjoint_optimization_submission,
)
from .adjoint_rows import (
    ADJOINT_ROW_SCHEMA_NAME,
    ADJOINT_ROW_SCHEMA_VERSION,
    append_adjoint_row,
    read_adjoint_rows,
)
from .manager import JobManager
from .robust_shape_optimization import (
    ROBUST_SHAPE_MANIFEST_SCHEMA_NAME,
    ROBUST_SHAPE_MANIFEST_SCHEMA_VERSION,
    ROBUST_SHAPE_SUBMISSION_SCHEMA_NAME,
    ROBUST_SHAPE_SUBMISSION_SCHEMA_VERSION,
    expand_robust_shape_manifest,
    normalize_robust_shape_submission,
)
from .store import JobStore

__all__ = [
    "ADJOINT_ROW_SCHEMA_NAME",
    "ADJOINT_ROW_SCHEMA_VERSION",
    "ADJOINT_MANIFEST_SCHEMA_NAME",
    "ADJOINT_MANIFEST_SCHEMA_VERSION",
    "ADJOINT_SUBMISSION_SCHEMA_NAME",
    "ADJOINT_SUBMISSION_SCHEMA_VERSION",
    "ROBUST_SHAPE_MANIFEST_SCHEMA_NAME",
    "ROBUST_SHAPE_MANIFEST_SCHEMA_VERSION",
    "ROBUST_SHAPE_SUBMISSION_SCHEMA_NAME",
    "ROBUST_SHAPE_SUBMISSION_SCHEMA_VERSION",
    "JobManager",
    "JobStore",
    "append_adjoint_row",
    "read_adjoint_rows",
    "expand_adjoint_optimization_manifest",
    "normalize_adjoint_optimization_submission",
    "expand_robust_shape_manifest",
    "normalize_robust_shape_submission",
]
