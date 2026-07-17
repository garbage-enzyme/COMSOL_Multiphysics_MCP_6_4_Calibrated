"""Strict adapter from point-audit artifacts to durable spectral rows."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from src.evidence.contracts import validate_physical_evidence

from .spectral_rows import spectral_point_identity


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object with string keys")
    return dict(value)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _contained_file(path: Path, root: Path, name: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{name} escapes its assigned artifact directory") from exc
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise ValueError(f"{name} is missing or empty")
    return resolved


def _finite(value: object, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0.0):
        qualifier = "positive and finite" if positive else "finite"
        raise ValueError(f"{name} must be {qualifier}")
    return number


def _optional_count(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer or null")
    return value


def build_spectral_audit_point(
    spec: Mapping[str, Any], wavelength_m: object
) -> dict[str, Any]:
    """Build one validation-collector point from an immutable spectral identity."""
    identity = spectral_point_identity(spec, wavelength_m)
    collector = _mapping(spec.get("collector"), "collector")
    incidence = spec.get("parameter_state", {}).get("incidence")
    return {
        "point_id": identity["point_id"],
        "point_fingerprint": identity["point_fingerprint"],
        "configuration_sha256": spec["configuration_sha256"],
        "wavelength": {
            "value": identity["requested_wavelength_m"],
            "unit": "m",
            "parameter": spec["wavelength_parameter"],
        },
        "incidence": incidence if isinstance(incidence, Mapping) else None,
        "collectors": [collector],
        "expected_artifact_ids": [f"audit-{identity['point_fingerprint'][:20]}"],
    }


def extract_spectral_audit_result(
    *,
    job_dir: str | Path,
    artifact_dir: str | Path,
    spec: Mapping[str, Any],
    point: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify a complete collector result and return exact append-row arguments."""
    response = _mapping(result, "point audit result")
    if response.get("success") is not True:
        raise ValueError("point audit did not complete successfully")
    if response.get("audit_status") not in {"measurement_complete", "policy_evaluated"}:
        raise ValueError("point audit evidence is incomplete")
    artifacts = _mapping(response.get("artifacts"), "point audit artifacts")
    manifest_value = artifacts.get("manifest")
    if not isinstance(manifest_value, str) or not manifest_value:
        raise ValueError("point audit wrapper path is unavailable")
    job_root = Path(job_dir).resolve()
    assigned_root = Path(artifact_dir).resolve()
    wrapper_path = _contained_file(Path(manifest_value), assigned_root, "point audit wrapper")
    try:
        wrapper = json.loads(wrapper_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("point audit wrapper is not valid JSON") from exc
    wrapper = _mapping(wrapper, "point audit wrapper")
    if wrapper.get("schema_name") != "comsol_mcp.validation_matrix_collector":
        raise ValueError("point audit wrapper schema is unsupported")
    if wrapper.get("collector") != spec["collector"]["name"]:
        raise ValueError("point audit wrapper collector differs from the job")
    wrapped_point = _mapping(wrapper.get("point"), "point audit wrapper point")
    for field in ("point_id", "point_fingerprint", "configuration_sha256", "wavelength"):
        if wrapped_point.get(field) != point.get(field):
            raise ValueError(f"point audit wrapper {field} differs from the frozen point")
    if wrapper.get("source_model_sha256") != spec["source_model_sha256"]:
        raise ValueError("point audit wrapper source hash differs from the job")
    if wrapper.get("audit_status") != response["audit_status"]:
        raise ValueError("point audit status differs between response and wrapper")
    inner_descriptor = _mapping(wrapper.get("inner_manifest"), "inner manifest descriptor")
    if set(inner_descriptor) != {"relative_path", "sha256", "size_bytes"}:
        raise ValueError("inner manifest descriptor fields are invalid")
    relative = inner_descriptor["relative_path"]
    if not isinstance(relative, str) or not relative:
        raise ValueError("inner manifest relative path is unavailable")
    inner_path = _contained_file(assigned_root / relative, assigned_root, "point audit inner manifest")
    if inner_path.stat().st_size != inner_descriptor["size_bytes"]:
        raise ValueError("point audit inner manifest size differs from its wrapper")
    if _sha256_file(inner_path) != inner_descriptor["sha256"]:
        raise ValueError("point audit inner manifest hash differs from its wrapper")
    try:
        inner = json.loads(inner_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("point audit inner manifest is not valid JSON") from exc
    inner = _mapping(inner, "point audit inner manifest")
    if inner.get("audit_status") != response["audit_status"]:
        raise ValueError("point audit inner status differs from its wrapper")
    physical = validate_physical_evidence(inner.get("physical_evidence"))
    if physical["producer"]["tool"] != "wave_optics_point_audit":
        raise ValueError("point audit physical evidence producer is unsupported")
    if physical["identity"]["source_sha256"] != spec["source_model_sha256"]:
        raise ValueError("point audit physical evidence source differs from the job")
    if physical["identity"]["config_id"] != point["point_fingerprint"]:
        raise ValueError("point audit physical evidence identity differs from the point")

    measurement = _mapping(inner.get("measurement"), "point audit measurement")
    wavelength = _mapping(measurement.get("wavelength"), "point audit wavelength")
    power = _mapping(measurement.get("power"), "point audit power")
    mesh = _mapping(measurement.get("mesh"), "point audit mesh")
    solve = _mapping(measurement.get("solve"), "point audit solve")
    if solve.get("ran") is not True or solve.get("error") is not None:
        raise ValueError("point audit solve did not complete cleanly")
    if measurement.get("integrity_errors") not in ([], None):
        raise ValueError("point audit contains integrity errors")
    if measurement.get("measurement_errors") not in ([], None):
        raise ValueError("point audit contains measurement errors")
    requested = _finite(wavelength.get("requested_m"), "requested wavelength", positive=True)
    if requested != float(point["wavelength"]["value"]):
        raise ValueError("point audit requested wavelength differs from the frozen point")

    for path, name in ((wrapper_path, "wrapper"), (inner_path, "inner")):
        try:
            path.relative_to(job_root)
        except ValueError as exc:
            raise ValueError(f"point audit {name} artifact escapes the durable job") from exc
    return {
        "requested_wavelength_m": requested,
        "evaluated_wavelength_m": _finite(
            wavelength.get("evaluated_parameter_m"),
            "evaluated wavelength",
            positive=True,
        ),
        "frequency_wavelength_m": _finite(
            wavelength.get("solved_frequency_wavelength_m"),
            "frequency wavelength",
            positive=True,
        ),
        "R": _finite(power.get("R"), "R"),
        "T": _finite(power.get("T"), "T"),
        "A": _finite(power.get("A"), "A"),
        "mesh_element_count": _optional_count(mesh.get("element_count"), "mesh element count"),
        "mesh_vertex_count": _optional_count(mesh.get("vertex_count"), "mesh vertex count"),
        "solve_seconds": _finite(solve.get("seconds"), "solve seconds"),
        "audit_artifact": {
            "wrapper_relative_path": wrapper_path.relative_to(job_root).as_posix(),
            "wrapper_sha256": _sha256_file(wrapper_path),
            "wrapper_size_bytes": wrapper_path.stat().st_size,
            "inner_relative_path": inner_path.relative_to(job_root).as_posix(),
            "inner_sha256": _sha256_file(inner_path),
            "inner_size_bytes": inner_path.stat().st_size,
            "physical_evidence_sha256": physical["contract_sha256"],
            "audit_status": response["audit_status"],
        },
    }


__all__ = ["build_spectral_audit_point", "extract_spectral_audit_result"]
