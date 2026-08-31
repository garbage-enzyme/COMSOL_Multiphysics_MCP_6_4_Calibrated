"""Versioned compatibility and skill-layer registry without runtime guessing.

``comsol_mcp.compatibility_registry`` records the active Python/MPh/JPype
identities, supported ranges from the packaged compatibility manifest, the
selected profile, skill-layer identities with bounded file hashes, and
explicit unknown-or-unsupported warnings. COMSOL identity appears only when a
session is actually bound through a passive provider; it is never inferred
from an unrelated installed executable and never triggers a fallback runtime
or profile.
"""

from __future__ import annotations

import hashlib
import sys
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Callable, Mapping

from comsol_mcp.compatibility import load_runtime_compatibility
from comsol_mcp.durable import canonical_sha256_v1
from comsol_mcp.tools.catalog import FEATURE_NAMES, PROFILE_NAMES

COMPATIBILITY_REGISTRY_SCHEMA_NAME = "comsol_mcp.compatibility_registry"
COMPATIBILITY_REGISTRY_SCHEMA_VERSION = "1.0.0"

_MAX_WARNING_ROWS = 32
_SKILL_FILE_ROOT = Path(__file__).resolve().parents[1]

SkillProvider = Callable[[], Mapping[str, Any] | None]


def _distribution_version(distribution: str) -> str | None:
    """Read installed distribution metadata without importing the package."""
    try:
        version = importlib_metadata.version(distribution)
    except importlib_metadata.PackageNotFoundError:
        return None
    return version or None


def _parse_version(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in text.split("."):
        digits = ""
        for character in piece:
            if character.isdigit():
                digits += character
            else:
                break
        if not digits:
            raise ValueError(f"unsupported version component in {text!r}")
        parts.append(int(digits))
    return tuple(parts)


def _version_in_range(version_text: str, range_text: str) -> bool:
    """Evaluate one comma-separated ``>=X,<Y`` range on numeric dot versions."""
    try:
        candidate = _parse_version(version_text)
    except ValueError:
        return False
    for comparator in range_text.split(","):
        comparator = comparator.strip()
        if comparator.startswith(">="):
            operator, bound_text = ">=", comparator[2:]
        elif comparator.startswith("<="):
            operator, bound_text = "<=", comparator[2:]
        elif comparator.startswith(">"):
            operator, bound_text = ">", comparator[1:]
        elif comparator.startswith("<"):
            operator, bound_text = "<", comparator[1:]
        elif comparator.startswith("=="):
            operator, bound_text = "==", comparator[2:]
        else:
            operator, bound_text = "==", comparator
        try:
            bound = _parse_version(bound_text)
        except ValueError:
            return False
        width = max(len(candidate), len(bound))
        left = candidate + (0,) * (width - len(candidate))
        right = bound + (0,) * (width - len(bound))
        if operator == ">=" and not left >= right:
            return False
        if operator == "<=" and not left <= right:
            return False
        if operator == ">" and not left > right:
            return False
        if operator == "<" and not left < right:
            return False
        if operator == "==" and left != right:
            return False
    return True


def _skill_layer_rows(enabled_features: frozenset[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Hash the packaged skill-layer files that exist; classify each layer."""
    definitions = (
        (
            "embedded_docs",
            None,
            (
                "knowledge/prompts/mph_api.md",
                "knowledge/prompts/physics_guide.md",
                "knowledge/prompts/workflow.md",
            ),
        ),
        ("lexical_docs", "lexical_docs", ()),
        ("semantic_docs", "semantic_docs", ()),
        ("shared_server", "shared_server", ()),
    )
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    for layer_id, gate, relative_files in definitions:
        enabled = gate is None or gate in enabled_features
        files: list[dict[str, Any]] = []
        for relative in relative_files:
            path = _SKILL_FILE_ROOT / relative
            try:
                payload = path.read_bytes()
            except OSError:
                warnings.append(f"skill_file_unreadable:{relative}")
                continue
            files.append({"path": relative, "sha256": hashlib.sha256(payload).hexdigest()})
        rows.append(
            {
                "layer_id": layer_id,
                "feature_gate": gate,
                "enabled": enabled,
                "files": files,
            }
        )
    return rows, warnings


def build_compatibility_registry(
    *,
    profile_name: str,
    enabled_features: tuple[str, ...] | list[str] = (),
    runtime_compatibility: Mapping[str, Any] | None = None,
    expected_skill_file_sha256s: Mapping[str, str] | None = None,
    comsol_bound_provider: SkillProvider | None = None,
) -> dict[str, Any]:
    """Build one bounded compatibility/skill registry snapshot offline."""
    warnings: list[str] = []

    if profile_name not in PROFILE_NAMES:
        raise ValueError(f"unknown profile {profile_name!r}; expected one of {PROFILE_NAMES}")
    features = frozenset(enabled_features)
    if not features <= set(FEATURE_NAMES):
        raise ValueError(f"unknown enabled features: {sorted(features - set(FEATURE_NAMES))}")

    manifest = (
        dict(runtime_compatibility)
        if runtime_compatibility is not None
        else load_runtime_compatibility()
    )

    python_version = "{0}.{1}.{2}".format(*sys.version_info[:3])
    mph_version = _distribution_version("MPh")
    jpype_version = _distribution_version("JPype1")
    if mph_version is None:
        warnings.append("mph_distribution_metadata_unavailable")
    if jpype_version is None:
        warnings.append("jpype_distribution_metadata_unavailable")

    dependency = manifest["dependency_compatibility"]
    python_in_range = _version_in_range(python_version, dependency["python"])
    mph_in_range = (
        _version_in_range(mph_version, dependency["mph"]) if mph_version is not None else False
    )
    if not python_in_range:
        warnings.append("active_python_outside_declared_dependency_range")
    if mph_version is not None and not mph_in_range:
        warnings.append("active_mph_outside_declared_dependency_range")

    accepted_lane = manifest["licensed_acceptance"][0]
    lane_python_matches = python_version == accepted_lane["python_version"]
    lane_mph_matches = mph_version == accepted_lane["mph_version"]
    if lane_python_matches and lane_mph_matches:
        licensed_lane_status = "active_runtime_matches_accepted_lane"
    elif python_in_range and mph_in_range:
        licensed_lane_status = "dependency_ranges_only_no_licensed_acceptance"
    else:
        licensed_lane_status = "active_runtime_outside_declared_support"

    bound_comsol: dict[str, Any] = {
        "identity_status": "not_requested",
        "build": None,
        "session_shared": False,
    }
    if comsol_bound_provider is not None:
        try:
            snapshot = comsol_bound_provider()
        except Exception:
            bound_comsol["identity_status"] = "unavailable"
            warnings.append("bound_comsol_probe_failed")
            snapshot = None
        if snapshot is None:
            if bound_comsol["identity_status"] != "unavailable":
                bound_comsol["identity_status"] = "not_bound"
        else:
            available = bool(snapshot.get("available"))
            build = snapshot.get("comsol_build")
            if not available or not isinstance(build, str) or not build:
                bound_comsol["identity_status"] = "not_bound"
            else:
                bound_comsol["identity_status"] = "bound"
                bound_comsol["build"] = build[:64]
                bound_comsol["session_shared"] = bool(snapshot.get("shared_session"))
                if build != accepted_lane["comsol_build"]:
                    warnings.append("bound_comsol_build_without_exact_licensed_acceptance")

    skill_layers, layer_warnings = _skill_layer_rows(features)
    warnings.extend(layer_warnings)
    if expected_skill_file_sha256s is not None:
        for row in skill_layers:
            for item in row["files"]:
                expected = expected_skill_file_sha256s.get(item["path"])
                if expected is None:
                    continue
                if expected.lower() != item["sha256"]:
                    warnings.append(f"skill_file_hash_mismatch:{item['path']}")

    unique_warnings = sorted(set(warnings))
    if len(unique_warnings) > _MAX_WARNING_ROWS:
        del unique_warnings[_MAX_WARNING_ROWS:]

    registry = {
        "schema_name": COMPATIBILITY_REGISTRY_SCHEMA_NAME,
        "schema_version": COMPATIBILITY_REGISTRY_SCHEMA_VERSION,
        "runtime": {
            "python_version": python_version,
            "mph_version": mph_version,
            "jpype_version": jpype_version,
            "bound_comsol": bound_comsol,
        },
        "supported_ranges": {
            "licensed_acceptance": manifest["licensed_acceptance"],
            "dependency_compatibility": manifest["dependency_compatibility"],
            "unknown_compatibility": manifest["unknown_compatibility"],
        },
        "licensed_lane_status": licensed_lane_status,
        "profile": {
            "name": profile_name,
            "enabled_features": sorted(features),
        },
        "skill_layers": skill_layers,
        "warnings": unique_warnings,
        "registry_sha256": "",
    }
    body = {key: value for key, value in registry.items() if key != "registry_sha256"}
    registry["registry_sha256"] = canonical_sha256_v1(body)
    return registry


__all__ = [
    "COMPATIBILITY_REGISTRY_SCHEMA_NAME",
    "COMPATIBILITY_REGISTRY_SCHEMA_VERSION",
    "build_compatibility_registry",
]
