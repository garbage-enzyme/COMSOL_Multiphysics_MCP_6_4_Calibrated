"""Pure normalization for one bounded thermo-optomechanical replay job."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from comsol_mcp.build_identity import get_build_identity
from comsol_mcp.compatibility import module_identity_matches
from comsol_mcp.contracts.thermo_optomechanical import (
    ThermoOptomechanicalReplayInput,
    ThermoOptomechanicalReplayManifest,
)
from comsol_mcp.durable.io import read_file_bytes_bounded

from .resource_admission import normalize_resource_policy
from .store import JOB_SCHEMA_VERSION

THERMO_OPTOMECHANICAL_REPLAY_DRIVER_VERSION = "1.0.0"
THERMO_OPTOMECHANICAL_STAGES = (
    "preflight",
    "thermal_structural_solve",
    "state_evidence",
    "deformation_transfer",
    "optical_replay",
)
THERMO_OPTOMECHANICAL_CONTROLS = (
    "positive_expansion",
    "zero_cte",
    "zero_temperature_rise",
    "fixed_boundary",
    "convection",
    "wrong_selection",
    "temperature_unit",
    "missing_material_state",
    "bad_mesh",
    "rollback",
)
MAX_THERMO_OPTOMECHANICAL_SPEC_BYTES = 4 * 1024 * 1024


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def current_thermo_optomechanical_driver_identity() -> dict[str, str]:
    """Bind resume to the exact implementation and package bytes."""
    build = get_build_identity()
    return {
        "implementation": "comsol_mcp.jobs.thermo_optomechanical_replay_worker",
        "driver_version": THERMO_OPTOMECHANICAL_REPLAY_DRIVER_VERSION,
        "package_content_sha256": build["package_content_sha256"],
        "build_identity_sha256": build["build_identity_sha256"],
    }


def validate_thermo_optomechanical_driver_identity(
    spec: Mapping[str, Any],
) -> dict[str, str]:
    observed = spec.get("driver_identity")
    expected = current_thermo_optomechanical_driver_identity()
    if (
        not isinstance(observed, Mapping)
        or set(observed) != set(expected)
        or any(key != "implementation" and observed[key] != expected[key] for key in expected)
        or not module_identity_matches(expected["implementation"], observed.get("implementation"))
    ):
        raise ValueError(
            "thermo-optomechanical replay driver identity differs from the running package"
        )
    return expected


def _validate_relative_identity(value: str) -> str:
    if "\\" in value:
        raise ValueError("source_model_relative_identity must use forward slashes")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("source_model_relative_identity must be a contained relative path")
    if path.suffix.casefold() != ".mph":
        raise ValueError("source_model_relative_identity must identify an MPH file")
    return path.as_posix()


def normalize_thermo_optomechanical_replay_spec(value: object) -> dict[str, Any]:
    """Normalize and bind one exact five-stage replay without starting COMSOL."""
    submission = ThermoOptomechanicalReplayInput.model_validate(value)
    submission_raw = submission.model_dump(mode="python")
    manifest_path = Path(submission_raw["specification_path"]).expanduser().resolve()
    if (
        not str(manifest_path).isascii()
        or not manifest_path.is_file()
        or manifest_path.suffix.casefold() != ".json"
    ):
        raise ValueError("specification_path must name an existing ASCII-safe JSON file")
    try:
        manifest_payload = read_file_bytes_bounded(
            manifest_path, max_bytes=MAX_THERMO_OPTOMECHANICAL_SPEC_BYTES
        )
    except (OSError, ValueError) as exc:
        raise ValueError(
            "thermo-optomechanical replay specification is absent or exceeds its bound"
        ) from exc
    manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()
    if manifest_sha256 != submission_raw["specification_sha256"].lower():
        raise ValueError("specification_sha256 does not match specification_path")
    try:
        manifest_value = json.loads(manifest_payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("thermo-optomechanical specification JSON is invalid") from exc
    parsed = ThermoOptomechanicalReplayManifest.model_validate(manifest_value)
    raw = parsed.model_dump(mode="python", exclude_unset=True)
    source = Path(raw["source_model_path"]).expanduser().resolve()
    if not source.is_file() or source.suffix.casefold() != ".mph":
        raise ValueError("source_model_path must name an existing MPH file")
    if not str(source).isascii():
        raise ValueError("thermo-optomechanical source_model_path must be ASCII-safe")
    relative_identity = _validate_relative_identity(raw["source_model_relative_identity"])

    controls = raw["validation_controls"]
    if len(set(controls)) != len(controls) or set(controls) != set(THERMO_OPTOMECHANICAL_CONTROLS):
        raise ValueError(
            "validation_controls must contain each thermo-optomechanical control exactly once"
        )

    wavelengths = raw["optical_replay"]["wavelengths_m"]
    if len(set(wavelengths)) != len(wavelengths):
        raise ValueError("optical replay wavelengths must be unique")
    branches = raw["optical_replay"]["branches"]
    if len(set(branches)) != len(branches):
        raise ValueError("optical replay branches must be unique")

    source_sha256 = _sha256_file(source)
    state = raw["material_state"]
    if state["source_model_sha256"].lower() != source_sha256:
        raise ValueError("material state reference identifies a different source model")
    validity = state["validity"]
    if validity["temperature_min_K"] > validity["temperature_max_K"]:
        raise ValueError("material state temperature validity is reversed")
    if validity["wavelength_min_m"] > validity["wavelength_max_m"]:
        raise ValueError("material state wavelength validity is reversed")
    temperatures = (
        raw["thermal_load"]["initial_temperature_K"],
        raw["thermal_load"]["ambient_temperature_K"],
        raw["thermal_load"]["applied_temperature_K"],
        raw["thermal_expansion"]["reference_temperature_K"],
    )
    if any(
        temperature < validity["temperature_min_K"] or temperature > validity["temperature_max_K"]
        for temperature in temperatures
    ):
        raise ValueError(
            "declared thermo-optomechanical temperature is outside the selected material state"
        )
    if any(
        wavelength < validity["wavelength_min_m"] or wavelength > validity["wavelength_max_m"]
        for wavelength in wavelengths
    ):
        raise ValueError(
            "declared thermo-optomechanical wavelength is outside the selected material state"
        )
    target = state["target"]
    contract = raw["model_contract"]
    if contract["component_tag"] != target["component_tag"]:
        raise ValueError("model component tag does not match the thermal material target")

    resource_policy = normalize_resource_policy(raw["resource_policy"])
    if resource_policy is None:
        raise ValueError("resource_policy must contain explicit rules")
    rules = resource_policy["rules"]
    if (
        "wall_time_budget_seconds" in rules
        and rules["wall_time_budget_seconds"] > raw["wall_time_budget_seconds"]
    ):
        raise ValueError("resource policy wall time exceeds the thermo-optomechanical job budget")

    normalized = {
        **raw,
        "source_model_path": str(source),
        "source_model_relative_identity": relative_identity,
        "submission_manifest_path": str(manifest_path),
        "submission_manifest_sha256": manifest_sha256,
        "source_model_sha256": source_sha256,
        "optical_configuration_sha256": raw["optical_configuration_sha256"].lower(),
        "material_state": {
            **state,
            "ledger_sha256": state["ledger_sha256"].lower(),
            "material_identity_sha256": state["material_identity_sha256"].lower(),
            "sample_identity_sha256": state["sample_identity_sha256"].lower(),
            "source_model_sha256": state["source_model_sha256"].lower(),
            "application_receipt_sha256": state["application_receipt_sha256"].lower(),
        },
        "material_state_id": state["state_id"],
        "material_ledger_sha256": state["ledger_sha256"].lower(),
        "validation_controls": list(THERMO_OPTOMECHANICAL_CONTROLS),
        "resource_policy": resource_policy,
        "declared_stages": list(THERMO_OPTOMECHANICAL_STAGES),
        "declared_stage_count": len(THERMO_OPTOMECHANICAL_STAGES),
        "declared_optical_point_count": len(wavelengths) * len(branches),
        "schema_version": JOB_SCHEMA_VERSION,
        "driver_identity": current_thermo_optomechanical_driver_identity(),
    }
    if len(_canonical_bytes(normalized)) > MAX_THERMO_OPTOMECHANICAL_SPEC_BYTES:
        raise ValueError("thermo-optomechanical replay specification exceeds its bound")
    normalized["spec_fingerprint"] = _fingerprint(normalized)
    return normalized


__all__ = [
    "THERMO_OPTOMECHANICAL_CONTROLS",
    "THERMO_OPTOMECHANICAL_STAGES",
    "current_thermo_optomechanical_driver_identity",
    "normalize_thermo_optomechanical_replay_spec",
    "validate_thermo_optomechanical_driver_identity",
]
