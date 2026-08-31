"""Bounded stdlib-only safety reader for offline `.mph` ZIP archives.

The reader never starts COMSOL, Java, MPh, or JPype and never writes to the
inspected file. Every refusal is a typed public reason code so callers can
distinguish corrupt, unsafe, unsupported, and over-limit archives without
path-bearing diagnostics.
"""

from __future__ import annotations

import hashlib
import types
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from comsol_mcp.contracts.mph_inspection import MphInspectionLimits

_STREAM_CHUNK_BYTES = 1_048_576
_UNIX_FILE_TYPE_MASK = 0o170_000
_UNIX_LINK_FILE_TYPE = 0o120_000


class MphInspectionError(ValueError):
    """One fail-closed inspection refusal with a stable public reason code."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class ArchiveEntry:
    """One bounded archive member observation."""

    name: str
    file_size: int
    compress_size: int
    is_directory: bool
    crc: int


@dataclass(frozen=True)
class ArchiveInventory:
    """Bounded observations for one accepted `.mph` container."""

    entries: tuple[ArchiveEntry, ...]
    byte_size: int
    sha256: str
    total_uncompressed_bytes: int
    total_compressed_bytes: int


def _reject_entry_name(raw_name: str) -> str:
    """Backward-compatible name-only rejection retained for direct callers."""
    return reject_entry_metadata(
        types.SimpleNamespace(filename=raw_name, external_attr=0, flag_bits=0)
    )


def reject_entry_metadata(info: Any) -> str:
    """Reject one archive member whose declared metadata is unsafe."""
    raw_name = info.filename
    if not raw_name:
        raise MphInspectionError(
            "mph_entry_path_unsafe",
            "archive contains an empty entry name",
        )
    stripped = raw_name[:-1] if raw_name.endswith("/") else raw_name
    if "\\" in raw_name or "\x00" in raw_name:
        raise MphInspectionError(
            "mph_entry_path_unsafe",
            "archive entry names must use forward slashes only",
        )
    normalized = unicodedata.normalize("NFC", stripped)
    if normalized != stripped:
        raise MphInspectionError(
            "mph_entry_path_unsafe",
            "archive entry names must already use canonical NFC Unicode",
        )
    if stripped.startswith("/") or stripped[1:2] == ":" or stripped.startswith("//"):
        raise MphInspectionError(
            "mph_entry_path_unsafe",
            "absolute archive entry paths are not allowed",
        )
    segments = stripped.split("/")
    if any(segment in {"..", "."} for segment in segments):
        raise MphInspectionError(
            "mph_entry_path_unsafe",
            "archive entry path traversal escapes are not allowed",
        )
    if any(not segment for segment in segments):
        raise MphInspectionError(
            "mph_entry_path_unsafe",
            "archive entry paths contain ambiguous empty components",
        )
    unix_mode = (getattr(info, "external_attr", 0) >> 16) & _UNIX_FILE_TYPE_MASK
    if unix_mode == _UNIX_LINK_FILE_TYPE:
        raise MphInspectionError(
            "mph_symlink_like_entry",
            "symlink-like archive entries are not allowed",
        )
    if getattr(info, "flag_bits", 0) & 0x1:
        raise MphInspectionError(
            "mph_encrypted_entry",
            "encrypted archive entries are not supported",
        )
    return "/".join(segments)


def inspect_archive_inventory(
    file_path: str | Path,
    limits: MphInspectionLimits | None = None,
) -> ArchiveInventory:
    """Read one bounded safe inventory of an offline `.mph` archive."""
    bounds = limits or MphInspectionLimits()
    path = Path(file_path)
    try:
        stat_result = path.stat()
    except OSError as exc:
        raise MphInspectionError(
            "mph_source_unavailable",
            "the requested archive could not be accessed",
        ) from exc
    if not stat_result.st_size > 0:
        raise MphInspectionError("mph_invalid_zip", "archive files must not be empty")
    if stat_result.st_size > bounds.max_archive_bytes:
        raise MphInspectionError(
            "mph_file_too_large",
            "archive exceeds the caller byte limit",
        )
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        consumed = 0
        while True:
            chunk = stream.read(_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            consumed += len(chunk)
            if consumed > bounds.max_archive_bytes:
                raise MphInspectionError(
                    "mph_file_too_large",
                    "archive grew beyond the caller byte limit during hashing",
                )
            digest.update(chunk)
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise MphInspectionError(
            "mph_invalid_zip",
            "the file is not a readable ZIP archive",
        ) from exc
    try:
        infos = archive.infolist()
        if len(infos) > bounds.max_entries:
            raise MphInspectionError(
                "mph_too_many_entries",
                "archive exceeds the caller entry-count limit",
            )
        seen_names: set[str] = set()
        entries: list[ArchiveEntry] = []
        total_uncompressed = 0
        total_compressed = 0
        for info in infos:
            name = reject_entry_metadata(info)
            folded = name.casefold()
            if folded in seen_names:
                raise MphInspectionError(
                    "mph_duplicate_entry_name",
                    "archive contains case-folded duplicate entry names",
                )
            seen_names.add(folded)
            if info.file_size > bounds.max_entry_uncompressed_bytes:
                raise MphInspectionError(
                    "mph_oversized_entry",
                    "an archive entry exceeds the per-entry uncompressed limit",
                )
            if info.compress_size > 0:
                ratio = info.file_size // info.compress_size
                if ratio > bounds.max_compression_ratio:
                    raise MphInspectionError(
                        "mph_excessive_compression_ratio",
                        "an archive entry exceeds the compression-ratio guard",
                    )
            total_uncompressed += info.file_size
            if total_uncompressed > bounds.max_total_uncompressed_bytes:
                raise MphInspectionError(
                    "mph_total_size_exceeded",
                    "archive exceeds the total uncompressed-byte limit",
                )
            total_compressed += info.compress_size
            entries.append(
                ArchiveEntry(
                    name=name,
                    file_size=info.file_size,
                    compress_size=info.compress_size,
                    is_directory=info.is_dir(),
                    crc=info.CRC,
                )
            )
    finally:
        archive.close()
    return ArchiveInventory(
        entries=tuple(entries),
        byte_size=stat_result.st_size,
        sha256=digest.hexdigest(),
        total_uncompressed_bytes=total_uncompressed,
        total_compressed_bytes=total_compressed,
    )


def read_bounded_member(
    file_path: str | Path,
    member_name: str,
    *,
    max_bytes: int,
) -> bytes:
    """Read exactly one bounded archive member after re-opening the container."""
    path = Path(file_path)
    with zipfile.ZipFile(path) as archive:
        names = {info.filename: info for info in archive.infolist()}
        if member_name not in names:
            raise MphInspectionError(
                "mph_marker_missing",
                "a required archive marker member is missing",
            )
        info = names[member_name]
        if info.file_size > max_bytes:
            raise MphInspectionError(
                "mph_oversized_entry",
                "an archive marker exceeds the declared bound",
            )
        return archive.read(member_name)


def entry_map(inventory: ArchiveInventory) -> dict[str, ArchiveEntry]:
    """Return one name-keyed view of an inventory for marker lookups."""
    return {entry.name: entry for entry in inventory.entries}


def size_breakdown(inventory: ArchiveInventory) -> dict[str, Any]:
    """Return a deterministic compressed/uncompressed breakdown by category."""
    categories = {
        "marker_xml": ("modelinfo.xml", "fileids.xml", "clusterignore.xml"),
        "model_definition_xml": ("dmodel.xml",),
        "model_json": ("smodel.json", "auxiliarydatainfo.json"),
        "savepoint_data": ("savepoint",),
        "binary_resource": (".mphbin",),
    }

    def category_for(name: str) -> str:
        for category, markers in categories.items():
            for marker in markers:
                if name == marker or name.startswith(marker) or name.endswith(marker):
                    return category
        return "other"

    totals: dict[str, dict[str, int]] = {}
    for entry in inventory.entries:
        bucket = totals.setdefault(category_for(entry.name), {"entries": 0, "bytes": 0})
        bucket["entries"] += 0 if entry.is_directory else 1
        bucket["bytes"] += entry.file_size
    ordered: dict[str, Any] = {}
    for category in sorted({*totals, "other"}):
        counts = totals.get(category)
        if counts is None:
            continue
        ordered[category] = {
            "entry_count": counts["entries"],
            "uncompressed_bytes": counts["bytes"],
        }
    ordered["totals"] = {
        "entry_count": sum(1 for item in inventory.entries if not item.is_directory),
        "uncompressed_bytes": inventory.total_uncompressed_bytes,
        "compressed_bytes": inventory.total_compressed_bytes,
        "archive_bytes": inventory.byte_size,
    }
    return ordered


__all__ = [
    "ArchiveEntry",
    "ArchiveInventory",
    "MphInspectionError",
    "entry_map",
    "inspect_archive_inventory",
    "read_bounded_member",
    "reject_entry_metadata",
    "size_breakdown",
]
