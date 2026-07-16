"""Locked adapters from validation-matrix points to physical audit collectors."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from .store import atomic_write_json
from src.evidence.field_matrix import (
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
            from src.tools.wave_optics_audit import run_wave_optics_point_audit

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
            from src.tools.wave_optics_audit import run_wave_optics_reference_audit

            reference_audit_runner = run_wave_optics_reference_audit
        result = reference_audit_runner(model, client, **kwargs)
    else:
        raise ValueError(f"unsupported physical audit collector: {name}")
    if not isinstance(result, Mapping):
        raise ValueError("physical audit collector returned a non-object result")
    if result.get("success") is not True:
        return dict(result)
    inner_manifest = _contained_manifest(result, root)
    wrapper_path = root / "matrix_collector.json"
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
    atomic_write_json(wrapper_path, wrapper)
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
        from src.evidence.field_dataset import collect_validation_matrix_field_evidence

        field_runner = collect_validation_matrix_field_evidence
    result = field_runner(
        model=model,
        request=request,
        view_id=request["views"][0]["view_id"],
        artifact_root=root,
    )
    if not isinstance(result, Mapping):
        raise ValueError("field evidence collector returned a non-object result")
    manifest_descriptor = result.get("manifest_artifact")
    array_descriptor = result.get("array_artifact")
    if not isinstance(manifest_descriptor, Mapping) or not isinstance(
        array_descriptor, Mapping
    ):
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
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("measurement_status") != "measurement_complete":
        return {
            "success": False,
            "audit_status": manifest.get("measurement_status", "partial"),
            "error": "matrix field evidence is partial and remains retryable",
        }
    wrapper_path = root / "matrix_collector.json"
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
    atomic_write_json(wrapper_path, wrapper)
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
