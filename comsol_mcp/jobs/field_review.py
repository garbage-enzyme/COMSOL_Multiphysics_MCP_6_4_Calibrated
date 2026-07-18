"""Solver-free assembly of paired validation-matrix field review artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any, Mapping

from comsol_mcp.evidence.field_matrix import bind_validation_matrix_field_request
from comsol_mcp.evidence.field_render import render_field_png_bundle

from .store import atomic_write_json
from .validation_rows import read_validation_rows


FIELD_REVIEW_BUNDLE_SCHEMA = "comsol_mcp.validation_matrix_field_review"
FIELD_REVIEW_BUNDLE_VERSION = "1.0.0"
MAX_WRAPPER_BYTES = 1024 * 1024
MAX_FIELD_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_FIELD_ARRAY_BYTES = 256 * 1024 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_PATH_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_WINDOWS_DEVICE_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fingerprint(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _portable_path_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and _PATH_IDENTIFIER.fullmatch(value) is not None
        and value.split(".", 1)[0].casefold() not in _WINDOWS_DEVICE_NAMES
    )


def _contained_file(root: Path, relative: object, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError(f"{label} relative path is unavailable")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes the durable job directory") from exc
    if not path.is_file():
        raise ValueError(f"{label} is missing")
    return path


def _verify_descriptor(
    root: Path,
    descriptor: object,
    *,
    label: str,
    maximum_bytes: int,
) -> Path:
    if not isinstance(descriptor, Mapping):
        raise ValueError(f"{label} descriptor is unavailable")
    path = _contained_file(root, descriptor.get("relative_path"), label)
    size = path.stat().st_size
    if size <= 0 or size > maximum_bytes or size != descriptor.get("byte_count"):
        raise ValueError(f"{label} size readback does not match")
    if _sha256_file(path) != descriptor.get("sha256"):
        raise ValueError(f"{label} hash readback does not match")
    return path


def _job_descriptor(
    directory: Path,
    path: Path,
    descriptor: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "artifact_id": descriptor["artifact_id"],
        "relative_path": path.relative_to(directory).as_posix(),
        "media_type": descriptor["media_type"],
        "sha256": descriptor["sha256"],
        "byte_count": descriptor["byte_count"],
    }


def _load_source_audit(
    directory: Path,
    spec: Mapping[str, Any],
    row: Mapping[str, Any],
    declared_point: Mapping[str, Any],
    *,
    source_artifact_id: object,
    field_summary_index: int,
) -> dict[str, Any]:
    matches = [
        (index, item)
        for index, item in enumerate(row["collector_summaries"])
        if item["collector"] == "wave_optics_point_audit"
        and item["artifact_id"] == source_artifact_id
    ]
    if len(matches) != 1 or matches[0][0] >= field_summary_index:
        raise ValueError("field wrapper is not bound to one preceding point audit")
    _, summary = matches[0]
    wrapper_path = _contained_file(
        directory, summary["manifest_relative_path"], "point audit wrapper"
    )
    if (
        wrapper_path.stat().st_size != summary["manifest_size_bytes"]
        or wrapper_path.stat().st_size > MAX_WRAPPER_BYTES
        or _sha256_file(wrapper_path) != summary["manifest_sha256"]
    ):
        raise ValueError("point audit wrapper differs from the durable row")
    wrapper = json.loads(wrapper_path.read_text(encoding="utf-8"))
    if (
        wrapper.get("schema_name") != "comsol_mcp.validation_matrix_collector"
        or wrapper.get("schema_version") != "1.0.0"
        or wrapper.get("collector") != "wave_optics_point_audit"
        or wrapper.get("source_model_sha256") != spec["source_model_sha256"]
        or wrapper.get("audit_status") != summary["audit_status"]
    ):
        raise ValueError("point audit wrapper identity does not match")
    point = wrapper.get("point")
    if not isinstance(point, Mapping) or (
        point.get("point_id") != row["point_id"]
        or point.get("point_fingerprint") != row["point_fingerprint"]
        or point.get("configuration_sha256") != row["configuration_sha256"]
        or point.get("wavelength") != declared_point["wavelength"]
        or point.get("incidence") != declared_point.get("incidence")
    ):
        raise ValueError("point audit wrapper point identity does not match")
    inner_descriptor = wrapper.get("inner_manifest")
    if not isinstance(inner_descriptor, Mapping):
        raise ValueError("point audit inner manifest descriptor is unavailable")
    inner_path = _contained_file(
        wrapper_path.parent,
        inner_descriptor.get("relative_path"),
        "point audit inner manifest",
    )
    if (
        inner_path.stat().st_size <= 0
        or inner_path.stat().st_size != inner_descriptor.get("size_bytes")
        or inner_path.stat().st_size > MAX_FIELD_MANIFEST_BYTES
        or _sha256_file(inner_path) != inner_descriptor.get("sha256")
    ):
        raise ValueError("point audit inner manifest differs from its wrapper")
    return {
        "summary": summary,
        "wrapper_path": wrapper_path,
        "inner_path": inner_path,
        "inner_descriptor": dict(inner_descriptor),
    }


def _load_point_field(
    directory: Path,
    spec: Mapping[str, Any],
    row: Mapping[str, Any],
) -> dict[str, Any]:
    declared_points = [
        item for item in spec["points"] if item.get("point_id") == row["point_id"]
    ]
    if len(declared_points) != 1:
        raise ValueError("matrix row does not resolve to one immutable point")
    declared_point = declared_points[0]
    field_summaries = [
        (index, item)
        for index, item in enumerate(row["collector_summaries"])
        if item["collector"] == "wave_optics_field_evidence"
    ]
    if len(field_summaries) != 1:
        raise ValueError("complete matrix row must contain exactly one field collector")
    field_summary_index, summary = field_summaries[0]
    wrapper_path = _contained_file(
        directory, summary["manifest_relative_path"], "field collector wrapper"
    )
    if (
        wrapper_path.stat().st_size != summary["manifest_size_bytes"]
        or wrapper_path.stat().st_size > MAX_WRAPPER_BYTES
        or _sha256_file(wrapper_path) != summary["manifest_sha256"]
    ):
        raise ValueError("field collector wrapper differs from the durable row")
    wrapper = json.loads(wrapper_path.read_text(encoding="utf-8"))
    if (
        wrapper.get("schema_name") != "comsol_mcp.validation_matrix_field_collector"
        or wrapper.get("schema_version") != "1.0.0"
    ):
        raise ValueError("field collector wrapper schema is unsupported")
    if wrapper.get("job_id") != directory.name:
        raise ValueError("field collector wrapper job identity does not match")
    field_collector = declared_point["collectors"][field_summary_index]
    expected_request = bind_validation_matrix_field_request(
        field_collector["inputs"],
        job_id=directory.name,
        point=declared_point,
        source_model_sha256=spec["source_model_sha256"],
    )
    expected_view = expected_request["views"][0]
    point = wrapper.get("point")
    if not isinstance(point, Mapping) or (
        point.get("point_id") != row["point_id"]
        or point.get("point_fingerprint") != row["point_fingerprint"]
        or point.get("configuration_sha256") != row["configuration_sha256"]
        or point.get("wavelength") != declared_point["wavelength"]
    ):
        raise ValueError("field collector wrapper point identity does not match")
    if wrapper.get("source_model_sha256") != spec["source_model_sha256"]:
        raise ValueError("field collector wrapper source identity does not match")
    if (
        wrapper.get("visual_review_state") != "visual_review_required"
        or wrapper.get("semantic_mode_label") != "not_assigned"
    ):
        raise ValueError("field collector wrapper contains an invalid visual-review state")
    source_artifact_id = wrapper.get("source_artifact_id")
    if (
        source_artifact_id != expected_view["source"]["artifact_id"]
        or wrapper.get("request_fingerprint") != expected_request["request_fingerprint"]
        or wrapper.get("view_fingerprint") != expected_view["view_fingerprint"]
    ):
        raise ValueError("field collector wrapper differs from the immutable point request")
    source_audit = _load_source_audit(
        directory,
        spec,
        row,
        declared_point,
        source_artifact_id=source_artifact_id,
        field_summary_index=field_summary_index,
    )

    artifact_root = wrapper_path.parent
    array_path = _verify_descriptor(
        artifact_root,
        wrapper.get("array_artifact"),
        label="field array",
        maximum_bytes=MAX_FIELD_ARRAY_BYTES,
    )
    manifest_path = _verify_descriptor(
        artifact_root,
        wrapper.get("field_manifest"),
        label="field manifest",
        maximum_bytes=MAX_FIELD_MANIFEST_BYTES,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("schema_name") != "comsol_mcp.field_evidence_manifest"
        or manifest.get("schema_version") != "1.0.0"
    ):
        raise ValueError("field manifest schema is unsupported")
    if manifest.get("measurement_status") != "measurement_complete":
        raise ValueError("paired review requires complete field manifests")
    if (
        manifest.get("visual_review_state") != "visual_review_required"
        or manifest.get("semantic_mode_label") != "not_assigned"
    ):
        raise ValueError("field manifest contains an invalid visual-review state")
    if (
        manifest.get("request_fingerprint") != wrapper.get("request_fingerprint")
        or manifest.get("view_fingerprint") != wrapper.get("view_fingerprint")
        or manifest.get("artifacts", {}).get("array") != wrapper.get("array_artifact")
        or manifest.get("configuration_sha256") != declared_point["configuration_sha256"]
        or manifest.get("view_id") != expected_view["view_id"]
        or manifest.get("wavelength_m") != expected_view["wavelength_m"]
        or manifest.get("expressions") != expected_request["expressions"]
        or manifest.get("grid") != expected_request["grid"]
        or manifest.get("slice") != expected_request["slice"]
        or wrapper.get("array_artifact", {}).get("artifact_id")
        != expected_view["outputs"]["array_artifact_id"]
        or wrapper.get("field_manifest", {}).get("artifact_id")
        != expected_view["outputs"]["manifest_artifact_id"]
    ):
        raise ValueError("field manifest identity differs from its wrapper")
    source = manifest.get("source")
    if not isinstance(source, Mapping) or (
        source.get("kind") != "validation_matrix_point"
        or source.get("source_model_sha256") != spec["source_model_sha256"]
        or source.get("job_id") != directory.name
        or source.get("point_id") != row["point_id"]
        or source.get("point_fingerprint") != row["point_fingerprint"]
        or source.get("artifact_id") != source_artifact_id
    ):
        raise ValueError("field manifest matrix source identity does not match")
    return {
        "row": dict(row),
        "wrapper": wrapper,
        "wrapper_path": wrapper_path,
        "array_path": array_path,
        "manifest": manifest,
        "manifest_path": manifest_path,
        "field_summary": summary,
        "source_audit": source_audit,
    }


def assemble_validation_matrix_field_review(
    *,
    job_directory: str | Path,
    point_ids: list[str],
    bundle_id: str,
    quantity_name: str,
    quantity_unit: str,
    coordinate_unit: str,
    color_scale: str = "linear",
    render_timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Verify two exact complete matrix fields and render one shared-scale bundle."""
    directory = Path(job_directory).expanduser().resolve()
    if not directory.is_dir() or not _portable_path_identifier(directory.name):
        raise ValueError("job_directory must be one portable durable job directory")
    if (
        not isinstance(point_ids, list)
        or len(point_ids) != 2
        or len(set(point_ids)) != 2
        or any(
            not isinstance(item, str) or not _IDENTIFIER.fullmatch(item)
            for item in point_ids
        )
    ):
        raise ValueError("point_ids must contain exactly two unique portable IDs")
    if not _portable_path_identifier(bundle_id):
        raise ValueError("bundle_id must be a portable identifier")
    spec_path = directory / "spec.json"
    if not spec_path.is_file() or spec_path.stat().st_size > 512 * 1024:
        raise ValueError("durable matrix spec is missing or oversized")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if spec.get("job_type") != "validation_matrix":
        raise ValueError("paired field review requires a validation_matrix job")
    spec_fingerprint = spec.get("spec_fingerprint")
    if not isinstance(spec_fingerprint, str) or _fingerprint(
        {key: value for key, value in spec.items() if key != "spec_fingerprint"}
    ) != spec_fingerprint:
        raise ValueError("durable matrix spec fingerprint does not match")
    rows = read_validation_rows(directory / "matrix_rows.jsonl", spec)
    selected = []
    for point_id in point_ids:
        matches = [
            row for row in rows if row["status"] == "ok" and row["point_id"] == point_id
        ]
        if len(matches) != 1:
            raise ValueError("each paired point must have exactly one complete row")
        selected.append(_load_point_field(directory, spec, matches[0]))

    first_manifest = selected[0]["manifest"]
    for item in selected[1:]:
        manifest = item["manifest"]
        for field in ("expressions", "grid", "slice", "coordinate_ranges"):
            if manifest.get(field) != first_manifest.get(field):
                raise ValueError(f"paired field manifests differ in {field}")
    expression_list = first_manifest.get("expressions")
    if not isinstance(expression_list, list) or not all(
        isinstance(item, Mapping) for item in expression_list
    ):
        raise ValueError("field manifest expressions are unavailable")
    expressions = [
        item for item in expression_list if item.get("name") == quantity_name
    ]
    if len(expressions) != 1 or expressions[0].get("unit") != quantity_unit:
        raise ValueError("requested paired quantity/unit is not present in both manifests")
    coordinate_ranges = first_manifest.get("coordinate_ranges")
    if (
        not isinstance(coordinate_ranges, Mapping)
        or coordinate_ranges.get("unit") != coordinate_unit
    ):
        raise ValueError("requested coordinate unit differs from the field manifests")

    output_root = directory / "artifacts" / "visual-review" / bundle_id
    try:
        output_root.mkdir(parents=True)
    except FileExistsError:
        raise FileExistsError("paired field review bundle already exists")
    try:
        render = render_field_png_bundle(
            views=[
                {
                    "view_id": item["row"]["point_id"],
                    "array_path": str(item["array_path"]),
                    "array_sha256": item["wrapper"]["array_artifact"]["sha256"],
                    "png_artifact_id": f"{bundle_id}-{item['row']['point_id']}-png",
                }
                for item in selected
            ],
            quantity_name=quantity_name,
            quantity_unit=quantity_unit,
            coordinate_unit=coordinate_unit,
            color_scale=color_scale,
            shared_color_limits=True,
            output_root=output_root / "png",
            timeout_seconds=render_timeout_seconds,
        )
        png_by_view = {item["view_id"]: item for item in render["views"]}
        bundle = {
            "schema_name": FIELD_REVIEW_BUNDLE_SCHEMA,
            "schema_version": FIELD_REVIEW_BUNDLE_VERSION,
            "bundle_id": bundle_id,
            "job_id": directory.name,
            "spec_fingerprint": spec["spec_fingerprint"],
            "source_model_sha256": spec["source_model_sha256"],
            "quantity": {"name": quantity_name, "unit": quantity_unit},
            "coordinate_unit": coordinate_unit,
            "color_scale": color_scale,
            "shared_color_limits": render["views"][0]["color_limits"],
            "common_grid": first_manifest["grid"],
            "common_slice": first_manifest["slice"],
            "common_coordinate_ranges": first_manifest["coordinate_ranges"],
            "points": [
                {
                    "point_id": item["row"]["point_id"],
                    "point_fingerprint": item["row"]["point_fingerprint"],
                    "row_sequence": item["row"]["sequence"],
                    "row_sha256": item["row"]["row_sha256"],
                    "wavelength_m": item["manifest"]["wavelength_m"],
                    "source_audit": {
                        "artifact_id": item["source_audit"]["summary"][
                            "artifact_id"
                        ],
                        "wrapper_relative_path": item["source_audit"][
                            "wrapper_path"
                        ]
                        .relative_to(directory)
                        .as_posix(),
                        "wrapper_sha256": item["source_audit"]["summary"][
                            "manifest_sha256"
                        ],
                        "wrapper_byte_count": item["source_audit"]["summary"][
                            "manifest_size_bytes"
                        ],
                        "inner_relative_path": item["source_audit"]["inner_path"]
                        .relative_to(directory)
                        .as_posix(),
                        "inner_sha256": item["source_audit"]["inner_descriptor"][
                            "sha256"
                        ],
                        "inner_byte_count": item["source_audit"]["inner_descriptor"][
                            "size_bytes"
                        ],
                    },
                    "wrapper": {
                        "relative_path": item["wrapper_path"]
                        .relative_to(directory)
                        .as_posix(),
                        "sha256": item["field_summary"]["manifest_sha256"],
                        "byte_count": item["field_summary"]["manifest_size_bytes"],
                    },
                    "array_artifact": _job_descriptor(
                        directory,
                        item["array_path"],
                        item["wrapper"]["array_artifact"],
                    ),
                    "field_manifest": _job_descriptor(
                        directory,
                        item["manifest_path"],
                        item["wrapper"]["field_manifest"],
                    ),
                    "png_artifact": {
                        **png_by_view[item["row"]["point_id"]],
                        "relative_path": (
                            output_root
                            / "png"
                            / png_by_view[item["row"]["point_id"]]["relative_path"]
                        )
                        .relative_to(directory)
                        .as_posix(),
                    },
                }
                for item in selected
            ],
            "visual_review_state": "visual_review_required",
            "semantic_mode_label": "not_assigned",
            "plot_process_isolated": True,
            "artifact_path_base": "job_directory",
        }
        bundle_path = output_root / "visual_review_bundle.json"
        atomic_write_json(bundle_path, bundle)
    except Exception:
        try:
            shutil.rmtree(output_root)
        except FileNotFoundError:
            pass
        except OSError as cleanup_error:
            raise RuntimeError(
                "paired field review failed and cleanup was incomplete"
            ) from cleanup_error
        raise
    return {
        "success": True,
        "bundle_id": bundle_id,
        "bundle_artifact": {
            "artifact_id": f"{bundle_id}-manifest",
            "relative_path": bundle_path.relative_to(directory).as_posix(),
            "media_type": "application/json",
            "sha256": _sha256_file(bundle_path),
            "byte_count": bundle_path.stat().st_size,
        },
        "point_count": 2,
        "shared_color_limits": bundle["shared_color_limits"],
        "visual_review_state": "visual_review_required",
        "semantic_mode_label": "not_assigned",
        "plot_process_isolated": True,
    }


__all__ = [
    "FIELD_REVIEW_BUNDLE_SCHEMA",
    "FIELD_REVIEW_BUNDLE_VERSION",
    "assemble_validation_matrix_field_review",
]
