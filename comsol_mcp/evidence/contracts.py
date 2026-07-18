"""Strict, deterministic contracts for physical evidence and validation policy.

This module is deliberately solver-free.  It does not import MPh, COMSOL, Java,
or solver ownership code, so contracts can be validated during discovery, in
workers, and by offline artifact reviewers.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Mapping

from comsol_mcp.durable import canonical_json_v1, canonical_sha256_v1


PHYSICAL_EVIDENCE_SCHEMA_NAME = "comsol_mcp.physical_evidence"
PHYSICAL_EVIDENCE_SCHEMA_VERSION = "1.1.0"
PHYSICAL_EVIDENCE_READABLE_VERSIONS = frozenset({"1.0.0", "1.1.0"})
VALIDATION_POLICY_SCHEMA_NAME = "comsol_mcp.validation_policy"
VALIDATION_POLICY_SCHEMA_VERSION = "1.0.0"

EVIDENCE_STATES = frozenset(
    {
        "measured",
        "derived_from_declared_convention",
        "label_only",
        "unknown",
        "not_requested",
        "not_applicable",
    }
)

MAX_CONTRACT_BYTES = 1024 * 1024
MAX_EVIDENCE_RECORDS = 512
MAX_POLICY_RULES = 32
MAX_TEXT = 4096
MAX_LIST_ITEMS = 1024
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_EVIDENCE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,191}$")

_ENVELOPE_FIELDS = {
    "schema_name",
    "schema_version",
    "artifact_type",
    "producer",
    "identity",
    "model",
    "evidence",
    "limitations",
    "migration",
    "contract_sha256",
}
_PRODUCER_FIELDS = {"tool", "tool_schema_version"}
_IDENTITY_FIELDS = {"config_id", "config_sha256", "source_sha256", "source_artifact_id"}
_MODEL_FIELDS = {
    "component_tag",
    "physics_tag",
    "study_tag",
    "study_step_tag",
    "mesh_tag",
    "mesh_element_count",
    "mesh_vertex_count",
}
_RECORD_FIELDS = {
    "state",
    "value",
    "unit",
    "expression",
    "sign_convention",
    "selection_ids",
    "source",
    "limitations",
}
_LEGACY_MIGRATION_FIELDS = {"source_schema_name", "source_schema_version", "semantics"}
_MIGRATION_FIELDS = _LEGACY_MIGRATION_FIELDS | {
    "source_artifact_sha256",
    "source_hash_basis",
    "transformation_id",
    "transformation_version",
    "transformation_sha256",
    "output_mode",
}
_POLICY_FIELDS = {"schema_name", "schema_version", "policy_id", "rules", "policy_sha256"}
_RULE_FIELDS = {"rule_id", "rule_type", "required_measurements", "tolerances", "assumptions"}

_RULE_SPECS = {
    "passive_rta_bounds": {
        "required": ("power.R", "power.T", "power.A"),
        "tolerances": frozenset({"margin"}),
        "required_tolerances": frozenset({"margin"}),
        "assumptions": {"passive": True, "power_normalized": True},
    },
    "wavelength_synchronization": {
        "required": ("wavelength.evaluated_parameter_m", "wavelength.solved_frequency_m"),
        "tolerances": frozenset({"absolute_m", "relative"}),
        "required_tolerances": frozenset(),
        "assumptions": {},
    },
    "declared_flux_closure": {
        "required": (
            "flux.incident_raw_power_w",
            "flux.reflected_raw_power_w",
            "flux.transmitted_raw_power_w",
            "flux.incident_positive_power_sign",
            "flux.reflected_positive_power_sign",
            "flux.transmitted_positive_power_sign",
            "flux.incident_power_w",
            "flux.reflected_power_w",
            "flux.transmitted_power_w",
            "flux.R",
            "flux.T",
            "flux.A",
            "flux.closure_abs",
            "flux.convention_complete",
            "flux.physical_flux_closure_eligible",
        ),
        "tolerances": frozenset({"closure_abs", "margin"}),
        "required_tolerances": frozenset({"closure_abs", "margin"}),
        "assumptions": {"sign_convention_declared": True, "plane_medium_declared": True},
    },
    "reference_air_polarization_ratio": {
        "required": (
            "polarization.reference_air_method_valid",
            "polarization.target_to_transverse_ratio",
        ),
        "tolerances": frozenset({"minimum_ratio"}),
        "required_tolerances": frozenset({"minimum_ratio"}),
        "assumptions": {},
    },
    "mesh_evidence_presence": {
        "required": ("mesh.element_count",),
        "tolerances": frozenset({"minimum_elements"}),
        "required_tolerances": frozenset(),
        "assumptions": {},
    },
}


def _reject_unknown(mapping: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ValueError(f"{label} contains unknown fields: {unknown}")


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _bounded_text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f"{label} must be a non-empty string")
    if len(value) > MAX_TEXT:
        raise ValueError(f"{label} exceeds {MAX_TEXT} characters")
    return value


def _identifier(value: Any, label: str) -> str:
    text = _bounded_text(value, label)
    if not _IDENTIFIER.fullmatch(text):
        raise ValueError(f"{label} is not a valid bounded identifier")
    return text


def _hash64(value: Any, label: str) -> str:
    text = _bounded_text(value, label).lower()
    if not _HEX64.fullmatch(text):
        raise ValueError(f"{label} must be exactly 64 hexadecimal characters")
    return text


def _finite_json(value: Any, label: str = "value", depth: int = 0) -> None:
    if depth > 32:
        raise ValueError(f"{label} exceeds the maximum nesting depth")
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str) and len(value) > MAX_TEXT:
            raise ValueError(f"{label} contains oversized text")
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} contains a non-finite number")
        return
    if isinstance(value, list):
        if len(value) > MAX_LIST_ITEMS:
            raise ValueError(f"{label} contains too many list items")
        for index, item in enumerate(value):
            _finite_json(item, f"{label}[{index}]", depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_LIST_ITEMS:
            raise ValueError(f"{label} contains too many object fields")
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 256:
                raise ValueError(f"{label} contains an invalid object key")
            _finite_json(item, f"{label}.{key}", depth + 1)
        return
    raise ValueError(f"{label} contains a non-JSON value of type {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON after rejecting non-finite data."""
    _finite_json(value)
    encoded = canonical_json_v1(value)
    if len(encoded) > MAX_CONTRACT_BYTES:
        raise ValueError(f"contract exceeds {MAX_CONTRACT_BYTES} bytes")
    return encoded


def canonical_sha256(value: Any) -> str:
    canonical_json_bytes(value)
    return canonical_sha256_v1(value)


def _validate_text_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_LIST_ITEMS:
        raise ValueError(f"{label} must be a bounded string list")
    return [_bounded_text(item, f"{label}[{index}]") for index, item in enumerate(value)]


def _validate_record(key: str, record: Any) -> None:
    item = _require_mapping(record, f"evidence.{key}")
    _reject_unknown(item, _RECORD_FIELDS, f"evidence.{key}")
    state = item.get("state")
    if state not in EVIDENCE_STATES:
        raise ValueError(f"evidence.{key}.state must be one of {sorted(EVIDENCE_STATES)}")
    if state in {"measured", "derived_from_declared_convention"} and "value" not in item:
        raise ValueError(f"evidence.{key} with state {state!r} requires value")
    if state in {"unknown", "not_requested", "not_applicable"} and "value" in item:
        raise ValueError(f"evidence.{key} with state {state!r} cannot contain value")
    for field in ("unit", "expression", "sign_convention", "source"):
        if field in item and item[field] is not None:
            _bounded_text(item[field], f"evidence.{key}.{field}")
    if "selection_ids" in item:
        selection_ids = item["selection_ids"]
        if not isinstance(selection_ids, list) or len(selection_ids) > MAX_LIST_ITEMS:
            raise ValueError(f"evidence.{key}.selection_ids must be a bounded list")
        for index, entity in enumerate(selection_ids):
            if isinstance(entity, bool) or not isinstance(entity, (int, str)):
                raise ValueError(f"evidence.{key}.selection_ids[{index}] must be an integer or string")
            if isinstance(entity, int) and entity <= 0:
                raise ValueError(f"evidence.{key}.selection_ids[{index}] must be positive")
            if isinstance(entity, str):
                _bounded_text(entity, f"evidence.{key}.selection_ids[{index}]")
    if "limitations" in item:
        _validate_text_list(item["limitations"], f"evidence.{key}.limitations")
    if "value" in item:
        _finite_json(item["value"], f"evidence.{key}.value")


def validate_physical_evidence(payload: Any, *, verify_hash: bool = True) -> dict[str, Any]:
    """Validate and return a detached physical-evidence envelope."""
    envelope = _require_mapping(payload, "physical_evidence")
    _reject_unknown(envelope, _ENVELOPE_FIELDS, "physical_evidence")
    if envelope.get("schema_name") != PHYSICAL_EVIDENCE_SCHEMA_NAME:
        raise ValueError("physical_evidence.schema_name is unsupported")
    schema_version = envelope.get("schema_version")
    if schema_version not in PHYSICAL_EVIDENCE_READABLE_VERSIONS:
        raise ValueError("physical_evidence.schema_version is unsupported")
    _identifier(envelope.get("artifact_type"), "physical_evidence.artifact_type")

    producer = _require_mapping(envelope.get("producer"), "physical_evidence.producer")
    _reject_unknown(producer, _PRODUCER_FIELDS, "physical_evidence.producer")
    _identifier(producer.get("tool"), "physical_evidence.producer.tool")
    _bounded_text(producer.get("tool_schema_version"), "physical_evidence.producer.tool_schema_version")

    identity = _require_mapping(envelope.get("identity"), "physical_evidence.identity")
    _reject_unknown(identity, _IDENTITY_FIELDS, "physical_evidence.identity")
    _bounded_text(identity.get("config_id"), "physical_evidence.identity.config_id")
    _hash64(identity.get("config_sha256"), "physical_evidence.identity.config_sha256")
    _hash64(identity.get("source_sha256"), "physical_evidence.identity.source_sha256")
    if "source_artifact_id" in identity:
        _bounded_text(identity["source_artifact_id"], "physical_evidence.identity.source_artifact_id")

    model = _require_mapping(envelope.get("model"), "physical_evidence.model")
    _reject_unknown(model, _MODEL_FIELDS, "physical_evidence.model")
    for field in ("component_tag", "physics_tag", "study_tag", "study_step_tag", "mesh_tag"):
        if model.get(field) is not None:
            _identifier(model[field], f"physical_evidence.model.{field}")
    for field in ("mesh_element_count", "mesh_vertex_count"):
        if model.get(field) is not None and (
            isinstance(model[field], bool) or not isinstance(model[field], int) or model[field] < 0
        ):
            raise ValueError(f"physical_evidence.model.{field} must be a non-negative integer or null")

    evidence = _require_mapping(envelope.get("evidence"), "physical_evidence.evidence")
    if len(evidence) > MAX_EVIDENCE_RECORDS:
        raise ValueError(f"physical_evidence.evidence exceeds {MAX_EVIDENCE_RECORDS} records")
    for key, record in evidence.items():
        if not isinstance(key, str) or not _EVIDENCE_KEY.fullmatch(key):
            raise ValueError(f"invalid evidence key: {key!r}")
        _validate_record(key, record)

    _validate_text_list(envelope.get("limitations", []), "physical_evidence.limitations")
    if "migration" in envelope:
        migration = _require_mapping(envelope["migration"], "physical_evidence.migration")
        migration_fields = (
            _LEGACY_MIGRATION_FIELDS
            if schema_version == "1.0.0"
            else _MIGRATION_FIELDS
        )
        _reject_unknown(migration, migration_fields, "physical_evidence.migration")
        if set(migration) != migration_fields:
            raise ValueError("physical_evidence.migration fields are incomplete")
        _bounded_text(migration.get("source_schema_name"), "physical_evidence.migration.source_schema_name")
        _bounded_text(migration.get("source_schema_version"), "physical_evidence.migration.source_schema_version")
        if migration.get("semantics") != "preserved_without_reinterpretation":
            raise ValueError("physical_evidence.migration.semantics is unsupported")
        if schema_version != "1.0.0":
            _hash64(
                migration.get("source_artifact_sha256"),
                "physical_evidence.migration.source_artifact_sha256",
            )
            if migration.get("source_hash_basis") not in {"canonical_json", "file_bytes"}:
                raise ValueError("physical_evidence.migration.source_hash_basis is unsupported")
            _identifier(
                migration.get("transformation_id"),
                "physical_evidence.migration.transformation_id",
            )
            _bounded_text(
                migration.get("transformation_version"),
                "physical_evidence.migration.transformation_version",
            )
            if migration.get("output_mode") != "new_artifact":
                raise ValueError("physical_evidence.migration.output_mode is unsupported")
            supplied_transformation_hash = _hash64(
                migration.get("transformation_sha256"),
                "physical_evidence.migration.transformation_sha256",
            )
            transformation = dict(migration)
            transformation.pop("transformation_sha256")
            transformation["target_schema_name"] = PHYSICAL_EVIDENCE_SCHEMA_NAME
            transformation["target_schema_version"] = schema_version
            if supplied_transformation_hash != canonical_sha256(transformation):
                raise ValueError("physical_evidence.migration transformation hash does not match")

    supplied_hash = _hash64(envelope.get("contract_sha256"), "physical_evidence.contract_sha256")
    without_hash = dict(envelope)
    without_hash.pop("contract_sha256", None)
    expected_hash = canonical_sha256(without_hash)
    if verify_hash and supplied_hash != expected_hash:
        raise ValueError("physical_evidence.contract_sha256 does not match the canonical payload")
    canonical_json_bytes(envelope)
    return deepcopy(envelope)


def build_physical_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Add a canonical content hash to an otherwise complete envelope."""
    envelope = deepcopy(dict(payload))
    if "contract_sha256" in envelope:
        raise ValueError("build_physical_evidence computes contract_sha256; callers must omit it")
    envelope["contract_sha256"] = canonical_sha256(envelope)
    return validate_physical_evidence(envelope)


def _record(
    state: str,
    *,
    value: Any = None,
    value_present: bool = False,
    unit: str | None = None,
    expression: str | None = None,
    sign_convention: str | None = None,
    selection_ids: list[int | str] | None = None,
    source: str | None = None,
    limitations: list[str] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {"state": state}
    if value_present:
        item["value"] = value
    if unit is not None:
        item["unit"] = unit
    if expression is not None:
        item["expression"] = expression
    if sign_convention is not None:
        item["sign_convention"] = sign_convention
    if selection_ids is not None:
        item["selection_ids"] = selection_ids
    if source is not None:
        item["source"] = source
    if limitations:
        item["limitations"] = limitations
    return item


def _measured_or_unknown(
    value: Any,
    *,
    unit: str | None = None,
    expression: str | None = None,
    sign_convention: str | None = None,
    source: str | None = None,
    limitations: list[str] | None = None,
) -> dict[str, Any]:
    if value is None:
        return _record("unknown", limitations=limitations or ["Legacy artifact did not contain this measurement."])
    return _record(
        "measured",
        value=value,
        value_present=True,
        unit=unit,
        expression=expression,
        sign_convention=sign_convention,
        source=source,
        limitations=limitations,
    )


def _point_audit_envelope(
    payload: Mapping[str, Any],
    *,
    migrated: bool,
    source_artifact_sha256: str | None = None,
    source_hash_basis: str = "canonical_json",
) -> dict[str, Any]:
    outer = _require_mapping(dict(payload), "legacy_point_audit")
    measurement = outer.get("measurement", outer)
    measurement = _require_mapping(measurement, "legacy_point_audit.measurement")
    provenance = _require_mapping(measurement.get("provenance"), "legacy_point_audit.measurement.provenance")
    wavelength = _require_mapping(measurement.get("wavelength", {}), "legacy_point_audit.measurement.wavelength")
    power = _require_mapping(measurement.get("power", {}), "legacy_point_audit.measurement.power")
    polarization = _require_mapping(measurement.get("polarization", {}), "legacy_point_audit.measurement.polarization")
    declared_flux = _require_mapping(
        measurement.get("declared_plane_flux", {"state": "not_requested"}),
        "legacy_point_audit.measurement.declared_plane_flux",
    )
    internal_absorption = _require_mapping(
        measurement.get("internal_absorption_consistency", {"state": "not_requested"}),
        "legacy_point_audit.measurement.internal_absorption_consistency",
    )
    mesh = _require_mapping(measurement.get("mesh", {}), "legacy_point_audit.measurement.mesh")
    integrity = _require_mapping(measurement.get("integrity", {}), "legacy_point_audit.measurement.integrity")

    config_sha256 = provenance.get("config_sha256") or outer.get("config_sha256")
    source_sha256 = provenance.get("source_sha256_before") or outer.get("source_sha256")
    power_expressions = power.get("expressions", {}) if isinstance(power.get("expressions", {}), dict) else {}
    power_provenance = power.get("provenance", {}) if isinstance(power.get("provenance", {}), dict) else {}
    flux_directions = power_provenance.get("flux_directions", {}) if isinstance(power_provenance.get("flux_directions", {}), dict) else {}
    record_source = "legacy_point_audit_v1" if migrated else "wave_optics_point_audit"

    evidence: dict[str, dict[str, Any]] = {
        "wavelength.requested_m": (
            _record(
                "derived_from_declared_convention",
                value=wavelength.get("requested_m"),
                value_present=True,
                unit="m",
                expression=wavelength.get("parameter_expression"),
                source=record_source,
            )
            if wavelength.get("requested_m") is not None
            else _record("unknown", limitations=["Legacy artifact did not contain the requested wavelength in metres."])
        ),
        "wavelength.evaluated_parameter_m": _measured_or_unknown(
            wavelength.get("evaluated_parameter_m"), unit="m", source=record_source
        ),
        "wavelength.solved_frequency_m": _measured_or_unknown(
            wavelength.get("solved_frequency_wavelength_m"), unit="m", source=record_source
        ),
        "wavelength.absolute_difference_m": _measured_or_unknown(
            wavelength.get("absolute_difference_m"), unit="m", source=record_source
        ),
        "wavelength.relative_difference": _measured_or_unknown(
            wavelength.get("relative_difference"), unit="1", source=record_source
        ),
        "power.R": _measured_or_unknown(
            power.get("R"), unit="1", expression=power_expressions.get("R"), sign_convention=flux_directions.get("R"), source=record_source
        ),
        "power.T": _measured_or_unknown(
            power.get("T"), unit="1", expression=power_expressions.get("T"), sign_convention=flux_directions.get("T"), source=record_source
        ),
        "power.A": _measured_or_unknown(
            power.get("A"), unit="1", expression=power_expressions.get("A"), sign_convention=power_provenance.get("A_definition"), source=record_source
        ),
        "power.port_closure_abs": _measured_or_unknown(
            power.get("closure_abs"), unit="1", source=record_source,
            limitations=["This legacy port-variable closure is not declared-plane flux closure."],
        ),
        "mesh.element_count": _measured_or_unknown(mesh.get("element_count"), unit="1", source=record_source),
        "mesh.vertex_count": _measured_or_unknown(mesh.get("vertex_count"), unit="1", source=record_source),
        "integrity.source_unchanged": _measured_or_unknown(integrity.get("source_unchanged"), unit="1", source=record_source),
    }

    flux_names = (
        "incident_raw_power_w",
        "reflected_raw_power_w",
        "transmitted_raw_power_w",
        "incident_positive_power_sign",
        "reflected_positive_power_sign",
        "transmitted_positive_power_sign",
        "incident_power_w",
        "reflected_power_w",
        "transmitted_power_w",
        "R",
        "T",
        "A",
        "closure_abs",
        "convention_complete",
        "physical_flux_closure_eligible",
    )
    if declared_flux.get("state") == "derived_from_declared_convention":
        planes = declared_flux.get("planes", {})
        for plane_name in ("incident", "reflected", "transmitted"):
            plane = planes.get(plane_name, {}) if isinstance(planes, dict) else {}
            evidence[f"flux.{plane_name}_raw_power_w"] = _record(
                "measured",
                value=plane.get("raw_power_w"),
                value_present=True,
                unit="W",
                expression=plane.get("expression"),
                selection_ids=plane.get("selection_ids"),
                source=record_source,
            )
            evidence[f"flux.{plane_name}_positive_power_sign"] = _record(
                "derived_from_declared_convention",
                value=plane.get("positive_power_sign"),
                value_present=True,
                sign_convention=(
                    f"directed_power_w = raw_power_w * {plane.get('positive_power_sign')}"
                ),
                source=record_source,
            )
            evidence[f"flux.{plane_name}_power_w"] = _record(
                "derived_from_declared_convention",
                value=plane.get("directed_power_w"),
                value_present=True,
                unit="W",
                expression=plane.get("expression"),
                sign_convention=(
                    f"directed_power_w = raw_power_w * {plane.get('positive_power_sign')}"
                ),
                selection_ids=plane.get("selection_ids"),
                source=record_source,
            )
        for name in ("R", "T", "A", "closure_abs"):
            evidence[f"flux.{name}"] = _record(
                "derived_from_declared_convention",
                value=declared_flux.get(name),
                value_present=True,
                unit="1",
                source=record_source,
            )
        evidence["flux.convention_complete"] = _record(
            "derived_from_declared_convention", value=True, value_present=True, source=record_source
        )
        evidence["flux.physical_flux_closure_eligible"] = _record(
            "derived_from_declared_convention", value=True, value_present=True, source=record_source
        )
    else:
        state = "not_requested" if declared_flux.get("state") == "not_requested" else "unknown"
        limitation = (
            "Caller did not request declared-plane flux evidence."
            if state == "not_requested"
            else "Declared-plane flux expressions did not produce complete finite evidence."
        )
        for name in flux_names:
            evidence[f"flux.{name}"] = _record(state, limitations=[limitation])

    if internal_absorption.get("state") == "measured":
        evidence["absorption.cross_section_normalized"] = _record(
            "derived_from_declared_convention",
            value=internal_absorption.get("cross_section", {}).get("normalized_absorption"),
            value_present=True,
            unit="1",
            expression=internal_absorption.get("cross_section", {}).get("expression"),
            source=record_source,
        )
        evidence["absorption.volume_loss_normalized"] = _record(
            "derived_from_declared_convention",
            value=internal_absorption.get("volume_loss", {}).get("normalized_absorption"),
            value_present=True,
            unit="1",
            expression=internal_absorption.get("volume_loss", {}).get("expression"),
            selection_ids=internal_absorption.get("volume_loss", {}).get("selection_ids"),
            source=record_source,
        )
        evidence["absorption.internal_relative_residual"] = _record(
            "derived_from_declared_convention",
            value=internal_absorption.get("relative_residual"),
            value_present=True,
            unit="1",
            source=record_source,
            limitations=["Internal normalization consistency is not physical flux closure."],
        )
        evidence["absorption.internal_consistency_closure_eligible"] = _record(
            "derived_from_declared_convention", value=False, value_present=True, source=record_source
        )
    else:
        internal_state = (
            "not_requested" if internal_absorption.get("state") == "not_requested" else "unknown"
        )
        for name in (
            "cross_section_normalized",
            "volume_loss_normalized",
            "internal_relative_residual",
            "internal_consistency_closure_eligible",
        ):
            evidence[f"absorption.{name}"] = _record(
                internal_state,
                limitations=["Internal absorption comparison was not complete."],
            )

    legacy_level = polarization.get("evidence_level", "unknown")
    if legacy_level in {"incident_reference", "direct_incident_field"}:
        evidence["polarization.physical_incident"] = _record(
            "measured",
            value=legacy_level,
            value_present=True,
            source=record_source,
            limitations=["The legacy evidence level is preserved; component statistics remain in the original artifact."],
        )
    elif legacy_level == "label_only":
        evidence["polarization.physical_incident"] = _record(
            "label_only", value=legacy_level, value_present=True, source=record_source
        )
    else:
        evidence["polarization.physical_incident"] = _record(
            "label_only" if legacy_level == "structure_total_field" else "unknown",
            value=legacy_level if legacy_level == "structure_total_field" else None,
            value_present=legacy_level == "structure_total_field",
            source=record_source,
            limitations=["Structure total field is diagnostic and is not incident-field evidence."],
        )
    evidence["polarization.target_to_transverse_ratio"] = _record(
        "unknown",
        limitations=["Legacy point-audit schema did not normalize a declared target/transverse reference-air ratio."],
    )
    evidence["polarization.reference_air_method_valid"] = _record(
        "unknown",
        limitations=["The point audit does not independently validate the reference-air construction method."],
    )
    structure_field = polarization.get("structure_total_field")
    if isinstance(structure_field, dict) and structure_field.get("complete"):
        selection = structure_field.get("selection", {})
        selection = selection if isinstance(selection, dict) else {}
        selection_ids: list[int | str] = []
        if isinstance(selection.get("named_selection"), str):
            selection_ids.append(selection["named_selection"])
        if isinstance(selection.get("domain_ids"), list):
            selection_ids.extend(
                entity for entity in selection["domain_ids"]
                if isinstance(entity, int) and not isinstance(entity, bool) and entity > 0
            )
        evidence["polarization.structure_total_field"] = _record(
            "measured",
            value={
                "sample_count": selection.get("sample_count"),
                "diagnostic_only": True,
            },
            value_present=True,
            expression=f"{provenance.get('physics_tag', 'ewfd')}.Ex/Ey/Ez",
            selection_ids=selection_ids,
            source=record_source,
            limitations=["Structure total field contains reflection and cannot prove the incident vector."],
        )
    else:
        evidence["polarization.structure_total_field"] = _record(
            "unknown",
            limitations=["No bounded structure-total-field sample was present in the point audit."],
        )

    envelope: dict[str, Any] = {
            "schema_name": PHYSICAL_EVIDENCE_SCHEMA_NAME,
            "schema_version": PHYSICAL_EVIDENCE_SCHEMA_VERSION,
            "artifact_type": "wave_optics_point_audit",
            "producer": {
                "tool": "wave_optics_point_audit",
                "tool_schema_version": (
                    str(measurement.get("schema_version", "1"))
                    if migrated
                    else "physical-evidence-1"
                ),
            },
            "identity": {
                "config_id": str(measurement.get("config_id") or outer.get("config_id") or "legacy-unknown"),
                "config_sha256": _hash64(config_sha256, "legacy config_sha256"),
                "source_sha256": _hash64(source_sha256, "legacy source_sha256"),
            },
            "model": {
                "component_tag": provenance.get("component_tag"),
                "physics_tag": provenance.get("physics_tag"),
                "study_tag": provenance.get("study_tag"),
                "study_step_tag": provenance.get("study_step_tag"),
                "mesh_tag": mesh.get("mesh_tag"),
                "mesh_element_count": mesh.get("element_count"),
                "mesh_vertex_count": mesh.get("vertex_count"),
            },
            "evidence": evidence,
            "limitations": [
                (
                    "Schema-1 fields are preserved without promoting labels or shared normalizations to newer physical evidence."
                    if migrated
                    else "Structure total field and port-variable closure retain their diagnostic limitations."
                ),
            ],
        }
    if migrated:
        migration = {
            "source_schema_name": "comsol_mcp.wave_optics_point_audit",
            "source_schema_version": str(measurement.get("schema_version", "1")),
            "source_artifact_sha256": (
                _hash64(source_artifact_sha256, "legacy source artifact sha256")
                if source_artifact_sha256 is not None
                else canonical_sha256(dict(payload))
            ),
            "source_hash_basis": source_hash_basis,
            "transformation_id": "legacy_point_audit_to_physical_evidence",
            "transformation_version": "1.0.0",
            "semantics": "preserved_without_reinterpretation",
            "output_mode": "new_artifact",
        }
        transformation = dict(migration)
        transformation["target_schema_name"] = PHYSICAL_EVIDENCE_SCHEMA_NAME
        transformation["target_schema_version"] = PHYSICAL_EVIDENCE_SCHEMA_VERSION
        migration["transformation_sha256"] = canonical_sha256(transformation)
        envelope["migration"] = migration
    return build_physical_evidence(envelope)


def build_point_audit_physical_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Build the native envelope emitted by a current point-audit run."""
    return _point_audit_envelope(payload, migrated=False)


def migrate_legacy_point_audit(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Read a schema-1 point audit without promoting old labels to measurements."""
    return _point_audit_envelope(payload, migrated=True)


def migrate_legacy_point_audit_file(
    source_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    """Write one migrated artifact to a distinct new file without altering source bytes."""
    source = Path(source_path).resolve()
    output = Path(output_path).resolve()
    if source == output:
        raise ValueError("migration output must be distinct from the source artifact")
    source_bytes = source.read_bytes()
    if len(source_bytes) > MAX_CONTRACT_BYTES:
        raise ValueError(f"legacy source artifact exceeds {MAX_CONTRACT_BYTES} bytes")
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    try:
        payload = json.loads(source_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("legacy source artifact must be UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("legacy source artifact must contain one JSON object")
    migrated = _point_audit_envelope(
        payload,
        migrated=True,
        source_artifact_sha256=source_sha256,
        source_hash_basis="file_bytes",
    )
    output_bytes = canonical_json_bytes(migrated) + b"\n"
    with output.open("xb") as stream:
        stream.write(output_bytes)
        stream.flush()
        os.fsync(stream.fileno())
    if hashlib.sha256(source.read_bytes()).hexdigest() != source_sha256:
        raise RuntimeError("legacy source artifact changed during migration")
    written = json.loads(output.read_text(encoding="utf-8"))
    if validate_physical_evidence(written) != migrated:
        raise RuntimeError("written migration artifact failed readback validation")
    return deepcopy(migrated)


def read_physical_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Read a native envelope or explicitly migrate a legacy point-audit artifact."""
    document = _require_mapping(dict(payload), "evidence_document")
    if document.get("schema_name") == PHYSICAL_EVIDENCE_SCHEMA_NAME:
        return validate_physical_evidence(document)
    nested = document.get("physical_evidence")
    if isinstance(nested, dict):
        return validate_physical_evidence(nested)
    if "measurement" in document or document.get("schema_version") == "1":
        return migrate_legacy_point_audit(document)
    raise ValueError("document is neither physical_evidence v1 nor a recognized legacy point audit")


def _validate_rule(rule: Any, index: int) -> dict[str, Any]:
    item = _require_mapping(rule, f"validation_policy.rules[{index}]")
    _reject_unknown(item, _RULE_FIELDS, f"validation_policy.rules[{index}]")
    _identifier(item.get("rule_id"), f"validation_policy.rules[{index}].rule_id")
    rule_type = _identifier(item.get("rule_type"), f"validation_policy.rules[{index}].rule_type")
    spec = _RULE_SPECS.get(rule_type)
    if spec is None:
        raise ValueError(f"unsupported validation rule type: {rule_type}")
    required = item.get("required_measurements")
    if not isinstance(required, list) or tuple(required) != spec["required"]:
        raise ValueError(
            f"validation_policy.rules[{index}].required_measurements must exactly equal {list(spec['required'])}"
        )
    tolerances = _require_mapping(item.get("tolerances", {}), f"validation_policy.rules[{index}].tolerances")
    _reject_unknown(tolerances, set(spec["tolerances"]), f"validation_policy.rules[{index}].tolerances")
    missing_tolerances = sorted(set(spec["required_tolerances"]) - set(tolerances))
    if missing_tolerances:
        raise ValueError(f"validation_policy.rules[{index}].tolerances is missing {missing_tolerances}")
    if rule_type == "wavelength_synchronization" and not tolerances:
        raise ValueError("wavelength_synchronization requires absolute_m and/or relative tolerance")
    for name, value in tolerances.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0:
            raise ValueError(f"validation_policy.rules[{index}].tolerances.{name} must be finite and non-negative")
        if name == "minimum_elements" and int(value) != value:
            raise ValueError(f"validation_policy.rules[{index}].tolerances.minimum_elements must be an integer")
    assumptions = _require_mapping(item.get("assumptions", {}), f"validation_policy.rules[{index}].assumptions")
    _reject_unknown(assumptions, set(spec["assumptions"]), f"validation_policy.rules[{index}].assumptions")
    if assumptions != spec["assumptions"]:
        raise ValueError(
            f"validation_policy.rules[{index}].assumptions must exactly equal {spec['assumptions']}"
        )
    return deepcopy(item)


def validate_validation_policy(payload: Any, *, verify_hash: bool = True) -> dict[str, Any]:
    policy = _require_mapping(payload, "validation_policy")
    _reject_unknown(policy, _POLICY_FIELDS, "validation_policy")
    if policy.get("schema_name") != VALIDATION_POLICY_SCHEMA_NAME:
        raise ValueError("validation_policy.schema_name is unsupported")
    if policy.get("schema_version") != VALIDATION_POLICY_SCHEMA_VERSION:
        raise ValueError("validation_policy.schema_version is unsupported")
    _identifier(policy.get("policy_id"), "validation_policy.policy_id")
    rules = policy.get("rules")
    if not isinstance(rules, list) or not rules or len(rules) > MAX_POLICY_RULES:
        raise ValueError(f"validation_policy.rules must contain 1..{MAX_POLICY_RULES} rules")
    normalized_rules = [_validate_rule(rule, index) for index, rule in enumerate(rules)]
    rule_ids = [rule["rule_id"] for rule in normalized_rules]
    if len(rule_ids) != len(set(rule_ids)):
        raise ValueError("validation_policy.rule_id values must be unique")
    supplied_hash = _hash64(policy.get("policy_sha256"), "validation_policy.policy_sha256")
    without_hash = dict(policy)
    without_hash.pop("policy_sha256", None)
    expected_hash = canonical_sha256(without_hash)
    if verify_hash and supplied_hash != expected_hash:
        raise ValueError("validation_policy.policy_sha256 does not match the canonical payload")
    canonical_json_bytes(policy)
    return deepcopy(policy)


def build_validation_policy(payload: Mapping[str, Any]) -> dict[str, Any]:
    policy = deepcopy(dict(payload))
    if "policy_sha256" in policy:
        raise ValueError("build_validation_policy computes policy_sha256; callers must omit it")
    policy["policy_sha256"] = canonical_sha256(policy)
    return validate_validation_policy(policy)


def _rule_outcome(rule: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
    unavailable = []
    values: dict[str, Any] = {}
    states: dict[str, str] = {}
    for name in rule["required_measurements"]:
        record = evidence.get(name)
        state = record.get("state") if isinstance(record, dict) else "unknown"
        states[name] = state
        declared_flux_derived = {
            "flux.incident_positive_power_sign",
            "flux.reflected_positive_power_sign",
            "flux.transmitted_positive_power_sign",
            "flux.incident_power_w",
            "flux.reflected_power_w",
            "flux.transmitted_power_w",
            "flux.R",
            "flux.T",
            "flux.A",
            "flux.closure_abs",
            "flux.convention_complete",
            "flux.physical_flux_closure_eligible",
        }
        accepted_states = (
            {"measured", "derived_from_declared_convention"}
            if rule["rule_type"] == "declared_flux_closure" and name in declared_flux_derived
            else {"measured"}
        )
        if state not in accepted_states:
            unavailable.append({"measurement": name, "state": state})
        else:
            values[name] = record.get("value")
    result = {
        "rule_id": rule["rule_id"],
        "rule_type": rule["rule_type"],
        "required_measurement_states": states,
    }
    if unavailable:
        result.update({"outcome": "missing", "reason": "required measured evidence is unavailable", "unavailable": unavailable})
        return result

    tolerances = rule["tolerances"]
    rule_type = rule["rule_type"]
    try:
        if rule_type == "passive_rta_bounds":
            margin = float(tolerances["margin"])
            measured = {name: float(values[f"power.{name}"]) for name in ("R", "T", "A")}
            passed = all(-margin <= value <= 1.0 + margin for value in measured.values())
            detail = {"measured": measured, "threshold": {"minimum": -margin, "maximum": 1.0 + margin}}
        elif rule_type == "wavelength_synchronization":
            left = float(values["wavelength.evaluated_parameter_m"])
            right = float(values["wavelength.solved_frequency_m"])
            absolute = abs(left - right)
            relative = None if right == 0 else absolute / abs(right)
            checks = []
            if "absolute_m" in tolerances:
                checks.append(absolute <= float(tolerances["absolute_m"]))
            if "relative" in tolerances:
                checks.append(relative is not None and relative <= float(tolerances["relative"]))
            passed = all(checks)
            detail = {"measured": {"absolute_m": absolute, "relative": relative}, "threshold": tolerances}
        elif rule_type == "declared_flux_closure":
            incident_raw = float(values["flux.incident_raw_power_w"])
            reflected_raw = float(values["flux.reflected_raw_power_w"])
            transmitted_raw = float(values["flux.transmitted_raw_power_w"])
            incident_sign = int(values["flux.incident_positive_power_sign"])
            reflected_sign = int(values["flux.reflected_positive_power_sign"])
            transmitted_sign = int(values["flux.transmitted_positive_power_sign"])
            incident = float(values["flux.incident_power_w"])
            reflected = float(values["flux.reflected_power_w"])
            transmitted = float(values["flux.transmitted_power_w"])
            r_value = float(values["flux.R"])
            t_value = float(values["flux.T"])
            a_value = float(values["flux.A"])
            closure = float(values["flux.closure_abs"])
            margin = float(tolerances["margin"])
            convention_complete = values["flux.convention_complete"] is True
            closure_eligible = values["flux.physical_flux_closure_eligible"] is True
            finite = all(
                math.isfinite(value)
                for value in (
                    incident_raw,
                    reflected_raw,
                    transmitted_raw,
                    incident,
                    reflected,
                    transmitted,
                    r_value,
                    t_value,
                    a_value,
                    closure,
                )
            )
            signs_valid = all(sign in {-1, 1} for sign in (incident_sign, reflected_sign, transmitted_sign))
            passive_bounds = all(
                -margin <= value <= 1.0 + margin
                for value in (r_value, t_value, a_value)
            )
            arithmetic_consistent = (
                incident > 0.0
                and signs_valid
                and math.isclose(incident_raw * incident_sign, incident, rel_tol=1e-12, abs_tol=1e-15)
                and math.isclose(reflected_raw * reflected_sign, reflected, rel_tol=1e-12, abs_tol=1e-15)
                and math.isclose(transmitted_raw * transmitted_sign, transmitted, rel_tol=1e-12, abs_tol=1e-15)
                and math.isclose(reflected / incident, r_value, rel_tol=1e-12, abs_tol=1e-15)
                and math.isclose(transmitted / incident, t_value, rel_tol=1e-12, abs_tol=1e-15)
                and math.isclose(
                    (incident - reflected - transmitted) / incident,
                    a_value,
                    rel_tol=1e-12,
                    abs_tol=1e-15,
                )
            )
            passed = (
                finite
                and convention_complete
                and closure_eligible
                and passive_bounds
                and arithmetic_consistent
                and closure <= float(tolerances["closure_abs"])
            )
            detail = {
                "measured": {
                    "incident_power_w": incident,
                    "reflected_power_w": reflected,
                    "transmitted_power_w": transmitted,
                    "R": r_value,
                    "T": t_value,
                    "A": a_value,
                    "closure_abs": closure,
                    "convention_complete": convention_complete,
                    "physical_flux_closure_eligible": closure_eligible,
                    "raw_power_w": {
                        "incident": incident_raw,
                        "reflected": reflected_raw,
                        "transmitted": transmitted_raw,
                    },
                    "positive_power_sign": {
                        "incident": incident_sign,
                        "reflected": reflected_sign,
                        "transmitted": transmitted_sign,
                    },
                },
                "threshold": tolerances,
                "checks": {
                    "finite": finite,
                    "passive_bounds": passive_bounds,
                    "arithmetic_consistent": arithmetic_consistent,
                    "signs_valid": signs_valid,
                    "convention_complete": convention_complete,
                    "physical_flux_closure_eligible": closure_eligible,
                },
            }
        elif rule_type == "reference_air_polarization_ratio":
            ratio = float(values["polarization.target_to_transverse_ratio"])
            method_valid = values["polarization.reference_air_method_valid"] is True
            passed = math.isfinite(ratio) and method_valid and ratio >= float(tolerances["minimum_ratio"])
            detail = {
                "measured": {"target_to_transverse_ratio": ratio, "reference_air_method_valid": method_valid},
                "threshold": tolerances["minimum_ratio"],
            }
        elif rule_type == "mesh_evidence_presence":
            count = int(values["mesh.element_count"])
            minimum = int(tolerances.get("minimum_elements", 1))
            passed = count >= minimum
            detail = {"measured": count, "threshold": minimum}
        else:  # pragma: no cover - guarded by strict validation
            raise ValueError(f"unsupported rule type: {rule_type}")
    except (TypeError, ValueError, KeyError) as exc:
        result.update({"outcome": "missing", "reason": f"measured evidence has invalid shape: {exc}"})
        return result
    result.update({"outcome": "pass" if passed else "fail", **detail})
    return result


def evaluate_physical_evidence_policy(evidence: Any, policy: Any) -> dict[str, Any]:
    envelope = validate_physical_evidence(evidence)
    strict_policy = validate_validation_policy(policy)
    outcomes = [_rule_outcome(rule, envelope["evidence"]) for rule in strict_policy["rules"]]
    states = [item["outcome"] for item in outcomes]
    overall = "fail" if "fail" in states else ("missing" if "missing" in states else "pass")
    return {
        "mode": "strict_physical_evidence_policy",
        "overall": overall,
        "policy_sha256": strict_policy["policy_sha256"],
        "evidence_sha256": envelope["contract_sha256"],
        "rules": outcomes,
    }


def example_validation_policies() -> dict[str, dict[str, Any]]:
    """Return portable templates; callers must choose project tolerances explicitly."""
    templates = {
        "passive_rta_bounds": {
            "rule_type": "passive_rta_bounds",
            "required_measurements": list(_RULE_SPECS["passive_rta_bounds"]["required"]),
            "tolerances": {"margin": 0.0},
            "assumptions": {"passive": True, "power_normalized": True},
        },
        "wavelength_synchronization": {
            "rule_type": "wavelength_synchronization",
            "required_measurements": list(_RULE_SPECS["wavelength_synchronization"]["required"]),
            "tolerances": {"absolute_m": 0.0, "relative": 0.0},
            "assumptions": {},
        },
        "declared_flux_closure": {
            "rule_type": "declared_flux_closure",
            "required_measurements": list(_RULE_SPECS["declared_flux_closure"]["required"]),
            "tolerances": {"closure_abs": 0.0, "margin": 0.0},
            "assumptions": {"sign_convention_declared": True, "plane_medium_declared": True},
        },
        "reference_air_polarization_ratio": {
            "rule_type": "reference_air_polarization_ratio",
            "required_measurements": list(_RULE_SPECS["reference_air_polarization_ratio"]["required"]),
            "tolerances": {"minimum_ratio": 1.0},
            "assumptions": {},
        },
        "mesh_evidence_presence": {
            "rule_type": "mesh_evidence_presence",
            "required_measurements": list(_RULE_SPECS["mesh_evidence_presence"]["required"]),
            "tolerances": {"minimum_elements": 1},
            "assumptions": {},
        },
    }
    return {
        name: build_validation_policy(
            {
                "schema_name": VALIDATION_POLICY_SCHEMA_NAME,
                "schema_version": VALIDATION_POLICY_SCHEMA_VERSION,
                "policy_id": f"example.{name}",
                "rules": [{"rule_id": name, **rule}],
            }
        )
        for name, rule in templates.items()
    }


__all__ = [
    "EVIDENCE_STATES",
    "PHYSICAL_EVIDENCE_SCHEMA_NAME",
    "PHYSICAL_EVIDENCE_READABLE_VERSIONS",
    "PHYSICAL_EVIDENCE_SCHEMA_VERSION",
    "VALIDATION_POLICY_SCHEMA_NAME",
    "VALIDATION_POLICY_SCHEMA_VERSION",
    "build_physical_evidence",
    "build_point_audit_physical_evidence",
    "build_validation_policy",
    "canonical_json_bytes",
    "canonical_sha256",
    "evaluate_physical_evidence_policy",
    "example_validation_policies",
    "migrate_legacy_point_audit",
    "migrate_legacy_point_audit_file",
    "read_physical_evidence",
    "validate_physical_evidence",
    "validate_validation_policy",
]
