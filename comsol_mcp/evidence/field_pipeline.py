"""Solver-free raw-sample to durable field-evidence artifact pipeline."""

from __future__ import annotations

from typing import Any

from .field_artifacts import write_field_evidence_artifacts
from .field_interpolation import interpolate_field_slice
from .field_sampling import select_field_slice_samples


def build_field_evidence_from_samples(
    *,
    request: object,
    view_id: str,
    artifact_root: object,
    coordinates: object,
    quantities: object,
    png_path: object | None = None,
) -> dict[str, Any]:
    """Select, deduplicate, interpolate, and durably serialize one field view."""
    selection = select_field_slice_samples(
        request=request,
        view_id=view_id,
        coordinates=coordinates,
        quantities=quantities,
    )
    interpolation = interpolate_field_slice(request=request, selection=selection)
    result = write_field_evidence_artifacts(
        request=request,
        view_id=view_id,
        artifact_root=artifact_root,
        axis_coordinates=interpolation["axis_coordinates"],
        quantity_grids=interpolation["quantity_grids"],
        raw_point_count=selection["raw_point_count"],
        selected_point_count=selection["selected_point_count"],
        unique_point_count=interpolation["unique_point_count"],
        collapsed_duplicate_point_count=interpolation[
            "collapsed_duplicate_point_count"
        ],
        png_path=png_path,
    )
    result["rejected_point_count"] = selection["rejected_point_count"]
    result["interpolation"] = interpolation["interpolation"]
    return result


__all__ = ["build_field_evidence_from_samples"]
