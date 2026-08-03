"""Deterministic SBOM and release inventory receipt tests."""

from __future__ import annotations

import pytest

from development_kit.scripts.sbom_probe import (
    build_cyclonedx_sbom,
    parse_locked_requirements,
)


def test_locked_requirement_parser_and_sbom_are_sorted_and_exact(tmp_path):
    lock = tmp_path / "lock.txt"
    lock.write_text(
        "# Python-Lane: 3.14\n"
        "zeta==2.0 \\\n    --hash=sha256:" + "a" * 64 + "\n"
        "Alpha_Core==1.0 \\\n    --hash=sha256:" + "b" * 64 + "\n",
        encoding="utf-8",
    )
    pins = parse_locked_requirements(lock)
    assert pins == {"zeta": "2.0", "alpha-core": "1.0"}

    sbom = build_cyclonedx_sbom(
        locked_pins=pins,
        installed_versions={"ZETA": "2.0", "alpha_core": "1.0"},
        package_version="0.6.0",
        lock_sha256="c" * 64,
    )
    assert sbom == {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "comsol-mcp",
                "version": "0.6.0",
                "bom-ref": "pkg:pypi/comsol-mcp@0.6.0",
                "purl": "pkg:pypi/comsol-mcp@0.6.0",
            },
            "properties": [
                {"name": "comsol-mcp:release-lock-sha256", "value": "c" * 64},
                {"name": "comsol-mcp:component-scope", "value": "locked-runtime"},
            ],
        },
        "components": [
            {
                "type": "library",
                "name": "alpha-core",
                "version": "1.0",
                "bom-ref": "pkg:pypi/alpha-core@1.0",
                "purl": "pkg:pypi/alpha-core@1.0",
            },
            {
                "type": "library",
                "name": "zeta",
                "version": "2.0",
                "bom-ref": "pkg:pypi/zeta@2.0",
                "purl": "pkg:pypi/zeta@2.0",
            },
        ],
        "dependencies": [
            {
                "ref": "pkg:pypi/comsol-mcp@0.6.0",
                "dependsOn": ["pkg:pypi/alpha-core@1.0", "pkg:pypi/zeta@2.0"],
            }
        ],
    }


@pytest.mark.parametrize("installed_versions", [{}, {"alpha": "2.0"}])
def test_sbom_fails_on_missing_or_different_locked_versions(installed_versions):
    with pytest.raises(ValueError, match="does not match"):
        build_cyclonedx_sbom(
            locked_pins={"alpha": "1.0"},
            installed_versions=installed_versions,
            package_version="0.6.0",
            lock_sha256="c" * 64,
        )
