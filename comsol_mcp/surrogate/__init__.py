"""Solver-free surrogate dataset, split, and transform manifests."""

from __future__ import annotations

__all__ = [
    "IneligibleRow",
    "LeakageGroup",
    "SplitAssignment",
    "SurrogateDatasetManifest",
    "SurrogateSchemaManifest",
    "SurrogateTrainingTransforms",
    "build_dataset_manifest",
    "build_schema_manifest",
    "build_training_transforms",
    "canonical_manifest_sha256",
    "validate_dataset_manifest",
    "validate_group_disjoint_split",
    "validate_schema_manifest",
    "validate_training_transforms",
]

from comsol_mcp.surrogate.manifests import (
    IneligibleRow,
    LeakageGroup,
    SplitAssignment,
    SurrogateDatasetManifest,
    SurrogateSchemaManifest,
    SurrogateTrainingTransforms,
    build_dataset_manifest,
    build_schema_manifest,
    build_training_transforms,
    canonical_manifest_sha256,
    validate_dataset_manifest,
    validate_group_disjoint_split,
    validate_schema_manifest,
    validate_training_transforms,
)
