from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import numpy as np
import pytest

from src.evidence.field_artifacts import write_field_evidence_artifacts
from src.evidence.field_bundle import normalize_field_evidence_request
from src.evidence.field_manifest import validate_field_evidence_manifest
from tests.test_field_bundle import _request


def _inputs(tmp_path, *, missing: bool = False):
    request = normalize_field_evidence_request(_request(paired=False, png=False))
    rows, columns = request["grid"]["shape"]
    x = np.linspace(-0.9, 0.9, columns)
    y = np.linspace(-1.4, 1.4, rows)
    xx, yy = np.meshgrid(x, y)
    electric = xx**2 + yy**2
    magnetic = np.sqrt(electric + 1.0)
    if missing:
        electric[0, 0] = np.nan
        magnetic[0, 0] = np.nan
    return request, {
        "request": request,
        "view_id": "on",
        "artifact_root": tmp_path,
        "axis_coordinates": {"x": x, "y": y},
        "quantity_grids": {
            "electric_norm": electric,
            "magnetic_norm": magnetic,
        },
        "raw_point_count": 50_000,
        "selected_point_count": 12_000,
    }


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_writer_creates_compressed_arrays_and_hash_bound_manifest(tmp_path):
    request, kwargs = _inputs(tmp_path)
    result = write_field_evidence_artifacts(**kwargs)
    array_path = tmp_path / result["array_artifact"]["relative_path"]
    manifest_path = tmp_path / result["manifest_artifact"]["relative_path"]

    assert array_path.is_file() and manifest_path.is_file()
    assert result["array_artifact"]["sha256"] == _sha256(array_path)
    assert result["manifest_artifact"]["sha256"] == _sha256(manifest_path)
    assert result["missing_grid_point_count"] == 0
    assert result["visual_review_state"] == "visual_review_required"
    assert "arrays" not in result and "values" not in result

    with np.load(array_path, allow_pickle=False) as archive:
        assert set(archive.files) == {
            "coordinate_x", "coordinate_y", "quantity_electric_norm",
            "quantity_magnetic_norm",
        }
        assert archive["quantity_electric_norm"].shape == tuple(request["grid"]["shape"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert validate_field_evidence_manifest(manifest, request=request) == manifest


def test_writer_preserves_shared_missing_mask_as_partial_evidence(tmp_path):
    _, kwargs = _inputs(tmp_path, missing=True)
    result = write_field_evidence_artifacts(**kwargs)
    manifest_path = tmp_path / result["manifest_artifact"]["relative_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert result["missing_grid_point_count"] == 1
    assert manifest["measurement_status"] == "partial"
    assert all(item["missing_count"] == 1 for item in result["quantity_summaries"])


def test_writer_rejects_shape_infinity_and_mismatched_missing_masks(tmp_path):
    _, kwargs = _inputs(tmp_path)
    wrong_shape = deepcopy(kwargs)
    wrong_shape["quantity_grids"]["electric_norm"] = np.ones((2, 2))
    infinity = deepcopy(kwargs)
    infinity["quantity_grids"]["electric_norm"][0, 0] = np.inf
    mismatched = deepcopy(kwargs)
    mismatched["quantity_grids"]["electric_norm"][0, 0] = np.nan

    with pytest.raises(ValueError, match="must have shape"):
        write_field_evidence_artifacts(**wrong_shape)
    with pytest.raises(ValueError, match="without infinity"):
        write_field_evidence_artifacts(**infinity)
    with pytest.raises(ValueError, match="same missing-cell mask"):
        write_field_evidence_artifacts(**mismatched)


def test_writer_rejects_bad_coordinates_and_expression_set(tmp_path):
    _, kwargs = _inputs(tmp_path)
    wrong_length = deepcopy(kwargs)
    wrong_length["axis_coordinates"]["x"] = np.arange(3)
    nonmonotonic = deepcopy(kwargs)
    nonmonotonic["axis_coordinates"]["x"] = nonmonotonic["axis_coordinates"]["x"][::-1]
    escaping = deepcopy(kwargs)
    escaping["axis_coordinates"]["x"] = np.linspace(-2.0, 0.9, 96)
    missing_expression = deepcopy(kwargs)
    missing_expression["quantity_grids"].pop("magnetic_norm")

    for value, message in (
        (wrong_length, "must have length"),
        (nonmonotonic, "strictly increasing"),
        (escaping, "escapes the requested bounds"),
        (missing_expression, "exactly the requested expressions"),
    ):
        with pytest.raises(ValueError, match=message):
            write_field_evidence_artifacts(**value)


def test_writer_is_immutable_and_refuses_existing_view_artifacts(tmp_path):
    _, kwargs = _inputs(tmp_path)
    write_field_evidence_artifacts(**kwargs)

    with pytest.raises(FileExistsError, match="already exist"):
        write_field_evidence_artifacts(**kwargs)


def test_writer_cleans_owned_partial_files_when_manifest_build_fails(tmp_path):
    request, kwargs = _inputs(tmp_path)
    kwargs["raw_point_count"] = request["limits"]["max_raw_points"] + 1

    with pytest.raises(ValueError, match="raw_point_count exceeds"):
        write_field_evidence_artifacts(**kwargs)

    assert not list(tmp_path.rglob("field_arrays.npz"))
    assert not list(tmp_path.rglob("field_manifest.json"))
    assert not list(tmp_path.rglob("*.tmp"))


def test_writer_supports_unicode_artifact_root_for_portable_development(tmp_path):
    _, kwargs = _inputs(tmp_path)
    kwargs["artifact_root"] = tmp_path / "可复用证据"

    result = write_field_evidence_artifacts(**kwargs)

    assert (kwargs["artifact_root"] / result["array_artifact"]["relative_path"]).is_file()
