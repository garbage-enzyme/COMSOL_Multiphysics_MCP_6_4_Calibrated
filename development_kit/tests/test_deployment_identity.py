"""deployment identity deployment identity and concurrent fresh-process consistency."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tomllib

from src import __version__
from src.build_identity import get_build_identity, package_content_sha256
from src.compatibility import load_runtime_compatibility
from src.tools.capabilities import get_capabilities
from src.tools.capabilities import startup_capability_summary
from src.tools.profiles import ProfileSelection


ROOT = Path(__file__).parents[2]
SNAPSHOTS = ROOT / "development_kit" / "tests" / "snapshots"


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
    assert identity["schema_version"] == "1.1.0"
    assert identity["package_version"] == __version__
    assert identity["build_identity"] == get_build_identity()
    assert identity["build_identity"]["package_version"] == __version__
    assert identity["full_tool_schemas_sha256"] == _snapshot_sha256(
        SNAPSHOTS / "full_tool_schemas.json"
    )
    assert identity["profile_tool_names_sha256"] == _snapshot_sha256(
        SNAPSHOTS / "profile_tool_names.json"
    )
    assert len(identity["catalog_contract_sha256"]) == 64
    assert identity["source_classification"] == "source_tree"
    assert identity["contains_local_path"] is False
    serialized = json.dumps(identity, ensure_ascii=False)
    assert "陆星" not in serialized
    assert "C:\\Users\\" not in serialized
    assert str(ROOT) not in serialized


def test_snapshot_identity_is_invariant_to_checkout_line_endings(tmp_path):
    lf = tmp_path / "lf.json"
    crlf = tmp_path / "crlf.json"
    lf.write_bytes(b'{\n  "value": 1\n}\n')
    crlf.write_bytes(b'{\r\n  "value": 1\r\n}\r\n')

    assert _snapshot_sha256(lf) == _snapshot_sha256(crlf)


def test_build_identity_ignores_generated_files_and_changes_with_package_bytes(tmp_path):
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

    (package / "alpha.py").write_text("value = 2\n", encoding="utf-8")
    assert package_content_sha256(package) != first
    identity = get_build_identity(package)
    assert identity["paths_included"] is False
    assert str(tmp_path) not in json.dumps(identity)


def test_package_version_has_one_authoritative_source():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    manifest = json.loads(
        (ROOT / "comsol_mcp" / "deployment_manifest.json").read_text(encoding="utf-8")
    )

    assert project["project"]["dynamic"] == ["version"]
    assert "version" not in project["project"]
    assert project["tool"]["hatch"]["version"]["path"] == "comsol_mcp/__init__.py"
    assert "package_version" not in manifest
    assert get_capabilities(_selection("core"))["deployment_identity"][
        "package_version"
    ] == __version__


def test_deployment_identity_is_profile_independent():
    identities = [
        get_capabilities(_selection(profile))["deployment_identity"]
        for profile in ("core", "basic_fem", "wave_optics", "semantic_docs", "full")
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
