from __future__ import annotations

import builtins

import pytest

from src.evidence.field_bundle import (
    MAX_FIELD_ARTIFACT_BYTES,
    MAX_GRID_FIELD_POINTS,
    MAX_INLINE_FIELD_SAMPLES,
    MAX_RAW_FIELD_POINTS,
    normalize_field_evidence_request,
    validate_field_evidence_request,
)


def _source(kind: str = "existing_dataset", suffix: str = "on") -> dict:
    if kind == "existing_dataset":
        return {
            "kind": kind,
            "source_model_sha256": "d" * 64,
            "component_tag": "comp1",
            "dataset_name": f"Study 1//Solution {suffix}",
            "dataset_tag": f"dset_{suffix}",
            "solution_tag": f"sol_{suffix}",
            "solution_number": 1,
        }
    return {
        "kind": "validation_matrix_point",
        "source_model_sha256": "d" * 64,
        "job_id": "job-123",
        "point_id": suffix,
        "point_fingerprint": ("a" if suffix == "on" else "b") * 64,
        "artifact_id": f"audit-{suffix}",
        "component_tag": "comp1",
        "dataset_name": f"Study 1//Solution {suffix}",
        "dataset_tag": f"dset_{suffix}",
        "solution_tag": f"sol_{suffix}",
    }


def _view(view_id: str = "on", *, source_kind: str = "existing_dataset", png: bool = True) -> dict:
    outputs = {
        "array_artifact_id": f"field-{view_id}-npz",
        "manifest_artifact_id": f"field-{view_id}-manifest",
    }
    if png:
        outputs["png_artifact_id"] = f"field-{view_id}-png"
    return {
        "view_id": view_id,
        "wavelength_m": 5.292e-6 if view_id == "on" else 5.25e-6,
        "source": _source(source_kind, view_id),
        "outputs": outputs,
    }


def _request(*, paired: bool = True, png: bool = True) -> dict:
    return {
        "request_id": "field-request-1",
        "configuration_sha256": "c" * 64,
        "expressions": [
            {"name": "electric_norm", "expression": "ewfd.normE", "unit": "V/m"},
            {"name": "magnetic_norm", "expression": "ewfd.normH", "unit": "A/m"},
        ],
        "views": [
            _view("on", source_kind="validation_matrix_point", png=png),
            *([_view("off", png=png)] if paired else []),
        ],
        "slice": {"axis": "z", "value": 0.5, "tolerance": 0.01, "unit": "um"},
        "coordinate_bounds": {
            "x": [-1.0, 1.0],
            "y": [-1.5, 1.5],
            "z": [0.0, 1.0],
            "unit": "um",
        },
        "grid": {"shape": [128, 96], "interpolation": "linear"},
        "render": {
            "png": png,
            "color_scale": "linear",
            "shared_color_limits": paired,
        },
        "limits": {
            "max_raw_points": 100_000,
            "max_grid_points": 20_000,
            "max_artifact_bytes": 32 * 1024 * 1024,
            "max_inline_samples": 8,
        },
    }


def test_normalization_is_solver_free_deterministic_and_binds_sources(monkeypatch):
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in {"mph", "numpy"} or name.startswith(("mph.", "numpy.")):
            raise AssertionError("field request normalization must remain solver-free")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    first = normalize_field_evidence_request(_request())
    second = normalize_field_evidence_request(_request())

    assert first == second
    assert first["schema_name"] == "comsol_mcp.field_evidence_request"
    assert first["grid_point_count"] == 128 * 96
    assert first["visual_review_state"] == "visual_review_required"
    assert len(first["request_fingerprint"]) == 64
    assert first["views"][0]["source"]["kind"] == "validation_matrix_point"
    assert first["views"][1]["source"]["kind"] == "existing_dataset"
    assert first["views"][0]["source"]["source_fingerprint"] != first["views"][1]["source"]["source_fingerprint"]


def test_source_or_extraction_changes_change_request_identity():
    first = normalize_field_evidence_request(_request())
    changed = _request()
    changed["slice"]["value"] = 0.6
    second = normalize_field_evidence_request(changed)
    changed_source = _request()
    changed_source["views"][0]["source"]["dataset_tag"] = "dset_changed"
    third = normalize_field_evidence_request(changed_source)

    assert first["request_fingerprint"] != second["request_fingerprint"]
    assert first["views"][0]["source"]["source_fingerprint"] != third["views"][0]["source"]["source_fingerprint"]


def test_single_view_without_png_is_supported_and_has_no_png_artifact():
    result = normalize_field_evidence_request(_request(paired=False, png=False))

    assert len(result["views"]) == 1
    assert result["render"] == {
        "png": False,
        "color_scale": "linear",
        "shared_color_limits": False,
    }
    assert result["views"][0]["outputs"]["png_artifact_id"] is None


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("max_raw_points", MAX_RAW_FIELD_POINTS + 1, "max_raw_points"),
        ("max_grid_points", MAX_GRID_FIELD_POINTS + 1, "max_grid_points"),
        ("max_artifact_bytes", MAX_FIELD_ARTIFACT_BYTES + 1, "max_artifact_bytes"),
        ("max_inline_samples", MAX_INLINE_FIELD_SAMPLES + 1, "max_inline_samples"),
    ],
)
def test_hard_limits_fail_closed(field, value, message):
    request = _request()
    request["limits"][field] = value

    with pytest.raises(ValueError, match=message):
        normalize_field_evidence_request(request)


def test_grid_product_must_fit_caller_declared_limit():
    request = _request()
    request["limits"]["max_grid_points"] = 1_000

    with pytest.raises(ValueError, match="caller-declared max_grid_points"):
        normalize_field_evidence_request(request)


def test_paired_png_requires_shared_limits_and_single_view_rejects_them():
    paired = _request()
    paired["render"]["shared_color_limits"] = False
    single = _request(paired=False)
    single["render"]["shared_color_limits"] = True

    with pytest.raises(ValueError, match="paired PNG views"):
        normalize_field_evidence_request(paired)
    with pytest.raises(ValueError, match="requires exactly two views"):
        normalize_field_evidence_request(single)


def test_artifact_ids_are_portable_unique_and_match_png_policy():
    unsafe = _request()
    unsafe["views"][0]["outputs"]["array_artifact_id"] = "C:\\private\\field.npz"
    duplicate = _request()
    duplicate["views"][1]["outputs"]["array_artifact_id"] = "field-on-npz"
    missing_png = _request()
    missing_png["views"][0]["outputs"].pop("png_artifact_id")

    with pytest.raises(ValueError, match="bounded portable identifier"):
        normalize_field_evidence_request(unsafe)
    with pytest.raises(ValueError, match="unique across all views"):
        normalize_field_evidence_request(duplicate)
    with pytest.raises(ValueError, match="present exactly when PNG rendering"):
        normalize_field_evidence_request(missing_png)


def test_slice_bounds_units_and_unknown_fields_fail_closed():
    outside = _request()
    outside["slice"]["value"] = 2.0
    mixed_units = _request()
    mixed_units["slice"]["unit"] = "nm"
    unknown = _request()
    unknown["python_callback"] = "arbitrary"

    with pytest.raises(ValueError, match="inside the matching coordinate bound"):
        normalize_field_evidence_request(outside)
    with pytest.raises(ValueError, match="same unit"):
        normalize_field_evidence_request(mixed_units)
    with pytest.raises(ValueError, match="unsupported fields"):
        normalize_field_evidence_request(unknown)


def test_nonfinite_values_duplicate_expressions_and_duplicate_sources_are_rejected():
    nonfinite = _request()
    nonfinite["views"][0]["wavelength_m"] = float("nan")
    duplicate_expression = _request()
    duplicate_expression["expressions"][1]["name"] = "electric_norm"
    duplicate_source = _request()
    duplicate_source["views"][1]["source"] = duplicate_source["views"][0]["source"]
    duplicate_source["views"][1]["wavelength_m"] = duplicate_source["views"][0]["wavelength_m"]

    with pytest.raises(ValueError, match="positive and finite"):
        normalize_field_evidence_request(nonfinite)
    with pytest.raises(ValueError, match="unique names"):
        normalize_field_evidence_request(duplicate_expression)
    with pytest.raises(ValueError, match="unique exact source identities"):
        normalize_field_evidence_request(duplicate_source)


def test_normalized_request_survives_json_transport_and_detects_tampering():
    import json

    normalized = normalize_field_evidence_request(_request())
    transported = json.loads(json.dumps(normalized))

    assert validate_field_evidence_request(transported) == normalized

    transported["grid_point_count"] += 1
    with pytest.raises(ValueError, match="not canonical or was modified"):
        validate_field_evidence_request(transported)


def test_normalized_request_rejects_unknown_fields_and_changed_generated_identity():
    unknown = normalize_field_evidence_request(_request())
    unknown["runtime_callback"] = "arbitrary"
    changed_source = normalize_field_evidence_request(_request())
    changed_source["views"][0]["source"]["source_fingerprint"] = "0" * 64
    changed_request = normalize_field_evidence_request(_request())
    changed_request["request_fingerprint"] = "0" * 64

    with pytest.raises(ValueError, match="unsupported fields"):
        validate_field_evidence_request(unknown)
    with pytest.raises(ValueError, match="not canonical or was modified"):
        validate_field_evidence_request(changed_source)
    with pytest.raises(ValueError, match="not canonical or was modified"):
        validate_field_evidence_request(changed_request)
