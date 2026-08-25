"""Solver-free matrix/hash/cold-discovery suite for the compatibility registry.

Covers the alpha7.3 fixture matrix: exact supported tuple, unknown bound
COMSOL build, unknown skill hash, mismatched profile, and a detected-but-not-
active install. Nothing here imports or starts COMSOL, Java, MPh, or JPype.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from comsol_mcp.compatibility import load_runtime_compatibility
from comsol_mcp.evidence.compatibility_registry import (
    COMPATIBILITY_REGISTRY_SCHEMA_NAME,
    COMPATIBILITY_REGISTRY_SCHEMA_VERSION,
    build_compatibility_registry,
)
from comsol_mcp.schema_registry import check_schema_support


def _skill_file_hash(relative: str) -> str:
    path = Path(__file__).parents[2] / "comsol_mcp" / relative.replace("/", "\\")
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Exact supported tuple
# ---------------------------------------------------------------------------


def test_active_runtime_matching_the_accepted_lane_is_classified_exactly(monkeypatch):
    import comsol_mcp.evidence.compatibility_registry as registry_module

    monkeypatch.setattr(registry_module.sys, "version_info", (3, 14, 6, "final", 0))
    monkeypatch.setattr(
        registry_module,
        "_distribution_version",
        lambda name: "1.3.1" if name == "MPh" else "1.7.1",
    )
    registry = build_compatibility_registry(profile_name="core", enabled_features=())
    assert registry["schema_name"] == COMPATIBILITY_REGISTRY_SCHEMA_NAME
    assert registry["schema_version"] == COMPATIBILITY_REGISTRY_SCHEMA_VERSION
    assert registry["licensed_lane_status"] == "active_runtime_matches_accepted_lane"
    assert (
        registry["runtime"]["mph_version"]
        == load_runtime_compatibility()["licensed_acceptance"][0]["mph_version"]
    )
    range_warnings = [
        warning
        for warning in registry["warnings"]
        if "outside_declared" in warning or "metadata_unavailable" in warning
    ]
    assert range_warnings == []
    assert registry["registry_sha256"]


def test_registry_is_deterministic_for_identical_inputs():
    first = build_compatibility_registry(
        profile_name="wave_optics",
        enabled_features=("lexical_docs",),
    )
    second = build_compatibility_registry(
        profile_name="wave_optics",
        enabled_features=("lexical_docs",),
    )
    assert first == second


def test_unknown_profile_and_unknown_features_fail_closed():
    with pytest.raises(ValueError):
        build_compatibility_registry(profile_name="not-a-profile")
    with pytest.raises(ValueError):
        build_compatibility_registry(
            profile_name="core",
            enabled_features=("teleportation",),
        )


# ---------------------------------------------------------------------------
# Bound COMSOL identity lanes
# ---------------------------------------------------------------------------


def test_bound_comsol_build_outside_the_accepted_lane_is_visible():
    registry = build_compatibility_registry(
        profile_name="core",
        comsol_bound_provider=lambda: {
            "available": True,
            "comsol_build": "9.9.9.999",
            "shared_session": False,
        },
    )
    assert registry["runtime"]["bound_comsol"] == {
        "identity_status": "bound",
        "build": "9.9.9.999",
        "session_shared": False,
    }
    assert "bound_comsol_build_without_exact_licensed_acceptance" in registry["warnings"]


def test_bound_comsol_on_the_accepted_build_produces_no_warning():
    registry = build_compatibility_registry(
        profile_name="core",
        comsol_bound_provider=lambda: {
            "available": True,
            "comsol_build": "6.4.0.293",
            "shared_session": True,
        },
    )
    assert registry["runtime"]["bound_comsol"]["identity_status"] == "bound"
    assert registry["runtime"]["bound_comsol"]["session_shared"] is True
    assert "bound_comsol_build_without_exact_licensed_acceptance" not in (registry["warnings"])


def test_unbound_and_failing_providers_stay_structured():
    unbound = build_compatibility_registry(
        profile_name="core",
        comsol_bound_provider=lambda: None,
    )
    assert unbound["runtime"]["bound_comsol"]["identity_status"] == "not_bound"

    def failing():
        raise RuntimeError("observer lost")

    failed = build_compatibility_registry(
        profile_name="core",
        comsol_bound_provider=failing,
    )
    assert failed["runtime"]["bound_comsol"]["identity_status"] == "unavailable"
    assert "bound_comsol_probe_failed" in failed["warnings"]


def test_comsol_identity_defaults_to_not_requested():
    registry = build_compatibility_registry(profile_name="core")
    assert registry["runtime"]["bound_comsol"] == {
        "identity_status": "not_requested",
        "build": None,
        "session_shared": False,
    }


# ---------------------------------------------------------------------------
# Skill-layer hashes
# ---------------------------------------------------------------------------


def test_embedded_docs_skill_files_are_hashed_from_packaged_prompts():
    registry = build_compatibility_registry(profile_name="core")
    embedded = next(row for row in registry["skill_layers"] if row["layer_id"] == "embedded_docs")
    assert embedded["enabled"] is True
    paths = [item["path"] for item in embedded["files"]]
    assert paths == [
        "knowledge/prompts/mph_api.md",
        "knowledge/prompts/physics_guide.md",
        "knowledge/prompts/workflow.md",
    ]
    for item in embedded["files"]:
        assert item["sha256"] == _skill_file_hash(item["path"])


def test_feature_gated_layers_reflect_the_selected_profile():
    registry = build_compatibility_registry(
        profile_name="core",
        enabled_features=("lexical_docs",),
    )
    by_layer = {row["layer_id"]: row for row in registry["skill_layers"]}
    assert by_layer["embedded_docs"]["enabled"] is True
    assert by_layer["lexical_docs"]["enabled"] is True
    assert by_layer["semantic_docs"]["enabled"] is False
    assert by_layer["shared_server"]["enabled"] is False


def test_unknown_expected_skill_hash_is_reported_not_silent():
    expected = {
        "knowledge/prompts/mph_api.md": "0" * 64,
    }
    registry = build_compatibility_registry(
        profile_name="core",
        expected_skill_file_sha256s=expected,
    )
    assert "skill_file_hash_mismatch:knowledge/prompts/mph_api.md" in registry["warnings"]

    exact = build_compatibility_registry(
        profile_name="core",
        expected_skill_file_sha256s={
            "knowledge/prompts/mph_api.md": _skill_file_hash("knowledge/prompts/mph_api.md")
        },
    )
    assert not any(warning.startswith("skill_file_hash_mismatch") for warning in exact["warnings"])


# ---------------------------------------------------------------------------
# Detected-but-not-active install
# ---------------------------------------------------------------------------


def test_detected_but_unsupported_mph_install_is_visible(monkeypatch):
    from comsol_mcp.evidence import compatibility_registry as module

    monkeypatch.setattr(
        module, "_distribution_version", lambda name: "9.9.9" if name == "MPh" else "1.7.1"
    )
    registry = build_compatibility_registry(profile_name="core")

    assert registry["licensed_lane_status"] == "active_runtime_outside_declared_support"
    assert "active_mph_outside_declared_dependency_range" in registry["warnings"]
    # The detected version is reported truthfully instead of being hidden.
    assert registry["runtime"]["mph_version"] == "9.9.9"


def test_missing_distributions_are_warned_not_guessed(monkeypatch):
    from comsol_mcp.evidence import compatibility_registry as module

    monkeypatch.setattr(module, "_distribution_version", lambda name: None)
    registry = build_compatibility_registry(profile_name="core")
    assert "mph_distribution_metadata_unavailable" in registry["warnings"]
    assert "jpype_distribution_metadata_unavailable" in registry["warnings"]
    assert registry["runtime"]["mph_version"] is None
    assert registry["runtime"]["jpype_version"] is None
    assert registry["licensed_lane_status"] == "active_runtime_outside_declared_support"


# ---------------------------------------------------------------------------
# Cold discovery and public dispatch
# ---------------------------------------------------------------------------


def test_cold_discovery_builds_the_registry_without_importing_solver_packages(tmp_path):
    fixture_dir = tmp_path / "cold"
    fixture_dir.mkdir()
    code = (
        "import json, sys\n"
        "from comsol_mcp.evidence.compatibility_registry import (\n"
        "    build_compatibility_registry,\n"
        ")\n"
        "registry = build_compatibility_registry(profile_name='comsolless_read_only')\n"
        "print(json.dumps({\n"
        "    'mph_loaded': 'mph' in sys.modules,\n"
        "    'jpype_loaded': 'jpype' in sys.modules,\n"
        "    'lane': registry['licensed_lane_status'],\n"
        "    'profile': registry['profile']['name'],\n"
        "}))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            **__import__("os").environ,
            "PYTHONPATH": str(Path(__file__).parents[2]),
        },
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["mph_loaded"] is False
    assert payload["jpype_loaded"] is False
    assert payload["profile"] == "comsolless_read_only"


def test_public_dispatch_reports_the_requested_profile_and_stays_solver_free():
    from src.server import create_server

    from development_kit.tests.mcp_test_support import decode_tool_result

    server = create_server("compat-registry-dispatch", profile="comsolless_read_only")
    result = decode_tool_result(
        __import__("asyncio").run(server.call_tool("runtime_compatibility_status", {}))
    )
    assert result["success"] is True
    assert result["solver_started"] is False
    assert result["filesystem_modified"] is False
    assert result["registry"]["profile"]["name"] == "comsolless_read_only"

    requested = decode_tool_result(
        __import__("asyncio").run(
            server.call_tool(
                "runtime_compatibility_status",
                {"request_comsol_identity": True},
            )
        )
    )
    assert requested["success"] is True
    assert requested["registry"]["runtime"]["bound_comsol"]["identity_status"] in {
        "not_bound",
        "unavailable",
        "bound",
    }


def test_capabilities_expose_the_registry_next_to_the_static_manifest():
    from src.server import create_server

    from development_kit.tests.mcp_test_support import decode_tool_result

    server = create_server("compat-registry-capabilities", profile="core")
    capabilities = decode_tool_result(
        __import__("asyncio").run(server.call_tool("capabilities", {}))
    )
    registry_block = capabilities["compatibility_registry"]
    assert registry_block["profile"]["name"] == "core"
    assert (
        registry_block["supported_ranges"]["licensed_acceptance"]
        == load_runtime_compatibility()["licensed_acceptance"]
    )
    assert capabilities["runtime_compatibility"] == load_runtime_compatibility()


def test_schema_registry_supports_the_published_contract():
    support = check_schema_support(COMPATIBILITY_REGISTRY_SCHEMA_NAME, "1.0.0")
    assert support["supported"] is True
    assert support["producer"] == "comsol_mcp.evidence.compatibility_registry"
