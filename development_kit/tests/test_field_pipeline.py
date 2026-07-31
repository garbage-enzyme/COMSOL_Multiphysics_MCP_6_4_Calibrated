from __future__ import annotations

import json

import numpy as np
from src.evidence.field_bundle import normalize_field_evidence_request
from src.evidence.field_manifest import validate_field_evidence_manifest
from src.evidence.field_pipeline import build_field_evidence_from_samples

from development_kit.tests.test_field_bundle import _request


def _pipeline_request(*, method: str = "linear") -> dict:
    raw = _request(paired=False, png=False)
    raw["grid"]["shape"] = [17, 19]
    raw["grid"]["interpolation"] = method
    raw["limits"]["max_grid_points"] = 500
    return normalize_field_evidence_request(raw)


def test_pipeline_closes_raw_to_manifest_provenance_with_duplicate_collapse(tmp_path):
    request = _pipeline_request()
    x = np.array([-1.0, 1.0, -1.0, 1.0, -1.0, 1.2])
    y = np.array([-1.5, -1.5, 1.5, 1.5, -1.5, 0.0])
    z = np.full(x.shape, 0.5)
    result = build_field_evidence_from_samples(
        request=request,
        view_id="on",
        artifact_root=tmp_path,
        coordinates={"x": x, "y": y, "z": z},
        quantities={
            "electric_norm": x + 2.0 * y,
            "magnetic_norm": 3.0 * x - y,
        },
    )
    manifest_path = tmp_path / result["manifest_artifact"]["relative_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert result["raw_point_count"] == 6
    assert result["selected_point_count"] == 5
    assert result["rejected_point_count"] == 1
    assert result["unique_point_count"] == 4
    assert result["collapsed_duplicate_point_count"] == 1
    assert manifest["unique_point_count"] == 4
    assert manifest["collapsed_duplicate_point_count"] == 1
    assert validate_field_evidence_manifest(manifest, request=request) == manifest


def test_pipeline_nearest_path_writes_complete_grid_without_inline_arrays(tmp_path):
    request = _pipeline_request(method="nearest")
    x = np.array([-1.0, 1.0, -1.0])
    y = np.array([-1.5, -1.5, 1.5])
    z = np.full(x.shape, 0.5)
    result = build_field_evidence_from_samples(
        request=request,
        view_id="on",
        artifact_root=tmp_path,
        coordinates={"x": x, "y": y, "z": z},
        quantities={
            "electric_norm": x**2 + y**2,
            "magnetic_norm": np.sqrt(x**2 + y**2 + 1.0),
        },
    )

    assert result["interpolation"] == "nearest"
    assert result["missing_grid_point_count"] == 0
    assert "quantity_grids" not in result and "coordinates" not in result
    array_path = tmp_path / result["array_artifact"]["relative_path"]
    with np.load(array_path, allow_pickle=False) as archive:
        assert set(archive.files) == {
            "coordinate_x",
            "coordinate_y",
            "quantity_electric_norm",
            "quantity_magnetic_norm",
        }
        for name in ("quantity_electric_norm", "quantity_magnetic_norm"):
            assert archive[name].shape == tuple(request["grid"]["shape"])
            assert np.isfinite(archive[name]).all()
