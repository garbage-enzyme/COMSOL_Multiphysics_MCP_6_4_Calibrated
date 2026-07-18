"""Read-only adapter from an existing MPh dataset to field-evidence artifacts."""

from __future__ import annotations

from typing import Any

from .field_bundle import validate_field_evidence_request
from .field_pipeline import build_field_evidence_from_samples


def _tags(collection: Any, label: str) -> list[str]:
    try:
        return [str(tag) for tag in list(collection.tags())]
    except Exception as exc:
        raise ValueError(f"{label} tags are unavailable") from exc


def _real_vector(value: Any, label: str) -> Any:
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - field tools require NumPy
        raise RuntimeError("NumPy is required to read field datasets") from exc
    array = np.asarray(value).reshape(-1)
    if array.size == 0 or array.dtype.kind not in "fciu":
        raise ValueError(f"{label} must be a nonempty numeric array")
    if np.iscomplexobj(array):
        if not np.all(array.imag == 0):
            raise ValueError(
                f"{label} contains complex values; request an explicit real scalar expression"
            )
        array = array.real
    array = np.asarray(array, dtype=np.float64)
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{label} contains nonfinite values")
    return array


def _collect_dataset_field_evidence(
    *,
    model: Any,
    request: object,
    view_id: str,
    artifact_root: object,
    png_path: object | None = None,
    source_kind: str,
) -> dict[str, Any]:
    """Evaluate one exact solution dataset without solving or mutating it."""
    request_value = validate_field_evidence_request(request)
    matches = [view for view in request_value["views"] if view["view_id"] == view_id]
    if len(matches) != 1:
        raise ValueError("view_id must identify exactly one requested view")
    view = matches[0]
    source = view["source"]
    if source["kind"] != source_kind:
        if source_kind == "existing_dataset":
            raise ValueError(
                "existing-dataset adapter cannot read a validation-matrix source"
            )
        raise ValueError(f"dataset adapter requires source kind {source_kind}")
    if model is None or not callable(getattr(model, "evaluate", None)):
        raise ValueError("model must provide MPh evaluate()")
    java_model = getattr(model, "java", None)
    if java_model is None:
        raise ValueError("model.java clientapi readback is required")

    try:
        component_collection = java_model.component()
    except Exception as exc:
        raise ValueError("component collection is unavailable") from exc
    component_tags = _tags(component_collection, "component")
    if source["component_tag"] not in component_tags:
        raise ValueError("declared component_tag is not present in the model")
    try:
        component_collection.get(source["component_tag"])
    except Exception as exc:
        raise ValueError("declared component_tag cannot be read back") from exc

    try:
        dataset_collection = java_model.result().dataset()
    except Exception as exc:
        raise ValueError("result dataset collection is unavailable") from exc
    dataset_tags = _tags(dataset_collection, "dataset")
    if source["dataset_tag"] not in dataset_tags:
        raise ValueError("declared dataset_tag is not present in the model")
    try:
        dataset = dataset_collection.get(source["dataset_tag"])
        solution_readback = str(dataset.getString("solution"))
    except Exception as exc:
        raise ValueError("dataset solution readback is unavailable") from exc
    if solution_readback != source["solution_tag"]:
        raise ValueError("dataset solution readback does not match the request")

    expressions = [item["expression"] for item in request_value["expressions"]]
    evaluation_expressions = [*expressions, "x", "y", "z"]
    try:
        evaluated = model.evaluate(
            evaluation_expressions,
            dataset=source["dataset_name"],
            inner=(
                [source["solution_number"]]
                if source.get("solution_number") is not None
                else None
            ),
        )
    except Exception as exc:
        raise RuntimeError(f"existing dataset field evaluation failed: {exc}") from exc
    if not isinstance(evaluated, (list, tuple)) or len(evaluated) != len(
        evaluation_expressions
    ):
        raise ValueError("field evaluation did not preserve expression order and count")
    vectors = [
        _real_vector(value, f"evaluation[{expression}]")
        for expression, value in zip(evaluation_expressions, evaluated)
    ]
    size = int(vectors[0].size)
    if any(vector.size != size for vector in vectors):
        raise ValueError("field evaluation returned incompatible array lengths")

    result = build_field_evidence_from_samples(
        request=request_value,
        view_id=view_id,
        artifact_root=artifact_root,
        coordinates={
            "x": vectors[-3],
            "y": vectors[-2],
            "z": vectors[-1],
        },
        quantities={
            expression["name"]: vectors[index]
            for index, expression in enumerate(request_value["expressions"])
        },
        png_path=png_path,
    )
    result["dataset_identity"] = {
        "source_kind": source["kind"],
        "component_tag": source["component_tag"],
        "dataset_name": source["dataset_name"],
        "dataset_tag": source["dataset_tag"],
        "solution_tag": source["solution_tag"],
        "solution_number": source.get("solution_number"),
        "source_fingerprint": source["source_fingerprint"],
        "readback_state": "verified",
    }
    if source["kind"] == "validation_matrix_point":
        result["dataset_identity"].update(
            {
                "source_model_sha256": source["source_model_sha256"],
                "job_id": source["job_id"],
                "point_id": source["point_id"],
                "point_fingerprint": source["point_fingerprint"],
                "source_artifact_id": source["artifact_id"],
            }
        )
    result["model_mutated"] = False
    result["study_run"] = False
    return result


def collect_existing_dataset_field_evidence(**kwargs: Any) -> dict[str, Any]:
    """Evaluate one caller-declared existing dataset."""
    return _collect_dataset_field_evidence(source_kind="existing_dataset", **kwargs)


def collect_validation_matrix_field_evidence(**kwargs: Any) -> dict[str, Any]:
    """Evaluate the dataset left by one exact validation-matrix point."""
    return _collect_dataset_field_evidence(
        source_kind="validation_matrix_point", **kwargs
    )


__all__ = [
    "collect_existing_dataset_field_evidence",
    "collect_validation_matrix_field_evidence",
]
