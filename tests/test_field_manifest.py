from __future__ import annotations

from copy import deepcopy
import json

import pytest

from src.evidence.field_bundle import normalize_field_evidence_request
from src.evidence.field_manifest import (
    build_field_evidence_manifest,
    validate_field_evidence_manifest,
)
from tests.test_field_bundle import _request


def _artifact(artifact_id: str, path: str, media_type: str, size: int = 1024) -> dict:
    return {
        "artifact_id": artifact_id,
        "relative_path": path,
        "media_type": media_type,
        "sha256": "d" * 64,
        "byte_count": size,
    }


def _summaries(grid_points: int = 128 * 96, missing: int = 0) -> list[dict]:
    finite = grid_points - missing
    return [
        {
            "name": "electric_norm", "unit": "V/m", "minimum": 0.1,
            "maximum": 4.2, "rms": 1.3, "finite_count": finite,
            "missing_count": missing,
        },
        {
            "name": "magnetic_norm", "unit": "A/m", "minimum": 0.2,
            "maximum": 2.1, "rms": 0.9, "finite_count": finite,
            "missing_count": missing,
        },
    ]


def _manifest(*, missing: int = 0) -> tuple[dict, dict]:
    request = normalize_field_evidence_request(_request())
    manifest = build_field_evidence_manifest(
        request=request,
        view_id="on",
        raw_point_count=50_000,
        selected_point_count=12_000,
        covered_grid_point_count=request["grid_point_count"] - missing,
        missing_grid_point_count=missing,
        coordinate_ranges={
            "x": [-0.9, 0.9], "y": [-1.4, 1.4], "z": [0.5, 0.5], "unit": "um",
        },
        quantity_summaries=_summaries(missing=missing),
        array_artifact=_artifact("field-on-npz", "on/fields.npz", "application/x-npz"),
        png_artifact=_artifact("field-on-png", "on/fields.png", "image/png"),
    )
    return request, manifest


def test_manifest_binds_request_source_counts_summaries_and_artifacts():
    request, manifest = _manifest()

    assert manifest["request_fingerprint"] == request["request_fingerprint"]
    assert manifest["view_fingerprint"] == request["views"][0]["view_fingerprint"]
    assert manifest["source"] == request["views"][0]["source"]
    assert manifest["coverage_fraction"] == 1.0
    assert manifest["measurement_status"] == "measurement_complete"
    assert manifest["visual_review_state"] == "visual_review_required"
    assert manifest["semantic_mode_label"] == "not_assigned"
    assert "arrays" not in manifest and "values" not in manifest
    assert len(manifest["manifest_sha256"]) == 64


def test_manifest_survives_json_transport_and_detects_tampering():
    request, manifest = _manifest()
    transported = json.loads(json.dumps(manifest))

    assert validate_field_evidence_manifest(transported, request=request) == manifest

    transported["quantity_summaries"][0]["maximum"] = 99.0
    with pytest.raises(ValueError, match="hash does not match"):
        validate_field_evidence_manifest(transported, request=request)


def test_interpolation_gaps_are_explicit_partial_evidence():
    _, manifest = _manifest(missing=12)

    assert manifest["missing_grid_point_count"] == 12
    assert manifest["measurement_status"] == "partial"
    assert manifest["quantity_summaries"][0]["missing_count"] == 12


def _kwargs() -> tuple[dict, dict, dict]:
    request, manifest = _manifest()
    kwargs = {
        "request": request,
        "view_id": "on",
        "raw_point_count": manifest["raw_point_count"],
        "selected_point_count": manifest["selected_point_count"],
        "covered_grid_point_count": manifest["covered_grid_point_count"],
        "missing_grid_point_count": 0,
        "coordinate_ranges": manifest["coordinate_ranges"],
        "quantity_summaries": manifest["quantity_summaries"],
        "array_artifact": deepcopy(manifest["artifacts"]["array"]),
        "png_artifact": deepcopy(manifest["artifacts"]["png"]),
    }
    return request, manifest, kwargs


def test_counts_must_close_and_fit_caller_limits():
    _, _, kwargs = _kwargs()

    with pytest.raises(ValueError, match="raw_point_count exceeds"):
        build_field_evidence_manifest(**dict(kwargs, raw_point_count=100_001))
    with pytest.raises(ValueError, match="must not exceed raw_point_count"):
        build_field_evidence_manifest(
            **dict(kwargs, raw_point_count=10, selected_point_count=11)
        )
    with pytest.raises(ValueError, match="must equal the exact grid size"):
        build_field_evidence_manifest(**dict(kwargs, covered_grid_point_count=100))


def test_artifacts_must_match_request_be_relative_and_fit_total_size_limit():
    request, _, kwargs = _kwargs()
    wrong_id = deepcopy(kwargs)
    wrong_id["array_artifact"]["artifact_id"] = "other-array"
    escaping = deepcopy(kwargs)
    escaping["array_artifact"]["relative_path"] = "../private/fields.npz"
    oversized = deepcopy(kwargs)
    oversized["array_artifact"]["byte_count"] = request["limits"]["max_artifact_bytes"]

    with pytest.raises(ValueError, match="does not match the field request"):
        build_field_evidence_manifest(**wrong_id)
    with pytest.raises(ValueError, match="relative and traversal-free"):
        build_field_evidence_manifest(**escaping)
    with pytest.raises(ValueError, match="caller-declared byte limit"):
        build_field_evidence_manifest(**oversized)


def test_summary_names_units_and_counts_must_match_expressions_and_coverage():
    _, _, kwargs = _kwargs()
    wrong_unit = _summaries()
    wrong_unit[0]["unit"] = "A/m"
    bad_count = _summaries()
    bad_count[0]["finite_count"] -= 1
    duplicate = _summaries()
    duplicate[1]["name"] = "electric_norm"
    missing_mismatch = _summaries()
    missing_mismatch[0]["finite_count"] -= 1
    missing_mismatch[0]["missing_count"] = 1

    for summaries, message in (
        (wrong_unit, "unit does not match"),
        (bad_count, "counts must equal"),
        (duplicate, "duplicate or unrequested"),
        (missing_mismatch, "must match the manifest coverage"),
    ):
        with pytest.raises(ValueError, match=message):
            build_field_evidence_manifest(
                **dict(kwargs, quantity_summaries=summaries)
            )


def test_coordinate_ranges_must_stay_inside_requested_bounds():
    _, _, kwargs = _kwargs()
    ranges = deepcopy(kwargs["coordinate_ranges"])
    ranges["x"] = [-2.0, 0.9]

    with pytest.raises(ValueError, match="escapes the requested bounds"):
        build_field_evidence_manifest(**dict(kwargs, coordinate_ranges=ranges))


def test_manifest_rejects_unknown_fields_even_with_recomputed_hash():
    request, manifest = _manifest()
    manifest["semantic_claim"] = "SPP"

    with pytest.raises(ValueError, match="unsupported fields"):
        validate_field_evidence_manifest(manifest, request=request)
