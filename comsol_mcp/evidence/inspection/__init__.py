"""Offline `.mph` archive inspection contracts (solver-free, stdlib-only)."""

from __future__ import annotations

from comsol_mcp.evidence.inspection.archive import (
    ArchiveEntry,
    ArchiveInventory,
    MphInspectionError,
    entry_map,
    inspect_archive_inventory,
    read_bounded_member,
    size_breakdown,
)
from comsol_mcp.evidence.inspection.summary import (
    MPH_INSPECTION_SUMMARY_SCHEMA_NAME,
    MPH_INSPECTION_SUMMARY_SCHEMA_VERSION,
    build_mph_inspection_summary,
)

__all__ = [
    "MPH_INSPECTION_SUMMARY_SCHEMA_NAME",
    "MPH_INSPECTION_SUMMARY_SCHEMA_VERSION",
    "ArchiveEntry",
    "ArchiveInventory",
    "MphInspectionError",
    "build_mph_inspection_summary",
    "entry_map",
    "inspect_archive_inventory",
    "read_bounded_member",
    "size_breakdown",
]
