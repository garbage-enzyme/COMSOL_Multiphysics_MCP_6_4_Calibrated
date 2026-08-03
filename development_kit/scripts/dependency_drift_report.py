"""Build the deterministic, information-only dependency-drift report."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import sys
import sysconfig
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

REPORT_SCHEMA = "comsol_mcp.dependency_drift_report"
REPORT_VERSION = "2.0.0"
BOOTSTRAP_TOOLS = frozenset({"pip", "setuptools", "wheel"})
_NORMALIZE_NAME = re.compile(r"[-_.]+")
_LOCK_REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\]+)")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def canonical_distribution_name(name: str) -> str:
    """Return the PEP 503 canonical distribution name."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("distribution name must be a non-empty string")
    return _NORMALIZE_NAME.sub("-", name.strip()).lower()


def _normalized_text_sha256(path: Path) -> str:
    raw = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(raw).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _display_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _declared_specifier(raw: str, requirement: Requirement) -> str | None:
    declaration = raw.partition(";")[0].strip()
    match = re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^]]+\])?\s*(.*)$", declaration)
    if match is not None and match.group(1).strip():
        return match.group(1).strip()
    rendered = str(requirement.specifier)
    return rendered or None


def _parse_scope_requirements(
    pyproject: Mapping[str, Any],
) -> dict[str, dict[str, str | None]]:
    project = pyproject.get("project")
    if not isinstance(project, dict):
        raise ValueError("pyproject.toml is missing [project]")
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise ValueError("project.optional-dependencies must be a table")
    groups: tuple[tuple[str, Any], ...] = (
        ("runtime_direct", project.get("dependencies", [])),
        ("optional_manuals", optional.get("manuals", [])),
        ("development", optional.get("dev", [])),
    )
    result: dict[str, dict[str, str | None]] = {}
    for scope, declarations in groups:
        if not isinstance(declarations, list) or any(
            not isinstance(item, str) for item in declarations
        ):
            raise ValueError(f"{scope} dependencies must be a string list")
        for declaration in declarations:
            try:
                requirement = Requirement(declaration)
            except InvalidRequirement as exc:
                raise ValueError(f"invalid declared requirement: {declaration}") from exc
            name = canonical_distribution_name(requirement.name)
            result.setdefault(
                name,
                {
                    "scope": scope,
                    "specifier": _declared_specifier(declaration, requirement),
                },
            )
    return result


def _parse_release_lock(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = _LOCK_REQUIREMENT.match(line)
        if match is None:
            continue
        name = canonical_distribution_name(match.group(1))
        if name in result:
            raise ValueError(f"release lock contains duplicate distribution: {name}")
        result[name] = match.group(2)
    if not result:
        raise ValueError("release lock contains no pinned requirements")
    return result


def _version_map(items: Sequence[Mapping[str, Any]], *, version_key: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        name = item.get("name")
        version = item.get(version_key)
        if not isinstance(name, str) or not isinstance(version, str) or not version:
            raise ValueError(f"dependency inventory item requires name and {version_key}")
        canonical = canonical_distribution_name(name)
        if canonical in result:
            raise ValueError(f"dependency inventory contains duplicate distribution: {canonical}")
        result[canonical] = version
    return result


def _satisfies(version: str | None, specifier: str | None) -> bool:
    if version is None or not specifier:
        return False
    try:
        return Version(version) in Requirement(f"placeholder{specifier}").specifier
    except InvalidRequirement, InvalidVersion:
        return False


def _exact_requirements(
    installed_requirements: Mapping[str, Sequence[str]],
) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    for parent, requirements in installed_requirements.items():
        parent_name = canonical_distribution_name(parent)
        for raw in requirements:
            try:
                requirement = Requirement(raw)
            except InvalidRequirement:
                continue
            if requirement.marker is not None and not requirement.marker.evaluate():
                continue
            specifier = str(requirement.specifier)
            if not re.fullmatch(r"==[^,;\s]+", specifier):
                continue
            child = canonical_distribution_name(requirement.name)
            result.setdefault(child, []).append({"name": parent_name, "specifier": specifier})
    for parents in result.values():
        parents.sort(key=lambda item: (item["name"], item["specifier"]))
    return result


def _canonical_inventory(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    copied = [dict(item) for item in items]
    return sorted(copied, key=lambda item: canonical_distribution_name(str(item["name"])))


def build_dependency_drift_report(
    *,
    pyproject_path: Path,
    tested_versions_path: Path,
    release_lock_path: Path,
    installed_environment: Sequence[Mapping[str, Any]],
    outdated_dependencies: Sequence[Mapping[str, Any]],
    installed_requirements: Mapping[str, Sequence[str]],
    source_commit: str,
    generated_at_utc: str,
    python_identity: Mapping[str, str],
) -> dict[str, Any]:
    """Build one deterministic report from captured dependency inventories."""
    if not _COMMIT.fullmatch(source_commit):
        raise ValueError("source commit must be a lowercase 40-character SHA")
    if not generated_at_utc:
        raise ValueError("generation time is required")
    pyproject_path = Path(pyproject_path)
    tested_versions_path = Path(tested_versions_path)
    release_lock_path = Path(release_lock_path)
    root = pyproject_path.parent
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8-sig"))
    tested = json.loads(tested_versions_path.read_text(encoding="utf-8-sig"))
    if not isinstance(tested, dict):
        raise ValueError("tested versions manifest must be a JSON object")
    scopes = _parse_scope_requirements(pyproject)
    locked = _parse_release_lock(release_lock_path)
    installed = _version_map(installed_environment, version_key="version")
    latest = _version_map(outdated_dependencies, version_key="latest_version")
    exact_requirements = _exact_requirements(installed_requirements)

    production = tested.get("production_python_3_14", {})
    reviewed_raw = production.get("direct_dependencies", {}) if isinstance(production, dict) else {}
    if not isinstance(reviewed_raw, dict):
        raise ValueError("reviewed direct dependencies must be an object")
    reviewed = {
        canonical_distribution_name(str(name)): str(version)
        for name, version in reviewed_raw.items()
    }
    optional_reviewed = production.get("optional_dependencies", {})
    if not isinstance(optional_reviewed, dict):
        raise ValueError("reviewed optional dependencies must be an object")
    for group in optional_reviewed.values():
        if not isinstance(group, dict):
            raise ValueError("reviewed optional dependency group must be an object")
        for name, version in group.items():
            reviewed[canonical_distribution_name(str(name))] = str(version)
    lock_record = tested.get("release_lock")
    if not isinstance(lock_record, dict):
        raise ValueError("tested versions manifest is missing release_lock")
    lock_hash = _normalized_text_sha256(release_lock_path)
    if lock_record.get("path") != _display_path(release_lock_path, root):
        raise ValueError("reviewed release-lock path differs")
    if lock_record.get("sha256") != lock_hash:
        raise ValueError("reviewed release-lock hash differs")
    if lock_record.get("requirement_count") != len(locked):
        raise ValueError("reviewed release-lock requirement count differs")

    names = sorted(set(scopes) | set(reviewed) | set(locked) | set(installed) | set(latest))
    packages: list[dict[str, Any]] = []
    for name in names:
        declaration = scopes.get(name, {})
        scope = str(declaration.get("scope") or "runtime_transitive")
        if name in BOOTSTRAP_TOOLS and name not in scopes and name not in locked:
            scope = "bootstrap"
        specifier_value = declaration.get("specifier")
        specifier = str(specifier_value) if specifier_value else None
        installed_version = installed.get(name)
        latest_version = latest.get(name)
        item: dict[str, Any] = {"name": name, "scope": scope}
        optional_values = (
            ("declared_specifier", specifier),
            ("reviewed_version", reviewed.get(name)),
            ("locked_version", locked.get(name)),
            ("installed_version", installed_version),
            ("latest_available", latest_version),
        )
        for key, value in optional_values:
            if value is not None:
                item[key] = value

        required_by = exact_requirements.get(name)
        if required_by:
            item["required_by"] = required_by
        if specifier and latest_version is not None:
            if _satisfies(latest_version, specifier):
                item["latest_allowed_by_project"] = latest_version
                item["latest_allowed_basis"] = "declared_specifier"
            elif _satisfies(installed_version, specifier):
                item["latest_allowed_by_project"] = installed_version
                item["latest_allowed_basis"] = "current_compatible_resolution"

        if installed_version is None and name in scopes:
            decision = "missing_scope"
        elif required_by and latest_version is not None and installed_version != latest_version:
            decision = "paired_only"
        elif scope == "bootstrap":
            decision = "informational"
        elif specifier and latest_version is not None and not _satisfies(latest_version, specifier):
            decision = "outside_project_range"
        elif latest_version is not None and installed_version != latest_version:
            decision = "candidate"
        elif name in locked and installed_version is not None and locked[name] != installed_version:
            decision = "candidate"
        else:
            decision = "informational"
        item["decision"] = decision
        packages.append(item)

    drifted = [
        {
            "name": name,
            "locked_version": locked[name],
            "installed_version": installed[name],
        }
        for name in sorted(set(locked) & set(installed))
        if locked[name] != installed[name]
    ]
    missing = [name for name in sorted(locked) if name not in installed]
    matched_count = sum(locked[name] == installed[name] for name in set(locked) & set(installed))
    direct_comparison = []
    for name in sorted(reviewed):
        actual = installed.get(name)
        direct_comparison.append(
            {
                "name": name,
                "reviewed_version": reviewed[name],
                "installed_version": actual,
                "status": (
                    "missing"
                    if actual is None
                    else "matched"
                    if actual == reviewed[name]
                    else "drifted"
                ),
                "scope": scopes.get(name, {}).get("scope", "runtime_transitive"),
            }
        )

    return {
        "schema_name": REPORT_SCHEMA,
        "schema_version": REPORT_VERSION,
        "generated_at_utc": generated_at_utc,
        "source_commit": source_commit,
        "python_identity": dict(sorted(python_identity.items())),
        "installed_extras": ["dev", "manuals"],
        "reviewed_inputs": {
            "pyproject": {
                "path": _display_path(pyproject_path, root),
                "sha256": _file_sha256(pyproject_path),
            },
            "tested_versions": {
                "path": _display_path(tested_versions_path, root),
                "sha256": _file_sha256(tested_versions_path),
            },
            "release_lock": {
                "path": _display_path(release_lock_path, root),
                "sha256": lock_hash,
            },
        },
        "packages": packages,
        "direct_dependency_comparison": direct_comparison,
        "release_lock_comparison": {
            "locked_count": len(locked),
            "matched_count": matched_count,
            "drift_count": len(drifted),
            "missing_count": len(missing),
            "drifted": drifted,
            "missing": missing,
        },
        "installed_environment": _canonical_inventory(installed_environment),
        "outdated_dependencies": _canonical_inventory(outdated_dependencies),
        "pip_check": "passed",
        "information_only": True,
    }


def serialize_dependency_drift_report(report: Mapping[str, Any]) -> bytes:
    """Serialize a report to stable UTF-8 JSON bytes."""
    return (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{path.name} must contain a JSON object list")
    return value


def _installed_requirement_metadata() -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for distribution in importlib.metadata.distributions():
        raw_name = distribution.metadata.get("Name")
        if not raw_name:
            continue
        name = canonical_distribution_name(raw_name)
        requirements = distribution.requires or []
        result[name] = sorted(str(item) for item in requirements)
    return result


def _python_identity() -> dict[str, str]:
    implementation = f"cp{sys.version_info.major}{sys.version_info.minor}"
    system_platform = sysconfig.get_platform().replace("-", "_")
    return {
        "version": platform.python_version(),
        "abi": f"{implementation}-{system_platform}",
        "platform": system_platform,
        "gil_mode": "standard",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument(
        "--tested-versions",
        type=Path,
        default=Path("constraints/tested_versions.json"),
    )
    parser.add_argument(
        "--release-lock",
        type=Path,
        default=Path("constraints/release_locked_py314.txt"),
    )
    parser.add_argument("--installed", type=Path, required=True)
    parser.add_argument("--outdated", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--generated-at-utc")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    generated_at = arguments.generated_at_utc or datetime.now(UTC).isoformat().replace(
        "+00:00", "Z"
    )
    report = build_dependency_drift_report(
        pyproject_path=arguments.pyproject,
        tested_versions_path=arguments.tested_versions,
        release_lock_path=arguments.release_lock,
        installed_environment=_load_json_list(arguments.installed),
        outdated_dependencies=_load_json_list(arguments.outdated),
        installed_requirements=_installed_requirement_metadata(),
        source_commit=arguments.source_commit,
        generated_at_utc=generated_at,
        python_identity=_python_identity(),
    )
    arguments.output.write_bytes(serialize_dependency_drift_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "build_dependency_drift_report",
    "canonical_distribution_name",
    "main",
    "serialize_dependency_drift_report",
]
