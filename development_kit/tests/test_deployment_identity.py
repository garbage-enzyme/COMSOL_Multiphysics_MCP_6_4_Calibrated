"""deployment identity deployment identity and concurrent fresh-process consistency."""

from __future__ import annotations

import _winapi
import hashlib
import json
import re
import subprocess
import sys
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
import src.tools.capabilities as capabilities_module
from src.build_identity import get_build_identity, package_content_sha256
from src.compatibility import load_runtime_compatibility
from src.tools.capabilities import get_capabilities, startup_capability_summary
from src.tools.profiles import PROFILE_NAMES, ProfileSelection

from src import __version__

ROOT = Path(__file__).parents[2]
SNAPSHOTS = ROOT / "development_kit" / "tests" / "snapshots"


def _string_leaves(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _string_leaves(key)
            yield from _string_leaves(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _string_leaves(item)


def _snapshot_sha256(path: Path) -> str:
    """Hash snapshot content independently of Git checkout line endings."""
    normalized = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(normalized).hexdigest()


def _selection(profile: str) -> ProfileSelection:
    return ProfileSelection(
        name=profile,
        source="deployment-identity-test",
        environment_variable="COMSOL_MCP_PROFILE",
        default_used=False,
    )


def test_deployment_manifest_matches_frozen_profile_and_schema_snapshots():
    identity = get_capabilities(_selection("core"))["deployment_identity"]

    assert identity["available"] is True
    assert identity["schema_name"] == "comsol_mcp.deployment_identity"
    assert identity["schema_version"] == "1.2.0"
    assert identity["package_version"] == __version__
    assert identity["build_identity"] == get_build_identity()
    assert identity["build_identity"]["package_version"] == __version__
    assert identity["full_tool_schemas_sha256"] == _snapshot_sha256(
        SNAPSHOTS / "full_tool_schemas.json"
    )
    assert identity["profile_tool_names_sha256"] == _snapshot_sha256(
        SNAPSHOTS / "profile_tool_names.json"
    )
    assert identity["feature_tool_names_sha256"] == _snapshot_sha256(
        SNAPSHOTS / "feature_tool_names.json"
    )
    assert len(identity["catalog_contract_sha256"]) == 64
    assert identity["source_classification"] == "source_tree"
    assert identity["contains_local_path"] is False
    string_leaves = tuple(_string_leaves(identity))
    normalized_leaves = tuple(value.replace("\\", "/").casefold() for value in string_leaves)
    normalized_root = str(ROOT.resolve()).replace("\\", "/").casefold()
    assert all("陆星" not in value for value in string_leaves)
    assert all(not re.search(r"\b[a-z]:/users/", value) for value in normalized_leaves)
    assert all(normalized_root not in value for value in normalized_leaves)


def test_nonobject_deployment_manifest_fails_closed(tmp_path, monkeypatch):
    manifest = tmp_path / "deployment_manifest.json"
    manifest.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(capabilities_module, "_DEPLOYMENT_MANIFEST", manifest)

    identity = get_capabilities(_selection("core"))["deployment_identity"]

    assert identity["available"] is False
    assert identity["source_classification"] == "unknown"
    assert "deployment manifest unavailable" in identity["error"]


def test_deployment_identity_does_not_mask_manifest_path_classification(tmp_path, monkeypatch):
    manifest = json.loads(capabilities_module._DEPLOYMENT_MANIFEST.read_text(encoding="utf-8"))
    manifest["contains_local_path"] = True
    path = tmp_path / "deployment_manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(capabilities_module, "_DEPLOYMENT_MANIFEST", path)

    identity = get_capabilities(_selection("core"))["deployment_identity"]

    assert identity["available"] is True
    assert identity["contains_local_path"] is True


def test_deployment_classification_is_bound_to_the_distribution_root(tmp_path, monkeypatch):
    distribution_root = tmp_path / "runtime" / "site-packages"
    source_lookalike = tmp_path / "workspace" / "site-packages" / "source"
    monkeypatch.setattr(
        capabilities_module,
        "distribution",
        lambda _name: SimpleNamespace(locate_file=lambda _relative: distribution_root),
    )

    assert (
        capabilities_module._deployment_source_classification(
            source_lookalike / "comsol_mcp" / "tools" / "capabilities.py"
        )
        == "source_tree"
    )
    assert (
        capabilities_module._deployment_source_classification(
            distribution_root / "comsol_mcp" / "tools" / "capabilities.py"
        )
        == "installed_site_package"
    )


def test_snapshot_identity_is_invariant_to_checkout_line_endings(tmp_path):
    lf = tmp_path / "lf.json"
    crlf = tmp_path / "crlf.json"
    lf.write_bytes(b'{\n  "value": 1\n}\n')
    crlf.write_bytes(b'{\r\n  "value": 1\r\n}\r\n')

    assert _snapshot_sha256(lf) == _snapshot_sha256(crlf)


def test_build_identity_ignores_interpreter_caches_and_covers_generated_package_bytes(tmp_path):
    package = tmp_path / "src"
    package.mkdir()
    (package / "alpha.py").write_text("value = 1\n", encoding="utf-8")
    nested = package / "data"
    nested.mkdir()
    (nested / "manifest.json").write_text('{"value":1}\n', encoding="utf-8")
    first = package_content_sha256(package)

    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "alpha.pyc").write_bytes(b"generated")
    assert package_content_sha256(package) == first

    generated = package / "generated_manifest.json"
    generated.write_text('{"generated":true}\n', encoding="utf-8")
    generated_digest = package_content_sha256(package)
    assert generated_digest != first

    (nested / "manifest.json").write_text('{"value":2}\n', encoding="utf-8")
    package_data_digest = package_content_sha256(package)
    assert package_data_digest != generated_digest

    (package / "alpha.py").write_text("value = 2\n", encoding="utf-8")
    assert package_content_sha256(package) != package_data_digest
    identity = get_build_identity(package)
    assert identity["generated_files_included"] is True
    assert identity["content_scope"] == (
        "length_prefixed_sorted_relative_non_cache_package_paths_and_file_bytes"
    )
    assert identity["paths_included"] is False
    serialized = json.dumps(identity)
    assert str(tmp_path) not in serialized
    assert all(
        private_component not in serialized
        for private_component in (
            tmp_path.name,
            package.name,
            "alpha.py",
            nested.name,
            "manifest.json",
            generated.name,
        )
    )


def test_build_identity_recomputes_after_each_package_change(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    target = package / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    first = package_content_sha256(package)

    target.write_text("value = 2\n", encoding="utf-8")

    assert package_content_sha256(package) != first


def test_concurrent_build_identity_requests_share_only_the_inflight_hash(tmp_path, monkeypatch):
    import comsol_mcp.build_identity as build_identity_module

    package = tmp_path / "package"
    package.mkdir()
    (package / "module.py").write_text("value = 1\n", encoding="utf-8")
    original_hash = build_identity_module._hash_package_content
    calls = 0
    entered = threading.Event()
    release = threading.Event()

    def bounded_hash(root):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(5)
        return original_hash(root)

    monkeypatch.setattr(build_identity_module, "_hash_package_content", bounded_hash)
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(package_content_sha256, package) for _ in range(8)]
        assert entered.wait(5)
        release.set()
        results = [future.result(timeout=5) for future in futures]

    assert len(set(results)) == 1
    assert calls == 1
    package_content_sha256(package)
    assert calls == 2


def test_build_identity_length_prefixes_paths_and_payloads(tmp_path):
    single = tmp_path / "single"
    multiple = tmp_path / "multiple"
    single.mkdir()
    multiple.mkdir()
    (single / "a").write_bytes(b"X\0b\0Y")
    (multiple / "a").write_bytes(b"X")
    (multiple / "b").write_bytes(b"Y")

    assert package_content_sha256(single) != package_content_sha256(multiple)


def test_build_identity_rejects_package_junctions(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "module.py").write_text("value = 1\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "hidden.py").write_text("hidden = True\n", encoding="utf-8")
    junction = package / "linked"
    _winapi.CreateJunction(str(outside), str(junction))
    try:
        with pytest.raises(ValueError, match="symlinks or junctions"):
            package_content_sha256(package)
    finally:
        junction.rmdir()


def test_build_identity_rejects_a_junction_in_package_root_ancestry(tmp_path):
    outside = tmp_path / "outside"
    package = outside / "package"
    package.mkdir(parents=True)
    (package / "module.py").write_text("value = 1\n", encoding="utf-8")
    junction = tmp_path / "linked"
    _winapi.CreateJunction(str(outside), str(junction))
    try:
        with pytest.raises(ValueError, match="symlink or junction"):
            package_content_sha256(junction / "package")
    finally:
        junction.rmdir()


def test_build_identity_rejects_content_changed_during_read(tmp_path, monkeypatch):
    import comsol_mcp.build_identity as build_identity_module

    package = tmp_path / "package"
    package.mkdir()
    target = package / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    original_read_bytes = Path.read_bytes

    def mutate_after_read(path):
        payload = original_read_bytes(path)
        if path == target:
            path.write_text("value = 200\n", encoding="utf-8")
        return payload

    monkeypatch.setattr(build_identity_module.Path, "read_bytes", mutate_after_read)

    with pytest.raises(ValueError, match="changed during identity collection"):
        package_content_sha256(package)


def test_package_version_has_one_authoritative_source():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    manifest = json.loads(
        (ROOT / "comsol_mcp" / "deployment_manifest.json").read_text(encoding="utf-8")
    )

    assert project["project"]["dynamic"] == ["version"]
    assert "version" not in project["project"]
    assert project["tool"]["hatch"]["version"]["path"] == "comsol_mcp/__init__.py"
    assert "package_version" not in manifest
    assert (
        get_capabilities(_selection("core"))["deployment_identity"]["package_version"]
        == __version__
    )


def test_deployment_identity_is_profile_independent():
    identities = [
        get_capabilities(_selection(profile))["deployment_identity"] for profile in PROFILE_NAMES
    ]

    assert identities
    assert all(identity == identities[0] for identity in identities[1:])


def test_concurrent_fresh_source_processes_report_identical_identity():
    code = (
        "import json; "
        "from src.tools.capabilities import get_capabilities; "
        "print(json.dumps(get_capabilities()['deployment_identity'], sort_keys=True))"
    )

    def probe(_index: int) -> dict:
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return json.loads(completed.stdout.strip().splitlines()[-1])

    with ThreadPoolExecutor(max_workers=6) as executor:
        identities = list(executor.map(probe, range(12)))

    assert identities
    assert all(identity == identities[0] for identity in identities[1:])
    assert identities[0]["source_classification"] == "source_tree"
    assert identities[0]["contains_local_path"] is False


def test_capabilities_report_exact_runtime_compatibility_without_future_inference():
    capabilities = get_capabilities(_selection("core"))
    compatibility = capabilities["runtime_compatibility"]

    assert compatibility == load_runtime_compatibility()
    assert capabilities["targets"] == {
        "comsol": "6.4.0.293",
        "mph": "1.3.1",
        "acceptance": "exact_licensed_acceptance",
    }
    assert compatibility["dependency_compatibility"]["comsol_builds"] == []
    assert compatibility["unknown_compatibility"]["status"] == "unknown"
    assert "6.4+" not in json.dumps(capabilities, sort_keys=True)
    summary = startup_capability_summary(_selection("core"))
    assert "COMSOL 6.4.0.293 exact licensed / MPh 1.3.1" in summary
