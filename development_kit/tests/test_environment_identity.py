"""Solver-free environment identity tests."""

from __future__ import annotations

import json
import os
import re

from src.durable import canonical_sha256_v1
from src.environment_identity import get_environment_identity
from src.tools.capabilities import get_capabilities
from src.tools.profiles import ProfileSelection

from src import __version__


def _selection() -> ProfileSelection:
    return ProfileSelection(
        name="core",
        source="environment-identity-test",
        environment_variable="COMSOL_MCP_PROFILE",
        default_used=False,
    )


def test_environment_identity_is_bounded_redacted_and_hash_stable():
    first = get_environment_identity()
    second = get_environment_identity()

    assert first == second
    assert first["schema_name"] == "comsol_mcp.environment_identity"
    assert first["schema_version"] == "1.0.0"
    assert first["collection_mode"] == "solver_free_metadata_only"
    assert first["package"] == {"name": "comsol-mcp", "version": __version__}
    assert re.fullmatch(r"[0-9a-f]{64}", first["identity_sha256"])
    identity_payload = dict(first)
    reported_digest = identity_payload.pop("identity_sha256")
    assert reported_digest == canonical_sha256_v1(identity_payload)
    assert first["redaction"] == {
        "paths_included": False,
        "host_identity_included": False,
        "user_identity_included": False,
    }

    serialized = json.dumps(first, ensure_ascii=False, sort_keys=True)
    for variable in ("COMPUTERNAME", "USERNAME", "USERPROFILE", "HOME"):
        value = os.environ.get(variable)
        if value:
            assert value not in serialized
    assert "C:\\Users\\" not in serialized


def test_environment_identity_separates_dependency_and_external_runtime_evidence():
    identity = get_environment_identity()
    direct = {item["name"]: item for item in identity["distributions"]["direct"]}
    transitive = {item["name"]: item for item in identity["distributions"]["relevant_transitive"]}

    assert set(direct) == {"matplotlib", "mcp", "mph", "numpy", "pydantic", "psutil", "scipy"}
    assert set(transitive) == {
        "anyio",
        "httpcore2",
        "httpx2",
        "jpype1",
        "mcp-types",
        "opentelemetry-api",
        "pydantic-core",
        "starlette",
        "truststore",
        "uvicorn",
    }
    assert all(transitive[name]["availability"] == "installed" for name in transitive)
    assert direct["mph"]["availability"] == "installed"
    assert identity["licensed_runtime_declaration"] == {
        "status": "exact_licensed_acceptance",
        "comsol_build": "6.4.0.293",
        "java_version": "21.0.7",
        "mph_version": "1.3.1",
        "python_version": "3.14.6",
    }
    assert identity["observed_external_runtime"] == {
        "status": "not_observed",
        "comsol_build": None,
        "java_version": None,
        "reason": "solver_free_collection_does_not_probe_external_runtimes",
    }


def test_capabilities_embed_the_same_environment_identity():
    capabilities = get_capabilities(_selection())
    assert capabilities["environment_identity"] == get_environment_identity()
