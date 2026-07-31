"""Locked adapters from validation-matrix points to physical audit collectors."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from comsol_mcp.durable.io import atomic_write_json_exclusive
from comsol_mcp.evidence.contracts import validate_physical_evidence
from comsol_mcp.evidence.field_manifest import validate_field_evidence_manifest
from comsol_mcp.evidence.field_matrix import (
    MATRIX_FIELD_COLLECTOR,
    bind_validation_matrix_field_request,
)

_LOCKED_INPUTS = frozenset(
    {
        "model_name",
        "wavelength_value",
        "wavelength_unit",
        "wavelength_parameter",
        "expected_source_sha256",
        "config_id",
        "artifact_dir",
        "session_state",
        "active_profile",
        "ownership_preflight",
        "clone_factory",
        "clone_register",
        "clone_cleanup",
    }
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _contained_manifest(result: Mapping[str, Any], artifact_root: Path) -> Path:
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise ValueError("physical audit collector did not return artifact identities")
    value = artifacts.get("manifest")
    if not isinstance(value, str) or not value:
        raise ValueError("physical audit collector did not return a manifest path")
    manifest = Path(value).resolve()
    try:
        manifest.relative_to(artifact_root.resolve())
    except ValueError as exc:
        raise ValueError("physical audit manifest escapes the assigned artifact directory") from exc
    if not manifest.is_file() or manifest.stat().st_size <= 0:
        raise ValueError("physical audit manifest is missing or empty")
    return manifest


def _validate_point_audit_inner_manifest(
    path: Path,
    *,
    expected_status: object,
    point: Mapping[str, Any],
    expected_source_sha256: str,
    require_clean_measurement: bool = False,
) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("point audit inner manifest is not valid JSON") from exc
    if not isinstance(document, Mapping):
        raise ValueError("point audit inner manifest must be a JSON object")
    if document.get("audit_status") != expected_status or expected_status not in {
        "measurement_complete",
        "policy_evaluated",
    }:
        raise ValueError("point audit inner manifest status is incomplete or inconsistent")
    try:
        physical = validate_physical_evidence(document.get("physical_evidence"))
    except ValueError as exc:
        raise ValueError("point audit inner manifest physical evidence is invalid") from exc
    if physical["producer"]["tool"] != "wave_optics_point_audit":
        raise ValueError("point audit inner manifest producer is unsupported")
    identity = physical["identity"]
    if identity["source_sha256"] != expected_source_sha256.lower() or identity[
        "config_id"
    ] != point.get("point_fingerprint"):
        raise ValueError("point audit inner manifest identity differs from the matrix point")
    measurement = document.get("measurement")
    if not isinstance(measurement, Mapping):
        raise ValueError("point audit inner manifest measurement is unavailable")
    if require_clean_measurement:
        solve = measurement.get("solve")
        if (
            not isinstance(solve, Mapping)
            or solve.get("ran") is not True
            or solve.get("error") is not None
        ):
            raise ValueError("point audit inner manifest solve did not complete cleanly")
        if measurement.get("measurement_errors") not in ([], None):
            raise ValueError("point audit inner manifest contains measurement errors")
        if measurement.get("integrity_errors") not in ([], None):
            raise ValueError("point audit inner manifest contains integrity errors")
    wavelength = measurement.get("wavelength")
    declared = point.get("wavelength")
    scales = {"m": 1.0, "um": 1.0e-6, "nm": 1.0e-9}
    if not isinstance(wavelength, Mapping) or not isinstance(declared, Mapping):
        raise ValueError("point audit inner manifest wavelength identity is unavailable")
    value = declared.get("value")
    unit = declared.get("unit")
    requested = wavelength.get("requested_m")
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or unit not in scales
        or isinstance(requested, bool)
        or not isinstance(requested, (int, float))
        or not math.isfinite(float(requested))
        or not math.isclose(float(requested), float(value) * scales[unit], rel_tol=1e-12)
    ):
        raise ValueError("point audit inner manifest wavelength differs from the matrix point")
    return dict(document)


def _locked_kwargs(
    point: Mapping[str, Any],
    collector: Mapping[str, Any],
    *,
    artifact_dir: Path,
    model_name: str,
    expected_source_sha256: str,
) -> dict[str, Any]:
    inputs = collector.get("inputs")
    if not isinstance(inputs, Mapping):
        raise ValueError("collector inputs must be an object")
    conflicts = sorted(set(inputs) & _LOCKED_INPUTS)
    if conflicts:
        raise ValueError(f"collector inputs attempt to override locked fields: {conflicts}")
    wavelength = point.get("wavelength")
    if not isinstance(wavelength, Mapping):
        raise ValueError("matrix point wavelength metadata is unavailable")
    return {
        **dict(inputs),
        "model_name": model_name,
        "wavelength_value": wavelength["value"],
        "wavelength_unit": wavelength["unit"],
        "wavelength_parameter": wavelength["parameter"],
        "expected_source_sha256": expected_source_sha256,
        "config_id": point["point_fingerprint"],
        "artifact_dir": str(artifact_dir),
    }


def execute_physical_audit_collector(
    point: Mapping[str, Any],
    collector: Mapping[str, Any],
    artifact_dir: str | Path,
    *,
    model: Any,
    client: Any,
    model_name: str,
    expected_source_sha256: str,
    session_state: Mapping[str, Any],
    ownership_preflight: Mapping[str, Any],
    active_profile: str = "wave_optics",
    point_audit_runner: Callable[..., Mapping[str, Any]] | None = None,
    reference_audit_runner: Callable[..., Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run one declared reference-power collector with matrix-owned identity fields."""
    if not isinstance(model_name, str) or not model_name:
        raise ValueError("model_name must be exact and nonempty")
    if not isinstance(expected_source_sha256, str) or not re.fullmatch(
        r"[0-9A-Fa-f]{64}", expected_source_sha256
    ):
        raise ValueError("expected_source_sha256 must contain exactly 64 hexadecimal characters")
    root = Path(artifact_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    name = collector.get("name")
    kwargs = _locked_kwargs(
        point,
        collector,
        artifact_dir=root,
        model_name=model_name,
        expected_source_sha256=expected_source_sha256.lower(),
    )
    if name == "wave_optics_point_audit":
        if point_audit_runner is None:
            from comsol_mcp.tools.wave_optics_audit import run_wave_optics_point_audit

            point_audit_runner = run_wave_optics_point_audit
        result = point_audit_runner(
            model,
            **kwargs,
            session_state=dict(session_state),
            active_profile=active_profile,
            ownership_preflight=dict(ownership_preflight),
        )
    elif name == "wave_optics_reference_audit":
        if reference_audit_runner is None:
            from comsol_mcp.tools.wave_optics_audit import run_wave_optics_reference_audit

            reference_audit_runner = run_wave_optics_reference_audit
        result = reference_audit_runner(model, client, **kwargs)
    else:
        raise ValueError(f"unsupported physical audit collector: {name}")
    if not isinstance(result, Mapping):
        raise ValueError("physical audit collector returned a non-object result")
    if result.get("success") is not True:
        return dict(result)
    inner_manifest = _contained_manifest(result, root)
    if name == "wave_optics_point_audit":
        _validate_point_audit_inner_manifest(
            inner_manifest,
            expected_status=result.get("audit_status"),
            point=point,
            expected_source_sha256=expected_source_sha256,
        )
    wrapper_path = root / "matrix_collector.json"
    if inner_manifest == wrapper_path:
        raise ValueError("physical audit manifest collides with the reserved wrapper path")
    inner_relative = inner_manifest.relative_to(root).as_posix()
    wrapper = {
        "schema_name": "comsol_mcp.validation_matrix_collector",
        "schema_version": "1.0.0",
        "collector": name,
        "point": {
            "point_id": point["point_id"],
            "point_fingerprint": point["point_fingerprint"],
            "configuration_sha256": point["configuration_sha256"],
            "wavelength": point["wavelength"],
            "incidence": point.get("incidence"),
            "incidence_application": "not_mutated_by_collector_adapter",
        },
        "source_model_sha256": expected_source_sha256.lower(),
        "audit_status": result.get("audit_status"),
        "inner_manifest": {
            "relative_path": inner_relative,
            "sha256": _sha256_file(inner_manifest),
            "size_bytes": inner_manifest.stat().st_size,
        },
    }
    atomic_write_json_exclusive(wrapper_path, wrapper)
    return {
        "success": True,
        "audit_status": result.get("audit_status"),
        "artifacts": {"manifest": str(wrapper_path)},
    }


def execute_field_evidence_collector(
    point: Mapping[str, Any],
    collector: Mapping[str, Any],
    artifact_dir: str | Path,
    *,
    model: Any,
    job_id: str,
    expected_source_sha256: str,
    field_runner: Callable[..., Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Collect one matrix-owned field view after its point audit solved."""
    if collector.get("name") != MATRIX_FIELD_COLLECTOR:
        raise ValueError("field collector adapter received the wrong collector")
    root = Path(artifact_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    request = bind_validation_matrix_field_request(
        collector.get("inputs"),
        job_id=job_id,
        point=point,
        source_model_sha256=expected_source_sha256,
    )
    if field_runner is None:
        from comsol_mcp.evidence.field_dataset import collect_validation_matrix_field_evidence

        field_runner = collect_validation_matrix_field_evidence
    result = field_runner(
        model=model,
        request=request,
        view_id=request["views"][0]["view_id"],
        artifact_root=root,
    )
    if not isinstance(result, Mapping):
        raise ValueError("field evidence collector returned a non-object result")
    if "success" in result and result.get("success") is not True:
        return dict(result)
    manifest_descriptor = result.get("manifest_artifact")
    array_descriptor = result.get("array_artifact")
    if not isinstance(manifest_descriptor, Mapping) or not isinstance(array_descriptor, Mapping):
        raise ValueError("field evidence collector did not return artifact descriptors")

    def resolve_descriptor(descriptor: Mapping[str, Any], label: str) -> Path:
        relative = descriptor.get("relative_path")
        if not isinstance(relative, str) or not relative:
            raise ValueError(f"{label} relative path is unavailable")
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"{label} escapes the assigned artifact directory") from exc
        if not path.is_file() or path.stat().st_size != descriptor.get("byte_count"):
            raise ValueError(f"{label} size readback does not match")
        if _sha256_file(path) != descriptor.get("sha256"):
            raise ValueError(f"{label} hash readback does not match")
        return path

    manifest_path = resolve_descriptor(manifest_descriptor, "field manifest")
    resolve_descriptor(array_descriptor, "field array")
    manifest = validate_field_evidence_manifest(
        json.loads(manifest_path.read_text(encoding="utf-8")),
        request=request,
    )
    view = request["views"][0]
    if manifest_descriptor.get("artifact_id") != view["outputs"]["manifest_artifact_id"]:
        raise ValueError("field manifest descriptor identity does not match the request")
    if dict(array_descriptor) != manifest["artifacts"]["array"]:
        raise ValueError("field array descriptor differs from the canonical manifest")
    if manifest.get("measurement_status") != "measurement_complete":
        return {
            "success": False,
            "audit_status": manifest.get("measurement_status", "partial"),
            "error": "matrix field evidence is partial and remains retryable",
        }
    wrapper_path = root / "matrix_collector.json"
    if manifest_path == wrapper_path:
        raise ValueError("field manifest collides with the reserved wrapper path")
    wrapper = {
        "schema_name": "comsol_mcp.validation_matrix_field_collector",
        "schema_version": "1.0.0",
        "collector": MATRIX_FIELD_COLLECTOR,
        "job_id": job_id,
        "point": {
            "point_id": point["point_id"],
            "point_fingerprint": point["point_fingerprint"],
            "configuration_sha256": point["configuration_sha256"],
            "wavelength": point["wavelength"],
        },
        "source_model_sha256": expected_source_sha256,
        "source_artifact_id": request["views"][0]["source"]["artifact_id"],
        "request_fingerprint": request["request_fingerprint"],
        "view_fingerprint": request["views"][0]["view_fingerprint"],
        "array_artifact": dict(array_descriptor),
        "field_manifest": dict(manifest_descriptor),
        "visual_review_state": "visual_review_required",
        "semantic_mode_label": "not_assigned",
    }
    atomic_write_json_exclusive(wrapper_path, wrapper)
    return {
        "success": True,
        "audit_status": "measurement_complete",
        "artifacts": {"manifest": str(wrapper_path)},
    }


def execute_validation_collector(
    point: Mapping[str, Any],
    collector: Mapping[str, Any],
    artifact_dir: str | Path,
    *,
    model: Any,
    client: Any,
    model_name: str,
    job_id: str,
    expected_source_sha256: str,
    session_state: Mapping[str, Any],
    ownership_preflight: Mapping[str, Any],
    active_profile: str = "wave_optics",
) -> dict[str, Any]:
    """Dispatch one immutable validation-matrix collector."""
    if collector.get("name") == MATRIX_FIELD_COLLECTOR:
        return execute_field_evidence_collector(
            point,
            collector,
            artifact_dir,
            model=model,
            job_id=job_id,
            expected_source_sha256=expected_source_sha256,
        )
    return execute_physical_audit_collector(
        point,
        collector,
        artifact_dir,
        model=model,
        client=client,
        model_name=model_name,
        expected_source_sha256=expected_source_sha256,
        session_state=session_state,
        ownership_preflight=ownership_preflight,
        active_profile=active_profile,
    )


__all__ = [
    "execute_field_evidence_collector",
    "execute_physical_audit_collector",
    "execute_validation_collector",
]
