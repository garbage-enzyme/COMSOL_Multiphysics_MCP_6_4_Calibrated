"""Canonical package identity and bounded legacy namespace compatibility."""

from __future__ import annotations

import importlib
from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).parents[2]


def test_legacy_and_canonical_imports_share_singletons():
    canonical_session = importlib.import_module("comsol_mcp.tools.session")
    legacy_session = importlib.import_module("src.tools.session")
    canonical_ownership = importlib.import_module("comsol_mcp.tools.ownership")
    legacy_ownership = importlib.import_module("src.tools.ownership")

    assert canonical_session is legacy_session
    assert canonical_ownership is legacy_ownership
    assert canonical_session.session_manager is legacy_session.session_manager
    assert canonical_ownership.ownership_manager is legacy_ownership.ownership_manager


def test_canonical_driver_writers_and_legacy_readers_agree():
    from comsol_mcp.jobs.branch_continuation_campaign import (
        current_branch_continuation_campaign_driver_identity,
        validate_branch_continuation_campaign_driver_identity,
    )
    from comsol_mcp.jobs.convergence_campaign import (
        current_convergence_campaign_driver_identity,
        validate_convergence_campaign_driver_identity,
    )
    from comsol_mcp.jobs.spectral_characterization import (
        current_spectral_driver_identity,
        validate_spectral_driver_identity,
    )

    cases = (
        (
            current_spectral_driver_identity,
            validate_spectral_driver_identity,
        ),
        (
            current_convergence_campaign_driver_identity,
            validate_convergence_campaign_driver_identity,
        ),
        (
            current_branch_continuation_campaign_driver_identity,
            validate_branch_continuation_campaign_driver_identity,
        ),
    )
    for writer, reader in cases:
        expected = writer()
        legacy = {
            **expected,
            "implementation": expected["implementation"].replace(
                "comsol_mcp.", "src.", 1
            ),
        }
        assert reader({"driver_identity": legacy}) == expected


def test_new_driver_identities_are_canonical():
    from comsol_mcp.jobs.spectral_characterization import current_spectral_driver_identity

    identity = current_spectral_driver_identity()

    assert identity["implementation"].startswith("comsol_mcp.")


def test_canonical_implementation_has_no_legacy_imports():
    legacy_import = re.compile(r"(?:from|import)\s+src(?:\.|\s)")
    matches = []
    for path in (ROOT / "comsol_mcp").rglob("*.py"):
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if legacy_import.search(line):
                matches.append(f"{path.relative_to(ROOT)}:{line_number}")

    assert matches == []


def test_packaging_declares_canonical_implementation_and_one_shim():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["scripts"]["comsol-mcp"] == "comsol_mcp.server:main"
    assert project["tool"]["hatch"]["version"]["path"] == "comsol_mcp/__init__.py"
    assert project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "comsol_mcp",
        "src",
    ]
    assert [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src").rglob("*.py")
    ] == ["src/__init__.py"]
