"""Warning-only automatic `.mph` artifact probes for post-run pipelines.

The probe summarizes bounded `.mph` files left in one job directory after a
job reaches a terminal state. A failed parse is recorded as a warning row
and never changes the primary job disposition, raises, or starts COMSOL,
Java, MPh, or JPype.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from comsol_mcp.evidence.inspection.archive import MphInspectionError
from comsol_mcp.evidence.inspection.summary import build_mph_inspection_summary

MPH_ARTIFACT_PROBE_SCHEMA_NAME = "comsol_mcp.mph_artifact_probe"
MPH_ARTIFACT_PROBE_SCHEMA_VERSION = "1.0.0"
MPH_ARTIFACT_PROBE_MAX_FILES = 4

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


def probe_mph_artifacts(directory: str | Path) -> dict[str, Any]:
    """Probe bounded `.mph` artifacts below one job directory without raising."""
    root = Path(directory)
    try:
        if not root.is_dir():
            return {
                "schema_name": MPH_ARTIFACT_PROBE_SCHEMA_NAME,
                "schema_version": MPH_ARTIFACT_PROBE_SCHEMA_VERSION,
                "available": False,
                "reason_code": "mph_probe_directory_unavailable",
                "probes": [],
                "truncated": False,
            }
        candidates = sorted(path for path in root.glob("*.mph") if path.is_file())
    except OSError:
        return {
            "schema_name": MPH_ARTIFACT_PROBE_SCHEMA_NAME,
            "schema_version": MPH_ARTIFACT_PROBE_SCHEMA_VERSION,
            "available": False,
            "reason_code": "mph_probe_directory_unavailable",
            "probes": [],
            "truncated": False,
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
    return {
        "schema_name": MPH_ARTIFACT_PROBE_SCHEMA_NAME,
        "schema_version": MPH_ARTIFACT_PROBE_SCHEMA_VERSION,
        "available": True,
        "probes": probes,
        "truncated": truncated,
    }


__all__ = [
    "MPH_ARTIFACT_PROBE_MAX_FILES",
    "MPH_ARTIFACT_PROBE_SCHEMA_NAME",
    "MPH_ARTIFACT_PROBE_SCHEMA_VERSION",
    "probe_mph_artifacts",
]
