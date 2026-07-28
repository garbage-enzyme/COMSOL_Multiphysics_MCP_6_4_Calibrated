"""Solver-free access to the packaged runtime compatibility declaration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_MANIFEST_PATH = Path(__file__).with_name("compatibility_manifest.json")
_TOP_LEVEL_FIELDS = {
    "schema_name",
    "schema_version",
    "licensed_acceptance",
    "dependency_compatibility",
    "unknown_compatibility",
}
_LICENSED_LANE_FIELDS = {
    "status",
    "comsol_build",
    "mph_version",
    "java_version",
    "python_version",
    "scope",
}
_DEPENDENCY_FIELDS = {
    "status",
    "python",
    "mph",
    "mcp",
    "comsol_builds",
    "establishes_licensed_compatibility",
}
_UNKNOWN_FIELDS = {
    "status",
    "comsol_builds",
    "requires_independent_licensed_acceptance",
}
_MAX_COMPATIBILITY_TEXT = 256
_MAX_ACCEPTANCE_SCOPE_ITEMS = 32


def _bounded_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_COMPATIBILITY_TEXT:
        raise ValueError(f"{label} must be a bounded nonempty string")
    return value


def canonical_module_identifier(value: object) -> object:
    """Map a legacy producer or worker module name to the canonical namespace."""
    if not isinstance(value, str):
        return value
    if value == "src":
        return "comsol_mcp"
    if value.startswith("src."):
        return "comsol_mcp" + value[3:]
    return value


def module_identity_matches(expected: object, observed: object) -> bool:
    """Compare module identities while preserving all non-namespace fields."""
    return canonical_module_identifier(expected) == canonical_module_identifier(observed)


def load_runtime_compatibility() -> dict[str, Any]:
    """Load and validate the packaged compatibility declaration."""
    value = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != _TOP_LEVEL_FIELDS:
        raise ValueError("runtime compatibility manifest fields are invalid")
    if (
        value.get("schema_name") != "comsol_mcp.runtime_compatibility"
        or value.get("schema_version") != "1.0.0"
    ):
        raise ValueError("runtime compatibility manifest schema is unsupported")

    accepted = value.get("licensed_acceptance")
    if not isinstance(accepted, list) or len(accepted) != 1:
        raise ValueError("runtime compatibility must declare one accepted lane")
    lane = accepted[0]
    if (
        not isinstance(lane, dict)
        or set(lane) != _LICENSED_LANE_FIELDS
        or lane.get("status") != "exact_licensed_acceptance"
    ):
        raise ValueError("licensed compatibility lane is invalid")
    for field in ("comsol_build", "mph_version", "java_version", "python_version"):
        _bounded_text(lane[field], f"licensed compatibility {field}")
    scope = lane["scope"]
    if not isinstance(scope, list) or not 1 <= len(scope) <= _MAX_ACCEPTANCE_SCOPE_ITEMS:
        raise ValueError("licensed compatibility scope is invalid")
    if not all(isinstance(item, str) for item in scope) or len(scope) != len(set(scope)):
        raise ValueError("licensed compatibility scope is invalid")
    for index, item in enumerate(scope):
        _bounded_text(item, f"licensed compatibility scope[{index}]")

    dependency = value.get("dependency_compatibility")
    if (
        not isinstance(dependency, dict)
        or set(dependency) != _DEPENDENCY_FIELDS
        or dependency.get("status") != "dependency_only"
        or dependency.get("comsol_builds") != []
        or dependency.get("establishes_licensed_compatibility") is not False
    ):
        raise ValueError("dependency compatibility declaration is invalid")
    for field in ("python", "mph", "mcp"):
        _bounded_text(dependency[field], f"dependency compatibility {field}")

    unknown = value.get("unknown_compatibility")
    if (
        not isinstance(unknown, dict)
        or set(unknown) != _UNKNOWN_FIELDS
        or unknown.get("status") != "unknown"
        or unknown.get("requires_independent_licensed_acceptance") is not True
    ):
        raise ValueError("unknown compatibility declaration is invalid")
    _bounded_text(unknown["comsol_builds"], "unknown compatibility comsol_builds")
    return value


__all__ = [
    "canonical_module_identifier",
    "load_runtime_compatibility",
    "module_identity_matches",
]
