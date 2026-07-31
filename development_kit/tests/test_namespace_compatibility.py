"""Canonical package identity and bounded legacy namespace compatibility."""

from __future__ import annotations

import ast
import importlib
import json
import runpy
import subprocess
import sys
import tomllib
from pathlib import Path
from types import FunctionType

import pytest

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


@pytest.mark.parametrize("legacy_first", [False, True])
def test_import_orders_share_exact_canonical_metadata_in_fresh_process(legacy_first):
    first, second = (
        ("src.tools.session", "comsol_mcp.tools.session")
        if legacy_first
        else ("comsol_mcp.tools.session", "src.tools.session")
    )
    code = f"""
import importlib
import json

first = importlib.import_module({first!r})
canonical = importlib.import_module('comsol_mcp.tools.session')
before = {{
    'cached': canonical.__cached__,
    'file': canonical.__file__,
    'loader_type': type(canonical.__loader__).__qualname__,
    'name': canonical.__name__,
    'package': canonical.__package__,
    'spec_cached': canonical.__spec__.cached,
    'spec_name': canonical.__spec__.name,
    'spec_origin': canonical.__spec__.origin,
}}
second = importlib.import_module({second!r})
after = {{
    'cached': canonical.__cached__,
    'file': canonical.__file__,
    'loader_type': type(canonical.__loader__).__qualname__,
    'name': canonical.__name__,
    'package': canonical.__package__,
    'spec_cached': canonical.__spec__.cached,
    'spec_name': canonical.__spec__.name,
    'spec_origin': canonical.__spec__.origin,
}}
print(json.dumps({{'same': first is second is canonical, 'before': before, 'after': after}}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["same"] is True
    assert result["after"] == result["before"]
    assert result["after"]["spec_name"] == "comsol_mcp.tools.session"
    assert result["after"]["spec_origin"].endswith("comsol_mcp\\tools\\session.py")
    assert result["after"]["spec_cached"].endswith("session.cpython-314.pyc")


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
            "implementation": expected["implementation"].replace("comsol_mcp.", "src.", 1),
        }
        assert reader({"driver_identity": legacy}) == expected


def test_new_driver_identities_are_canonical():
    from comsol_mcp.jobs.branch_continuation_campaign import (
        current_branch_continuation_campaign_driver_identity,
    )
    from comsol_mcp.jobs.convergence_campaign import (
        current_convergence_campaign_driver_identity,
    )
    from comsol_mcp.jobs.spectral_characterization import (
        current_spectral_driver_identity,
    )

    for writer in (
        current_spectral_driver_identity,
        current_convergence_campaign_driver_identity,
        current_branch_continuation_campaign_driver_identity,
    ):
        assert writer()["implementation"].startswith("comsol_mcp.")


def test_canonical_implementation_has_no_legacy_imports():
    matches = []
    for path in sorted((ROOT / "comsol_mcp").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module]
            if any(name == "src" or name.startswith("src.") for name in imported):
                matches.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert matches == []


def test_packaging_declares_canonical_implementation_and_one_shim():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["scripts"]["comsol-mcp"] == "comsol_mcp.server:main"
    assert project["tool"]["hatch"]["version"]["path"] == "comsol_mcp/__init__.py"
    assert project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "comsol_mcp",
        "src",
    ]
    assert [path.relative_to(ROOT).as_posix() for path in sorted((ROOT / "src").rglob("*.py"))] == [
        "src/__init__.py"
    ]


def test_legacy_module_execution_uses_real_main_context(monkeypatch):
    import src

    calls = []
    monkeypatch.setattr(runpy, "run_module", lambda *args, **kwargs: calls.append((args, kwargs)))
    loader = src._CanonicalAliasLoader("src.fake", "comsol_mcp.fake")

    FunctionType(loader.get_code("src.fake"), {})()

    assert calls == [(("comsol_mcp.fake",), {"run_name": "__main__", "alter_sys": True})]


def test_reloading_legacy_package_retains_one_alias_finder():
    import src

    for _ in range(3):
        importlib.reload(src)

    finders = [
        item
        for item in sys.meta_path
        if getattr(item, "alias_finder_identity", None) == "comsol_mcp.src_alias_finder.v1"
    ]
    assert len(finders) == 1
