"""Warning-only automatic `.mph` artifact probes for post-run pipelines.

The probe summarizes bounded `.mph` files left in one job directory after a
job reaches a terminal state. A failed parse is recorded as a warning row
and never changes the primary job disposition, raises, or starts COMSOL,
Java, MPh, or JPype.

B13: the terminal probe summary is cached on disk. Ordinary status reads
reuse the cache when every probed file's name/size/mtime still match;
``refresh=True`` forces a re-scan. A stale/unavailable cache never
changes the job terminal state.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from comsol_mcp.evidence.inspection.archive import MphInspectionError
from comsol_mcp.evidence.inspection.summary import build_mph_inspection_summary

MPH_ARTIFACT_PROBE_SCHEMA_NAME = "comsol_mcp.mph_artifact_probe"
MPH_ARTIFACT_PROBE_SCHEMA_VERSION = "1.0.0"
MPH_ARTIFACT_PROBE_MAX_FILES = 4
MPH_ARTIFACT_PROBE_CACHE_FILENAME = "mph_probe_summary.json"
_MPH_PROBE_CACHE_BYTES = 262_144

_IDENTITY_FIELDS = (
    "sha256",
    "byte_size",
    "comsol_version",
    "title",
    "node_type",
    "runnable_state",
    "solved_state",
    "preview_state",
)


def _bounded_summary(summary: dict[str, Any]) -> dict[str, Any]:
    identity = {field: summary[field] for field in _IDENTITY_FIELDS}
    return {
        "schema_name": summary["schema_name"],
        "schema_version": summary["schema_version"],
        **identity,
    }


def _file_fingerprint(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "file_name": path.name,
        "byte_size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _cache_is_current(cache: Mapping[str, Any] | None, candidates: list[Path]) -> bool:
    if not isinstance(cache, Mapping):
        return False
    cached_files = cache.get("file_fingerprints")
    if not isinstance(cached_files, list) or len(cached_files) != len(candidates):
        return False
    for path, recorded in zip(candidates, cached_files, strict=True):
        if not isinstance(recorded, Mapping):
            return False
        try:
            live = _file_fingerprint(path)
        except OSError:
            return False
        if (
            recorded.get("file_name") != live["file_name"]
            or recorded.get("byte_size") != live["byte_size"]
            or recorded.get("mtime_ns") != live["mtime_ns"]
        ):
            return False
    return cache.get("probe") is not None


def _list_candidates(root: Path) -> list[Path]:
    try:
        if not root.is_dir():
            return []
        return sorted(path for path in root.glob("*.mph") if path.is_file())
    except OSError:
        return []


def probe_mph_artifacts(directory: str | Path, *, refresh: bool = False) -> dict[str, Any]:
    """Probe bounded `.mph` artifacts below one job directory without raising.

    B13: ordinary calls reuse a hash-bound cache. ``refresh`` re-scans.
    """
    root = Path(directory)
    cache_path = root / MPH_ARTIFACT_PROBE_CACHE_FILENAME
    candidates = _list_candidates(root)
    if not root.is_dir():
        return {
            "schema_name": MPH_ARTIFACT_PROBE_SCHEMA_NAME,
            "schema_version": MPH_ARTIFACT_PROBE_SCHEMA_VERSION,
            "available": False,
            "reason_code": "mph_probe_directory_unavailable",
            "probes": [],
            "truncated": False,
        }

    if not refresh:
        cache: Mapping[str, Any] | None = None
        try:
            raw = cache_path.read_bytes()
            if 0 < len(raw) <= _MPH_PROBE_CACHE_BYTES:
                cache = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            cache = None
        if _cache_is_current(cache, candidates):
            cached_probe = cache["probe"]
            return {
                **cached_probe,
                "cache": {
                    "status": "hit",
                    "refreshed": False,
                },
            }

    truncated = len(candidates) > MPH_ARTIFACT_PROBE_MAX_FILES
    probes: list[dict[str, Any]] = []
    for path in candidates[:MPH_ARTIFACT_PROBE_MAX_FILES]:
        try:
            summary = build_mph_inspection_summary(path)
        except MphInspectionError as exc:
            probes.append(
                {
                    "file_name": path.name,
                    "available": False,
                    "reason_code": exc.reason_code,
                }
            )
            continue
        except Exception:
            probes.append(
                {
                    "file_name": path.name,
                    "available": False,
                    "reason_code": "mph_inspection_rejected",
                }
            )
            continue
        probes.append(
            {
                "file_name": path.name,
                "available": True,
                "summary": _bounded_summary(summary),
            }
        )
    probe = {
        "schema_name": MPH_ARTIFACT_PROBE_SCHEMA_NAME,
        "schema_version": MPH_ARTIFACT_PROBE_SCHEMA_VERSION,
        "available": True,
        "probes": probes,
        "truncated": truncated,
        "cache": {
            "status": "miss",
            "refreshed": bool(refresh),
        },
    }
    try:
        fingerprints = []
        for path in candidates[:MPH_ARTIFACT_PROBE_MAX_FILES]:
            try:
                fingerprints.append(_file_fingerprint(path))
            except OSError:
                continue
        cache_payload = {
            "schema_name": MPH_ARTIFACT_PROBE_SCHEMA_NAME,
            "schema_version": MPH_ARTIFACT_PROBE_SCHEMA_VERSION,
            "file_fingerprints": fingerprints,
            "probe": {key: value for key, value in probe.items() if key != "cache"},
        }
        cache_path.write_text(
            json.dumps(cache_payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        # Cache write failure must not affect the probe result.
        probe["cache"] = {"status": "unavailable", "refreshed": bool(refresh)}
    return probe


__all__ = [
    "MPH_ARTIFACT_PROBE_CACHE_FILENAME",
    "MPH_ARTIFACT_PROBE_MAX_FILES",
    "MPH_ARTIFACT_PROBE_SCHEMA_NAME",
    "MPH_ARTIFACT_PROBE_SCHEMA_VERSION",
    "probe_mph_artifacts",
]
