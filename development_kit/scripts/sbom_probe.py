"""Generate a deterministic CycloneDX SBOM from a locked installed runtime."""

from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import re
from typing import Mapping


_PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^ ]+) \\$")


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).casefold()


def parse_locked_requirements(path: str | Path) -> dict[str, str]:
    pins = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line or line.startswith(("#", " ")):
            continue
        match = _PIN.fullmatch(line)
        if match is None:
            raise ValueError(f"unsupported locked requirement line: {line}")
        name = _canonical_name(match.group(1))
        if name in pins:
            raise ValueError(f"duplicate locked requirement: {name}")
        pins[name] = match.group(2)
    if not pins:
        raise ValueError("locked requirements contain no packages")
    return pins


def build_cyclonedx_sbom(
    *,
    locked_pins: Mapping[str, str],
    installed_versions: Mapping[str, str],
    package_version: str,
    lock_sha256: str,
) -> dict:
    if "comsol-mcp" in locked_pins:
        raise ValueError("release lock must not contain the root comsol-mcp package")
    normalized_installed = {
        _canonical_name(name): value for name, value in installed_versions.items()
    }
    if dict(locked_pins) != {name: normalized_installed.get(name) for name in locked_pins}:
        mismatches = {
            name: {"locked": expected, "installed": normalized_installed.get(name)}
            for name, expected in locked_pins.items()
            if normalized_installed.get(name) != expected
        }
        raise ValueError(f"installed runtime does not match locked versions: {mismatches}")
    components = [
        {
            "type": "library",
            "name": name,
            "version": locked_pins[name],
            "bom-ref": f"pkg:pypi/{name}@{locked_pins[name]}",
            "purl": f"pkg:pypi/{name}@{locked_pins[name]}",
        }
        for name in sorted(locked_pins)
    ]
    root = {
        "type": "application",
        "name": "comsol-mcp",
        "version": package_version,
        "bom-ref": f"pkg:pypi/comsol-mcp@{package_version}",
        "purl": f"pkg:pypi/comsol-mcp@{package_version}",
    }
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": root,
            "properties": [
                {"name": "comsol-mcp:release-lock-sha256", "value": lock_sha256},
                {"name": "comsol-mcp:component-scope", "value": "locked-runtime"},
            ],
        },
        "components": components,
        "dependencies": [
            {
                "ref": root["bom-ref"],
                "dependsOn": [item["bom-ref"] for item in components],
            }
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    pins = parse_locked_requirements(args.lock)
    installed = {}
    for name in pins:
        try:
            installed[name] = version(name)
        except PackageNotFoundError as exc:
            raise RuntimeError(f"locked package is not installed: {name}") from exc
    try:
        package_version = version("comsol-mcp")
    except PackageNotFoundError as exc:
        raise RuntimeError("comsol-mcp wheel is not installed") from exc
    sbom = build_cyclonedx_sbom(
        locked_pins=pins,
        installed_versions=installed,
        package_version=package_version,
        lock_sha256=hashlib.sha256(args.lock.read_bytes()).hexdigest(),
    )
    args.output.write_text(
        json.dumps(sbom, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
