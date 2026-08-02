"""Hash-chained stage evidence for thermo-optomechanical replay jobs."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping

from .journal import locked_journal, recover_jsonl_tail
from .store import read_json
from .thermo_optomechanical_replay import THERMO_OPTOMECHANICAL_STAGES

THERMO_OPTOMECHANICAL_STAGE_SCHEMA_NAME = "comsol_mcp.thermo_optomechanical_stage"
THERMO_OPTOMECHANICAL_STAGE_SCHEMA_VERSION = "1.0.0"
MAX_THERMO_OPTOMECHANICAL_STAGE_ROW_BYTES = 256 * 1024
MAX_STAGE_EVIDENCE_BYTES = 2 * 1024 * 1024


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


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _exact(value: object, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{name} fields are invalid")
    return dict(value)


def _validate_common_evidence(
    value: object,
    spec: Mapping[str, Any],
    stage_id: str,
) -> dict[str, Any]:
    evidence = _exact(
        value,
        {
            "schema_name",
            "schema_version",
            "stage_id",
            "execution_state",
            "spec_fingerprint",
            "source_model_sha256",
            "optical_configuration_sha256",
            "material_ledger_sha256",
            "payload",
            "evidence_sha256",
        },
        "thermo-optomechanical stage evidence",
    )
    if (
        evidence["schema_name"] != THERMO_OPTOMECHANICAL_STAGE_SCHEMA_NAME
        or evidence["schema_version"] != THERMO_OPTOMECHANICAL_STAGE_SCHEMA_VERSION
        or evidence["stage_id"] != stage_id
        or evidence["execution_state"] != "completed"
        or evidence["spec_fingerprint"] != spec["spec_fingerprint"]
        or evidence["source_model_sha256"] != spec["source_model_sha256"]
        or evidence["optical_configuration_sha256"] != spec["optical_configuration_sha256"]
        or evidence["material_ledger_sha256"] != spec["material_ledger_sha256"]
    ):
        raise ValueError("thermo-optomechanical stage evidence identity is invalid")
    body = dict(evidence)
    supplied = body.pop("evidence_sha256")
    if not isinstance(supplied, str) or _fingerprint(body) != supplied:
        raise ValueError("thermo-optomechanical stage evidence hash does not match")
    _validate_stage_payload(stage_id, evidence["payload"], spec)
    return evidence


def _validate_stage_payload(stage_id: str, value: object, spec: Mapping[str, Any]) -> None:
    if stage_id == "preflight":
        payload = _exact(
            value,
            {
                "required_products",
                "available_products",
                "interface_tags",
                "selection_readback",
                "temperature_unit_readback",
                "material_state_id",
                "material_state_readback",
                "source_unchanged",
                "rollback_available",
            },
            "preflight payload",
        )
        required = payload["required_products"]
        available = payload["available_products"]
        if (
            not isinstance(required, list)
            or not required
            or len(required) > 8
            or not all(isinstance(item, str) and item for item in required)
            or not isinstance(available, list)
            or not set(required) <= set(available)
        ):
            raise ValueError("preflight product evidence is incomplete")
        if payload["temperature_unit_readback"] != "K":
            raise ValueError("preflight temperature unit must read back as kelvin")
        if payload["material_state_id"] != spec["material_state_id"]:
            raise ValueError("preflight material state does not match the job")
        material = _exact(
            payload["material_state_readback"],
            {
                "ledger_sha256",
                "state_id",
                "classification",
                "target",
                "property_value_type",
                "property_values",
                "function_tags",
                "application_receipt_sha256",
            },
            "material state readback",
        )
        state = spec["material_state"]
        if (
            material["ledger_sha256"] != state["ledger_sha256"]
            or material["state_id"] != state["state_id"]
            or material["classification"] != state["classification"]
            or material["target"] != state["target"]
            or material["property_values"] != state["expected_property_values"]
            or material["function_tags"] != state["expected_function_tags"]
            or material["application_receipt_sha256"] != state["application_receipt_sha256"]
            or not isinstance(material["property_value_type"], str)
            or not material["property_value_type"]
        ):
            raise ValueError("preflight material state readback does not match the job")
        if payload["source_unchanged"] is not True or payload["rollback_available"] is not True:
            raise ValueError("preflight immutability or rollback evidence is incomplete")
        contract = spec["model_contract"]
        tags = payload["interface_tags"]
        expected_tags = {
            "heat_transfer": contract["heat_transfer_tag"],
            "solid_mechanics": contract["solid_mechanics_tag"],
            "moving_mesh": contract["moving_mesh_tag"],
            "wave_optics": contract["wave_optics_tag"],
        }
        if tags != expected_tags:
            raise ValueError("preflight interface tags do not match the immutable contract")
        selections = payload["selection_readback"]
        if (
            not isinstance(selections, Mapping)
            or set(selections)
            != {
                "heated_domain",
                "structural_domain",
                "fixed_boundary",
                "thermal_boundary",
                "optical_domain",
            }
            or any(
                not isinstance(item, Mapping)
                or set(item) != {"tag", "entity_count"}
                or not isinstance(item["tag"], str)
                or isinstance(item["entity_count"], bool)
                or not isinstance(item["entity_count"], int)
                or item["entity_count"] <= 0
                for item in selections.values()
            )
        ):
            raise ValueError("preflight selection readback is incomplete")
        return
    if stage_id == "thermal_structural_solve":
        payload = _exact(
            value,
            {"temperature", "displacement", "stress", "energy_balance", "expansion"},
            "thermal-structural payload",
        )
        temperature = _exact(payload["temperature"], {"minimum_K", "maximum_K"}, "temperature")
        if _finite(temperature["maximum_K"], "maximum temperature") < _finite(
            temperature["minimum_K"], "minimum temperature"
        ):
            raise ValueError("temperature extrema are reversed")
        displacement = _exact(
            payload["displacement"], {"maximum_m", "delta_length_m", "frame"}, "displacement"
        )
        if (
            displacement["frame"] != "spatial"
            or _finite(displacement["maximum_m"], "maximum displacement") < 0
        ):
            raise ValueError("displacement evidence is invalid")
        stress = _exact(payload["stress"], {"maximum_abs_Pa"}, "stress")
        if _finite(stress["maximum_abs_Pa"], "maximum stress") < 0:
            raise ValueError("stress evidence is invalid")
        energy = _exact(
            payload["energy_balance"],
            {"source_W", "loss_W", "residual_W", "relative_residual"},
            "energy balance",
        )
        if any(not math.isfinite(_finite(item, "energy value")) for item in energy.values()):
            raise ValueError("energy evidence is invalid")
        expansion = _exact(
            payload["expansion"],
            {
                "coefficient_input_type",
                "coefficient_per_K",
                "reference_temperature_K",
                "expected_delta_length_m",
                "observed_delta_length_m",
                "relative_error",
            },
            "expansion",
        )
        declared = spec["thermal_expansion"]
        if (
            expansion["coefficient_input_type"] != declared["coefficient_input_type"]
            or _finite(expansion["coefficient_per_K"], "coefficient")
            != declared["coefficient_per_K"]
            or _finite(expansion["reference_temperature_K"], "reference temperature")
            != declared["reference_temperature_K"]
        ):
            raise ValueError("thermal expansion readback differs from the job")
        return
    if stage_id == "state_evidence":
        payload = _exact(
            value,
            {"mesh", "frame", "deformation_scale", "displacement_to_length"},
            "state evidence payload",
        )
        mesh = _exact(
            payload["mesh"],
            {
                "identity_sha256",
                "element_count",
                "vertex_count",
                "minimum_quality",
                "inverted_element_count",
            },
            "mesh evidence",
        )
        if (
            any(
                isinstance(mesh[key], bool) or not isinstance(mesh[key], int) or mesh[key] <= 0
                for key in ("element_count", "vertex_count")
            )
            or mesh["inverted_element_count"] != 0
            or not 0.0 < _finite(mesh["minimum_quality"], "mesh quality") <= 1.0
        ):
            raise ValueError("mesh evidence is invalid")
        frame = _exact(
            payload["frame"],
            {"identity_sha256", "displacement_frame", "topology_unchanged"},
            "frame evidence",
        )
        if frame["displacement_frame"] != "spatial" or frame["topology_unchanged"] is not True:
            raise ValueError("frame evidence is invalid")
        _finite(payload["deformation_scale"], "deformation scale")
        _finite(payload["displacement_to_length"], "displacement ratio")
        return
    if stage_id == "deformation_transfer":
        payload = _exact(
            value,
            {
                "method",
                "material_frame_semantics",
                "source_geometry_sha256",
                "deformed_geometry_sha256",
                "readback_exact",
                "rollback_verified",
            },
            "deformation transfer payload",
        )
        if (
            payload["method"] != spec["deformation_transfer"]["method"]
            or payload["material_frame_semantics"] != "spatial_deformation_preserves_material"
            or payload["readback_exact"] is not True
            or payload["rollback_verified"] is not True
            or payload["source_geometry_sha256"] == payload["deformed_geometry_sha256"]
        ):
            raise ValueError("deformation transfer evidence is invalid")
        return
    if stage_id == "optical_replay":
        payload = _exact(
            value,
            {"rows", "control_results", "source_unchanged", "derived_model_sha256"},
            "optical replay payload",
        )
        rows = payload["rows"]
        expected_count = spec["declared_optical_point_count"]
        if not isinstance(rows, list) or len(rows) != expected_count:
            raise ValueError("optical replay row count differs from the exact coordinate grid")
        expected = {
            (wavelength, branch)
            for wavelength in spec["optical_replay"]["wavelengths_m"]
            for branch in spec["optical_replay"]["branches"]
        }
        observed = set()
        for row_value in rows:
            row = _exact(
                row_value,
                {
                    "requested_wavelength_m",
                    "solved_wavelength_m",
                    "branch",
                    "baseline_rta",
                    "deformed_rta",
                },
                "optical replay row",
            )
            requested = _finite(row["requested_wavelength_m"], "requested wavelength")
            solved = _finite(row["solved_wavelength_m"], "solved wavelength")
            if requested != solved:
                raise ValueError("optical replay wavelength readback is not exact")
            observed.add((requested, row["branch"]))
            for name in ("baseline_rta", "deformed_rta"):
                rta = _exact(row[name], {"R", "T", "A", "closure_residual", "passive"}, name)
                values = [_finite(rta[key], f"{name}.{key}") for key in ("R", "T", "A")]
                if any(item < -1.0e-10 for item in values) or rta["passive"] is not True:
                    raise ValueError("optical replay contains non-passive R/T/A")
        if observed != expected:
            raise ValueError("optical replay coordinates differ from the declared grid")
        controls = payload["control_results"]
        if (
            not isinstance(controls, list)
            or len(controls) != len(spec["validation_controls"])
            or {item.get("control_id") for item in controls if isinstance(item, Mapping)}
            != set(spec["validation_controls"])
            or any(
                not isinstance(item, Mapping)
                or set(item) != {"control_id", "passed", "reason_code"}
                or item["passed"] is not True
                or not isinstance(item["reason_code"], str)
                or not item["reason_code"]
                for item in controls
            )
        ):
            raise ValueError("thermo-optomechanical control matrix is incomplete")
        if payload["source_unchanged"] is not True:
            raise ValueError("optical replay did not preserve the immutable source")
        return
    raise ValueError(f"unsupported thermo-optomechanical stage: {stage_id}")


def build_stage_evidence(
    spec: Mapping[str, Any], stage_id: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    if stage_id not in THERMO_OPTOMECHANICAL_STAGES:
        raise ValueError("stage_id is unsupported")
    body = {
        "schema_name": THERMO_OPTOMECHANICAL_STAGE_SCHEMA_NAME,
        "schema_version": THERMO_OPTOMECHANICAL_STAGE_SCHEMA_VERSION,
        "stage_id": stage_id,
        "execution_state": "completed",
        "spec_fingerprint": spec["spec_fingerprint"],
        "source_model_sha256": spec["source_model_sha256"],
        "optical_configuration_sha256": spec["optical_configuration_sha256"],
        "material_ledger_sha256": spec["material_ledger_sha256"],
        "payload": dict(payload),
    }
    evidence = {**body, "evidence_sha256": _fingerprint(body)}
    return _validate_common_evidence(evidence, spec, stage_id)


def _artifact_descriptor(path: Path, root: Path) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("thermo-optomechanical evidence escapes the job directory") from exc
    return {
        "relative_path": relative,
        "sha256": _sha256_file(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def _load_stage_evidence(root: Path, stage_id: str, spec: Mapping[str, Any]) -> dict[str, Any]:
    path = root / stage_id / "evidence.json"
    if not path.is_file() or path.stat().st_size > MAX_STAGE_EVIDENCE_BYTES:
        raise ValueError("completed thermo-optomechanical stage evidence is missing or oversized")
    return _validate_common_evidence(read_json(path), spec, stage_id)


def _validate_row(
    value: object,
    spec: Mapping[str, Any],
    *,
    ordinal: int,
    previous_row_sha256: str | None,
    artifact_root: Path,
) -> dict[str, Any]:
    row = _exact(
        value,
        {
            "schema_name",
            "schema_version",
            "spec_fingerprint",
            "attempt",
            "ordinal",
            "stage_id",
            "execution_state",
            "evidence_sha256",
            "evidence_artifact",
            "previous_row_sha256",
            "row_sha256",
        },
        "thermo-optomechanical stage row",
    )
    stage_id = THERMO_OPTOMECHANICAL_STAGES[ordinal]
    if (
        row["schema_name"] != THERMO_OPTOMECHANICAL_STAGE_SCHEMA_NAME
        or row["schema_version"] != THERMO_OPTOMECHANICAL_STAGE_SCHEMA_VERSION
        or row["spec_fingerprint"] != spec["spec_fingerprint"]
        or row["ordinal"] != ordinal
        or row["stage_id"] != stage_id
        or row["execution_state"] != "completed"
        or row["previous_row_sha256"] != previous_row_sha256
    ):
        raise ValueError("thermo-optomechanical stage row identity is invalid")
    if (
        isinstance(row["attempt"], bool)
        or not isinstance(row["attempt"], int)
        or row["attempt"] <= 0
    ):
        raise ValueError("thermo-optomechanical stage attempt is invalid")
    evidence = _load_stage_evidence(artifact_root, stage_id, spec)
    descriptor = _artifact_descriptor(artifact_root / stage_id / "evidence.json", artifact_root)
    if (
        row["evidence_sha256"] != evidence["evidence_sha256"]
        or row["evidence_artifact"] != descriptor
    ):
        raise ValueError("thermo-optomechanical stage row does not replay from its artifact")
    body = dict(row)
    supplied = body.pop("row_sha256")
    if _fingerprint(body) != supplied:
        raise ValueError("thermo-optomechanical stage row hash does not match")
    return row


def _read_unlocked(path: Path, spec: Mapping[str, Any], root: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    recover_jsonl_tail(path, max_row_bytes=MAX_THERMO_OPTOMECHANICAL_STAGE_ROW_BYTES)
    if (
        path.stat().st_size
        > len(THERMO_OPTOMECHANICAL_STAGES) * MAX_THERMO_OPTOMECHANICAL_STAGE_ROW_BYTES
    ):
        raise ValueError("thermo-optomechanical stage journal exceeds its bound")
    values = []
    with path.open("rb") as handle:
        for raw_line in handle:
            if len(raw_line) > MAX_THERMO_OPTOMECHANICAL_STAGE_ROW_BYTES:
                raise ValueError("thermo-optomechanical stage row exceeds its bound")
            if raw_line.strip():
                values.append(json.loads(raw_line.decode("utf-8")))
    if len(values) > len(THERMO_OPTOMECHANICAL_STAGES):
        raise ValueError("thermo-optomechanical job has more rows than declared stages")
    rows = []
    previous = None
    for ordinal, value in enumerate(values):
        row = _validate_row(
            value,
            spec,
            ordinal=ordinal,
            previous_row_sha256=previous,
            artifact_root=root,
        )
        rows.append(row)
        previous = row["row_sha256"]
    return rows


def read_thermo_optomechanical_stage_rows(
    path: str | Path, spec: Mapping[str, Any], *, artifact_root: str | Path
) -> list[dict[str, Any]]:
    root = Path(artifact_root).resolve()
    with locked_journal(path) as journal:
        return _read_unlocked(journal, spec, root)


def append_thermo_optomechanical_stage_row(
    path: str | Path,
    spec: Mapping[str, Any],
    *,
    attempt: int,
    artifact_root: str | Path,
) -> dict[str, Any]:
    root = Path(artifact_root).resolve()
    with locked_journal(path) as journal:
        rows = _read_unlocked(journal, spec, root)
        ordinal = len(rows)
        if ordinal >= len(THERMO_OPTOMECHANICAL_STAGES):
            raise ValueError("all thermo-optomechanical stages are already complete")
        stage_id = THERMO_OPTOMECHANICAL_STAGES[ordinal]
        evidence = _load_stage_evidence(root, stage_id, spec)
        body = {
            "schema_name": THERMO_OPTOMECHANICAL_STAGE_SCHEMA_NAME,
            "schema_version": THERMO_OPTOMECHANICAL_STAGE_SCHEMA_VERSION,
            "spec_fingerprint": spec["spec_fingerprint"],
            "attempt": attempt,
            "ordinal": ordinal,
            "stage_id": stage_id,
            "execution_state": "completed",
            "evidence_sha256": evidence["evidence_sha256"],
            "evidence_artifact": _artifact_descriptor(root / stage_id / "evidence.json", root),
            "previous_row_sha256": rows[-1]["row_sha256"] if rows else None,
        }
        row = {**body, "row_sha256": _fingerprint(body)}
        payload = _canonical_bytes(row) + b"\n"
        if len(payload) > MAX_THERMO_OPTOMECHANICAL_STAGE_ROW_BYTES:
            raise ValueError("thermo-optomechanical stage row exceeds its bound")
        with journal.open("ab") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        replayed = _read_unlocked(journal, spec, root)
        if replayed[-1] != row:
            raise RuntimeError("thermo-optomechanical stage row did not replay after append")
        return row


__all__ = [
    "THERMO_OPTOMECHANICAL_STAGE_SCHEMA_NAME",
    "THERMO_OPTOMECHANICAL_STAGE_SCHEMA_VERSION",
    "append_thermo_optomechanical_stage_row",
    "build_stage_evidence",
    "read_thermo_optomechanical_stage_rows",
]
