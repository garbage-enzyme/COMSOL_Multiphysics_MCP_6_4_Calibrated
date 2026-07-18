"""Solver-free binding of validation-matrix points to field requests."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .field_bundle import normalize_field_evidence_request


MATRIX_FIELD_COLLECTOR = "wave_optics_field_evidence"
_WAVELENGTH_TO_METERS = {
    "m": 1.0,
    "mm": 1e-3,
    "um": 1e-6,
    "µm": 1e-6,
    "nm": 1e-9,
}


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise ValueError(f"{label} must be an object with string keys")
    return dict(value)


def normalize_validation_matrix_field_inputs(value: object) -> dict[str, Any]:
    """Validate one single-view matrix field template before client startup."""
    raw = _mapping(value, "matrix field collector inputs")
    fields = {
        "request_id",
        "source_artifact_id",
        "expressions",
        "view",
        "slice",
        "coordinate_bounds",
        "grid",
        "render",
        "limits",
    }
    if set(raw) != fields:
        raise ValueError("matrix field collector inputs have missing or unsupported fields")
    view = _mapping(raw["view"], "matrix field collector view")
    view_fields = {
        "view_id",
        "component_tag",
        "dataset_name",
        "dataset_tag",
        "solution_tag",
        "outputs",
    }
    if set(view) != view_fields:
        raise ValueError("matrix field collector view has missing or unsupported fields")
    dummy = normalize_field_evidence_request(
        {
            "request_id": raw["request_id"],
            "configuration_sha256": "0" * 64,
            "expressions": raw["expressions"],
            "views": [
                {
                    "view_id": view["view_id"],
                    "wavelength_m": 1.0,
                    "source": {
                        "kind": "validation_matrix_point",
                        "source_model_sha256": "1" * 64,
                        "job_id": "job-template",
                        "point_id": "point-template",
                        "point_fingerprint": "2" * 64,
                        "artifact_id": raw["source_artifact_id"],
                        "component_tag": view["component_tag"],
                        "dataset_name": view["dataset_name"],
                        "dataset_tag": view["dataset_tag"],
                        "solution_tag": view["solution_tag"],
                    },
                    "outputs": view["outputs"],
                }
            ],
            "slice": raw["slice"],
            "coordinate_bounds": raw["coordinate_bounds"],
            "grid": raw["grid"],
            "render": raw["render"],
            "limits": raw["limits"],
        }
    )
    if dummy["render"]["png"]:
        raise ValueError("validation-matrix field collection does not render PNGs")
    normalized_view = dummy["views"][0]
    return {
        "request_id": dummy["request_id"],
        "source_artifact_id": normalized_view["source"]["artifact_id"],
        "expressions": dummy["expressions"],
        "view": {
            "view_id": normalized_view["view_id"],
            "component_tag": normalized_view["source"]["component_tag"],
            "dataset_name": normalized_view["source"]["dataset_name"],
            "dataset_tag": normalized_view["source"]["dataset_tag"],
            "solution_tag": normalized_view["source"]["solution_tag"],
            "outputs": {
                key: item
                for key, item in normalized_view["outputs"].items()
                if item is not None
            },
        },
        "slice": dummy["slice"],
        "coordinate_bounds": dummy["coordinate_bounds"],
        "grid": dummy["grid"],
        "render": dummy["render"],
        "limits": dummy["limits"],
    }


def bind_validation_matrix_field_request(
    inputs: object,
    *,
    job_id: str,
    point: Mapping[str, Any],
    source_model_sha256: str,
) -> dict[str, Any]:
    """Bind caller extraction settings to immutable job and point identities."""
    template = normalize_validation_matrix_field_inputs(inputs)
    collectors = point.get("collectors")
    artifacts = point.get("expected_artifact_ids")
    if not isinstance(collectors, list) or not isinstance(artifacts, list):
        raise ValueError("matrix point collector identities are unavailable")
    field_indices = [
        index
        for index, collector in enumerate(collectors)
        if isinstance(collector, Mapping) and collector.get("name") == MATRIX_FIELD_COLLECTOR
    ]
    if len(field_indices) != 1:
        raise ValueError("matrix point must declare exactly one field collector")
    try:
        source_index = artifacts.index(template["source_artifact_id"])
    except ValueError as exc:
        raise ValueError("field source_artifact_id is not declared by the matrix point") from exc
    field_index = field_indices[0]
    if source_index >= field_index:
        raise ValueError("field source artifact must precede the field collector")
    source_collector = collectors[source_index]
    if source_collector.get("name") != "wave_optics_point_audit":
        raise ValueError("field source artifact must belong to wave_optics_point_audit")
    wavelength = point.get("wavelength")
    if not isinstance(wavelength, Mapping):
        raise ValueError("matrix point wavelength identity is unavailable")
    unit = wavelength.get("unit")
    if unit not in _WAVELENGTH_TO_METERS:
        raise ValueError("matrix point wavelength unit cannot be converted to meters")
    wavelength_m = float(wavelength["value"]) * _WAVELENGTH_TO_METERS[str(unit)]
    view = template["view"]
    request = normalize_field_evidence_request(
        {
            "request_id": template["request_id"],
            "configuration_sha256": point["configuration_sha256"],
            "expressions": template["expressions"],
            "views": [
                {
                    "view_id": view["view_id"],
                    "wavelength_m": wavelength_m,
                    "source": {
                        "kind": "validation_matrix_point",
                        "source_model_sha256": source_model_sha256,
                        "job_id": job_id,
                        "point_id": point["point_id"],
                        "point_fingerprint": point["point_fingerprint"],
                        "artifact_id": template["source_artifact_id"],
                        "component_tag": view["component_tag"],
                        "dataset_name": view["dataset_name"],
                        "dataset_tag": view["dataset_tag"],
                        "solution_tag": view["solution_tag"],
                    },
                    "outputs": view["outputs"],
                }
            ],
            "slice": template["slice"],
            "coordinate_bounds": template["coordinate_bounds"],
            "grid": template["grid"],
            "render": template["render"],
            "limits": template["limits"],
        }
    )
    return deepcopy(request)


__all__ = [
    "MATRIX_FIELD_COLLECTOR",
    "bind_validation_matrix_field_request",
    "normalize_validation_matrix_field_inputs",
]
