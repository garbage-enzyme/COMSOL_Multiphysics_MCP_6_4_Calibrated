"""Versioned offline inspection summaries for `.mph` archives.

The summary only reports values that are explicitly declared inside the
archive markers (`fileversion`, `modelinfo.xml`, `usedlicenses.txt`,
`fileids.xml`, `dmodel.xml`). Ambiguous, conflicting, or missing declarations
fail closed instead of being guessed. The builder never starts COMSOL, Java,
MPh, or JPype.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any

from comsol_mcp.contracts.mph_inspection import MphInspectionLimits
from comsol_mcp.durable import canonical_sha256_v1
from comsol_mcp.evidence.inspection.archive import (
    MphInspectionError,
    entry_map,
    inspect_archive_inventory,
    read_bounded_member,
    size_breakdown,
)

MPH_INSPECTION_SUMMARY_SCHEMA_NAME = "comsol_mcp.mph_inspection_summary"
MPH_INSPECTION_SUMMARY_SCHEMA_VERSION = "1.0.0"

_FILEVERSION_PATTERN = re.compile(r"\A(\d{1,8}):COMSOL (\d+\.\d+(?:\.\d+)*)\n?\Z")
_MARKER_XML_LIMIT_BYTES = 26_214_144
_MODEL_ROOT_TAG = "Model"
_TAG_CONTAINERS = (
    ("PhysicsList", "Physics", True),
    ("MaterialList", "Material", False),
    ("StudyList", "Study", False),
    ("SolverSequenceList", "SolverSequence", False),
    ("GeomList", "Geom", False),
    ("MeshList", "Mesh", False),
)


def _marker_bytes(
    file_path: str | Path,
    available: set[str],
    member: str,
) -> bytes:
    if member not in available:
        raise MphInspectionError(
            "mph_unsupported_format",
            "required COMSOL archive markers are missing",
        )
    return read_bounded_member(file_path, member, max_bytes=_MARKER_XML_LIMIT_BYTES)


def _decode_marker(payload: bytes, member: str) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MphInspectionError(
            "mph_marker_encoding_invalid",
            f"archive marker {member} is not valid UTF-8",
        ) from exc


def _parse_file_version(payload: bytes) -> tuple[str, str]:
    text = _decode_marker(payload, "fileversion")
    match = _FILEVERSION_PATTERN.match(text)
    if match is None:
        raise MphInspectionError(
            "mph_version_marker_ambiguous",
            "the fileversion marker does not declare exactly one COMSOL version",
        )
    return match.group(1), match.group(2)


def _parse_xml(payload: bytes, member: str) -> ElementTree.Element:
    # The stdlib parser cannot resolve external entities, and internal entity
    # expansion is refused below before parsing: bounded marker bytes cannot
    # grow into unbounded memory through a DTD.
    folded = payload.lower()
    if b"<!doctype" in folded or b"<!entity" in folded:
        raise MphInspectionError(
            "mph_marker_dtd_rejected",
            f"archive marker {member} declares a DTD or entity; markers are data-only",
        )
    try:
        # Justified S314: input is byte-bounded, DTD/entity-free by the guard
        # above, parsed read-only, and never evaluated or rendered as markup.
        return ElementTree.fromstring(payload)  # noqa: S314
    except ElementTree.ParseError as exc:
        raise MphInspectionError(
            "mph_marker_xml_invalid",
            f"archive marker {member} is not well-formed XML",
        ) from exc


def _bounded_text(value: Any, *, limit: int = 16_384) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) > limit:
        return text[:limit]
    return text


def _tag_rows(
    model_root: ElementTree.Element,
    limits: MphInspectionLimits,
    warnings: list[str],
) -> dict[str, list[dict[str, Any]]]:
    collected: dict[str, list[dict[str, Any]]] = {}
    for container_name, child_name, include_op in _TAG_CONTAINERS:
        container = model_root.find(container_name)
        rows: list[dict[str, Any]] = []
        if container is not None:
            for child in container.findall(child_name):
                row: dict[str, Any] = {
                    "tag": _bounded_text(child.get("tag")),
                    "name": _bounded_text(child.get("name")),
                }
                if include_op:
                    row["op"] = _bounded_text(child.get("op"))
                rows.append(row)
                if len(rows) >= limits.max_tag_entries:
                    warnings.append("tag_list_truncated")
                    break
        collected[child_name.lower()] = sorted(rows, key=lambda item: item["tag"])
    return collected


def _parameter_rows(
    model_root: ElementTree.Element,
    limits: MphInspectionLimits,
    warnings: list[str],
) -> dict[str, Any]:
    container = model_root.find("ModelParam")
    rows: list[dict[str, Any]] = []
    truncated = False
    if container is not None:
        for child in container.findall("expressions"):
            name = child.get("name") or ""
            expression = child.get("expr") or ""
            if not name:
                continue
            rows.append({"name": _bounded_text(name), "expression": _bounded_text(expression)})
            if len(rows) >= limits.max_parameters:
                truncated = True
                warnings.append("parameter_list_truncated")
                break
    rows.sort(key=lambda item: item["name"])
    return {
        "count": len(rows),
        "truncated": truncated,
        "parameters": rows,
    }


def _license_rows(model_info: ElementTree.Element) -> list[str]:
    products: set[str] = set()
    license_element = model_info.find("licenseInfo")
    if license_element is not None:
        raw = _bounded_text(license_element.get("products"))
        products.update(item for item in raw.split("##") if item)
    return sorted(products)


def _declared_licenses(payload: bytes | None) -> list[str]:
    if payload is None:
        return []
    lines = [line.strip() for line in payload.decode("utf-8", "replace").splitlines()]
    return sorted({line for line in lines if line})


def build_mph_inspection_summary(
    file_path: str | Path,
    limits: MphInspectionLimits | None = None,
) -> dict[str, Any]:
    """Build one bounded path-redacted inspection summary without a solver."""
    bounds = limits or MphInspectionLimits()
    path = Path(file_path)
    inventory = inspect_archive_inventory(path, bounds)
    members = entry_map(inventory)
    available = set(members)

    schema_marker, declared_version = _parse_file_version(
        _marker_bytes(path, available, "fileversion")
    )

    model_info_payload = _marker_bytes(path, available, "modelinfo.xml")
    model_info = _parse_xml(model_info_payload, "modelinfo.xml")
    if model_info.tag != "modelInfo":
        raise MphInspectionError(
            "mph_unsupported_format",
            "modelinfo.xml does not declare a COMSOL modelInfo root",
        )
    info_version = _bounded_text(model_info.get("comsolVersion"))
    if not info_version:
        raise MphInspectionError(
            "mph_version_marker_ambiguous",
            "modelinfo.xml does not declare a comsolVersion",
        )
    if info_version != declared_version:
        raise MphInspectionError(
            "mph_conflicting_version_markers",
            "fileversion and modelinfo.xml declare different COMSOL versions",
        )
    runnable_state = _bounded_text(model_info.get("isRunnable")).casefold()
    if runnable_state not in {"true", "false"}:
        raise MphInspectionError(
            "mph_runnable_state_ambiguous",
            "modelinfo.xml declares an unknown runnable state",
        )
    node_type = _bounded_text(model_info.get("modelType"))
    solved_state = _bounded_text(model_info.get("nodeType"))

    model_payload = _marker_bytes(path, available, "dmodel.xml")
    model_root = _parse_xml(model_payload, "dmodel.xml")
    if model_root.tag != _MODEL_ROOT_TAG:
        raise MphInspectionError(
            "mph_unsupported_format",
            "dmodel.xml does not declare a COMSOL Model root",
        )

    used_licenses_member = "usedlicenses.txt"
    used_licenses = _declared_licenses(
        read_bounded_member(path, used_licenses_member, max_bytes=65_536)
        if used_licenses_member in available
        else None
    )
    required_licenses = _license_rows(model_info)
    warnings: list[str] = []
    if used_licenses and required_licenses and used_licenses != required_licenses:
        warnings.append("license_marker_sets_differ")

    tags = _tag_rows(model_root, bounds, warnings)

    geometry_rows: list[dict[str, Any]] = []
    geometry_element = model_info.find("geometryInfo")
    if geometry_element is not None:
        for geom in geometry_element.findall("geom"):
            dimension_text = _bounded_text(geom.get("dimension"))
            geometry_rows.append(
                {
                    "tag": _bounded_text(geom.get("tag")),
                    "dimension": int(dimension_text) if dimension_text.isdigit() else None,
                }
            )
    if len(geometry_rows) < len(tags["geom"]):
        known = {row["tag"] for row in geometry_rows}
        geometry_rows.extend(
            {"tag": row["tag"], "dimension": None}
            for row in tags["geom"]
            if row["tag"] not in known
        )
    geometry_rows.sort(key=lambda item: item["tag"])

    savepoint_members = sorted(
        entry.name for entry in inventory.entries if entry.name.startswith("savepoint")
    )
    fileids_root = None
    if "fileids.xml" in available:
        fileids_root = _parse_xml(
            _marker_bytes(path, available, "fileids.xml"),
            "fileids.xml",
        )
    savepoint_rows: list[dict[str, Any]] = []
    binary_resources: list[dict[str, Any]] = []
    if fileids_root is not None:
        for element in fileids_root.findall("SavePoint"):
            savepoint_rows.append(
                {
                    "tag": _bounded_text(element.get("tag")),
                    "fileid": _bounded_text(element.get("fileid")),
                    "purgeable": _bounded_text(element.get("purgeable")).casefold() == "true",
                }
            )
        for element in fileids_root.findall("BinaryResource"):
            binary_resources.append(
                {
                    "file": _bounded_text(element.get("file")),
                    "binarytype": _bounded_text(element.get("binarytype")),
                }
            )
    savepoint_rows.sort(key=lambda item: item["tag"])
    binary_resources.sort(key=lambda item: item["file"])

    parameters = _parameter_rows(model_root, bounds, warnings)
    preview_state = "savepoint_present" if savepoint_members else "no_savepoint"

    summary: dict[str, Any] = {
        "schema_name": MPH_INSPECTION_SUMMARY_SCHEMA_NAME,
        "schema_version": MPH_INSPECTION_SUMMARY_SCHEMA_VERSION,
        "source_path_redacted": "**/" + path.name,
        "sha256": inventory.sha256,
        "byte_size": inventory.byte_size,
        "zip_valid": True,
        "format_family": "mph_zip",
        "comsol_version": declared_version,
        "schema_marker": schema_marker,
        "node_type": node_type,
        "runnable_state": runnable_state,
        "solved_state": solved_state,
        "preview_state": preview_state,
        "title": _bounded_text(model_info.get("title")),
        "description": _bounded_text(model_info.get("description")),
        "model_tags": [_MODEL_ROOT_TAG],
        "parameter_summary": parameters,
        "physics_tags": tags["physics"],
        "study_tags": tags["study"],
        "material_tags": tags["material"],
        "solution_tags": tags["solversequence"],
        "geometry_summary": {"geoms": geometry_rows},
        "mesh_summary": {
            "mesh_tags": tags["mesh"],
            "binary_resources": [item for item in binary_resources if item["binarytype"] == "MESH"],
        },
        "savepoint_summary": {
            "present": bool(savepoint_members),
            "savepoints": savepoint_rows,
            "members": savepoint_members,
        },
        "entry_count": sum(1 for entry in inventory.entries if not entry.is_directory),
        "bounded_bytes": size_breakdown(inventory),
        "warnings": sorted(set(warnings)),
        "inspection_fingerprint": "",
    }

    body = dict(summary)
    fingerprint_payload = canonical_sha256_v1(body)
    summary["inspection_fingerprint"] = fingerprint_payload
    return summary


def summarize_manifest_json(payload: bytes) -> dict[str, Any]:
    """Parse one auxiliary bounded JSON marker for diagnostics."""
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MphInspectionError(
            "mph_marker_encoding_invalid",
            "an auxiliary JSON marker is not valid UTF-8 JSON",
        ) from exc
    if not isinstance(decoded, dict):
        raise MphInspectionError(
            "mph_marker_xml_invalid",
            "an auxiliary JSON marker must contain an object",
        )
    return decoded


__all__ = [
    "MPH_INSPECTION_SUMMARY_SCHEMA_NAME",
    "MPH_INSPECTION_SUMMARY_SCHEMA_VERSION",
    "build_mph_inspection_summary",
    "summarize_manifest_json",
]
