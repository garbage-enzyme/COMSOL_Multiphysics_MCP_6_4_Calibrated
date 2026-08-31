"""Versioned offline two-file `.mph` diffs.

The diff compares two already-validated bounded inspection summaries and
reports declared metadata, tag, parameter, geometry, savepoint, and
normalized archive-entry differences with deterministic ordering. It is a
metadata observation only: a zero-difference result never proves scientific
equivalence of two solved models. The builder re-hashes both inputs after
comparison to prove neither file changed and never starts COMSOL, Java,
MPh, or JPype.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from comsol_mcp.contracts.mph_inspection import MphInspectionLimits
from comsol_mcp.durable import canonical_sha256_v1
from comsol_mcp.evidence.inspection.archive import (
    MphInspectionError,
    inspect_archive_inventory,
)
from comsol_mcp.evidence.inspection.summary import build_mph_inspection_summary

MPH_DIFF_SCHEMA_NAME = "comsol_mcp.mph_diff"
MPH_DIFF_SCHEMA_VERSION = "1.0.0"

_METADATA_FIELDS = (
    "format_family",
    "comsol_version",
    "schema_marker",
    "node_type",
    "runnable_state",
    "solved_state",
    "preview_state",
    "title",
    "description",
)

_TAG_CATEGORY_FIELDS = {
    "physics": "physics_tags",
    "material": "material_tags",
    "study": "study_tags",
    "solver_sequence": "solution_tags",
}


def _identity(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_path_redacted": summary["source_path_redacted"],
        "sha256": summary["sha256"],
        "byte_size": summary["byte_size"],
        "comsol_version": summary["comsol_version"],
    }


def _rows_by_key(rows: list[dict[str, Any]], key_field: str) -> dict[str, dict[str, Any]]:
    return {str(row[key_field]): row for row in rows if row.get(key_field)}


def _bounded_list(values: list[Any], cap: int, warnings: list[str]) -> list[Any]:
    ordered = sorted(values, key=lambda item: canonical_sha256_v1(item))
    if len(ordered) > cap:
        warnings.append("diff_row_list_truncated")
        return ordered[:cap]
    return ordered


def _changed_rows(
    left_rows: dict[str, dict[str, Any]],
    right_rows: dict[str, dict[str, Any]],
    field: str,
) -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "field": field,
            "left": left_rows[key].get(field),
            "right": right_rows[key].get(field),
        }
        for key in left_rows.keys() & right_rows.keys()
        if left_rows[key].get(field) != right_rows[key].get(field)
    ]


def _row_set_changes(
    left_summary: dict[str, Any],
    right_summary: dict[str, Any],
    extractor: Any,
    key_field: str,
    compare_fields: tuple[str, ...],
    cap: int,
    warnings: list[str],
) -> dict[str, Any]:
    left_rows = _rows_by_key(extractor(left_summary), key_field)
    right_rows = _rows_by_key(extractor(right_summary), key_field)
    return {
        "added": _bounded_list(
            [right_rows[key] for key in right_rows.keys() - left_rows.keys()], cap, warnings
        ),
        "removed": _bounded_list(
            [left_rows[key] for key in left_rows.keys() - right_rows.keys()], cap, warnings
        ),
        "changed": _bounded_list(
            [
                row
                for field in compare_fields
                for row in _changed_rows(left_rows, right_rows, field)
            ],
            cap,
            warnings,
        ),
    }


def _tag_changes(
    left: dict[str, Any],
    right: dict[str, Any],
    cap: int,
    warnings: list[str],
) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for category, field in _TAG_CATEGORY_FIELDS.items():
        changes[category] = _row_set_changes(
            left, right, lambda s, f=field: s[f], "tag", ("name", "op"), cap, warnings
        )
    changes["mesh"] = _row_set_changes(
        left,
        right,
        lambda s: s["mesh_summary"]["mesh_tags"],
        "tag",
        ("name",),
        cap,
        warnings,
    )
    return changes


def _parameter_changes(
    left: dict[str, Any],
    right: dict[str, Any],
    cap: int,
    warnings: list[str],
) -> dict[str, Any]:
    return _row_set_changes(
        left,
        right,
        lambda s: s["parameter_summary"]["parameters"],
        "name",
        ("expression",),
        cap,
        warnings,
    )


def _geometry_changes(
    left: dict[str, Any],
    right: dict[str, Any],
    cap: int,
    warnings: list[str],
) -> dict[str, Any]:
    return _row_set_changes(
        left,
        right,
        lambda s: s["geometry_summary"]["geoms"],
        "tag",
        ("dimension",),
        cap,
        warnings,
    )


def _savepoint_changes(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left_members = set(left["savepoint_summary"]["members"])
    right_members = set(right["savepoint_summary"]["members"])
    return {
        "present_changed": left["savepoint_summary"]["present"]
        != right["savepoint_summary"]["present"],
        "added": sorted(right_members - left_members),
        "removed": sorted(left_members - right_members),
    }


def _entry_changes(
    left_inventory: Any,
    right_inventory: Any,
    cap: int,
    warnings: list[str],
) -> dict[str, Any]:
    left_entries = {entry.name: entry for entry in left_inventory.entries}
    right_entries = {entry.name: entry for entry in right_inventory.entries}
    added = sorted(right_entries.keys() - left_entries.keys())
    removed = sorted(left_entries.keys() - right_entries.keys())
    changed = [
        {
            "name": name,
            "field": "crc_or_size",
            "left": {
                "file_size": left_entries[name].file_size,
                "crc": left_entries[name].crc,
            },
            "right": {
                "file_size": right_entries[name].file_size,
                "crc": right_entries[name].crc,
            },
        }
        for name in left_entries.keys() & right_entries.keys()
        if (left_entries[name].crc, left_entries[name].file_size)
        != (right_entries[name].crc, right_entries[name].file_size)
    ]
    return {
        "added_count": len(added),
        "removed_count": len(removed),
        "added_names": _bounded_list(added, cap, warnings),
        "removed_names": _bounded_list(removed, cap, warnings),
        "changed": _bounded_list(changed, cap, warnings),
    }


def _size_deltas(left_inventory: Any, right_inventory: Any) -> dict[str, int]:
    return {
        "archive_bytes": right_inventory.byte_size - left_inventory.byte_size,
        "entry_count": len(right_inventory.entries) - len(left_inventory.entries),
        "uncompressed_bytes": right_inventory.total_uncompressed_bytes
        - left_inventory.total_uncompressed_bytes,
        "compressed_bytes": right_inventory.total_compressed_bytes
        - left_inventory.total_compressed_bytes,
    }


def build_mph_diff(
    left_path: str | Path,
    right_path: str | Path,
    limits: MphInspectionLimits | None = None,
) -> dict[str, Any]:
    """Compare two offline `.mph` archives without starting a solver."""
    bounds = limits or MphInspectionLimits()
    cap = bounds.max_tag_entries
    warnings: list[str] = []
    left = build_mph_inspection_summary(left_path, bounds)
    right = build_mph_inspection_summary(right_path, bounds)
    left_inventory = inspect_archive_inventory(Path(left_path), bounds)
    right_inventory = inspect_archive_inventory(Path(right_path), bounds)

    metadata_changes = [
        {"field": field, "left": left[field], "right": right[field]}
        for field in _METADATA_FIELDS
        if left[field] != right[field]
    ]
    tags = _tag_changes(left, right, cap, warnings)
    parameters = _parameter_changes(left, right, cap, warnings)
    geometry = _geometry_changes(left, right, cap, warnings)
    savepoints = _savepoint_changes(left, right)
    entries = _entry_changes(left_inventory, right_inventory, cap, warnings)

    row_differences = bool(
        metadata_changes
        or any(parts[key] for parts in tags.values() for key in ("added", "removed", "changed"))
        or any(parameters[key] for key in ("added", "removed", "changed"))
        or any(geometry[key] for key in ("added", "removed", "changed"))
        or any(savepoints[key] for key in ("present_changed", "added", "removed"))
        or entries["added_count"]
        or entries["removed_count"]
        or entries["changed"]
    )

    # Prove neither input was modified by the comparison itself by re-hashing
    # both containers after every read completed.
    for path, observed in (
        (Path(left_path), left["sha256"]),
        (Path(right_path), right["sha256"]),
    ):
        reread = inspect_archive_inventory(path, bounds)
        if reread.sha256 != observed:
            raise MphInspectionError(
                "mph_input_mutated",
                "an input archive changed while the diff was computed",
            )

    body: dict[str, Any] = {
        "schema_name": MPH_DIFF_SCHEMA_NAME,
        "schema_version": MPH_DIFF_SCHEMA_VERSION,
        "left": _identity(left),
        "right": _identity(right),
        "inputs_unmodified": True,
        "identical": not row_differences,
        "not_a_scientific_equivalence_proof": True,
        "metadata_changes": metadata_changes,
        "tag_changes": tags,
        "parameter_changes": parameters,
        "geometry_changes": geometry,
        "savepoint_changes": savepoints,
        "entry_changes": entries,
        "size_deltas": _size_deltas(left_inventory, right_inventory),
        "warnings": sorted(set(warnings)),
        "diff_fingerprint": "",
    }
    body["diff_fingerprint"] = canonical_sha256_v1(body)
    return body


__all__ = [
    "MPH_DIFF_SCHEMA_NAME",
    "MPH_DIFF_SCHEMA_VERSION",
    "build_mph_diff",
]
