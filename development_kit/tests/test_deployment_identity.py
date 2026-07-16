"""deployment identity deployment identity and concurrent fresh-process consistency."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from src.tools.capabilities import get_capabilities
from src.tools.profiles import ProfileSelection


ROOT = Path(__file__).parents[2]
SNAPSHOTS = ROOT / "development_kit" / "tests" / "snapshots"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    assert identity["schema_version"] == "1.0.0"
    assert identity["full_tool_schemas_sha256"] == _sha256(
        SNAPSHOTS / "full_tool_schemas.json"
    )
    assert identity["profile_tool_names_sha256"] == _sha256(
        SNAPSHOTS / "profile_tool_names.json"
    )
    assert len(identity["catalog_contract_sha256"]) == 64
    assert identity["source_classification"] == "source_tree"
    assert identity["contains_local_path"] is False
    serialized = json.dumps(identity, ensure_ascii=False)
    assert "陆星" not in serialized
    assert "C:\\Users\\" not in serialized
    assert str(ROOT) not in serialized


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
