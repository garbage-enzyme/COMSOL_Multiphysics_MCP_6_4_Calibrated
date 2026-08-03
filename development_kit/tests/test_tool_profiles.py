"""Static profile selection and registration compatibility tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from src.server import create_server, register_all_tools
from src.settings import RUNTIME_ENV, SETTINGS_PATH_ENV
from src.shared_session.contracts import SHARED_SERVER_FEATURE_ENV
from src.tools.catalog import FEATURE_NAMES, PROFILE_NAMES, TOOL_METADATA, snapshot_tool_schemas
from src.tools.profiles import DEFAULT_PROFILE, PROFILE_ENV_VAR, ProfileSelection, resolve_profile

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
ROOT = Path(__file__).parents[2]


def _tool_names(server) -> list[str]:
    return sorted(tool.name for tool in asyncio.run(server.list_tools()))


def _call_tool(server, name: str, arguments: dict) -> dict:
    result = asyncio.run(server.call_tool(name, arguments))
    if isinstance(result, dict):
        return result
    for block in result:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            value = json.loads(text)
            if isinstance(value, dict):
                return value
    raise ValueError("public FastMCP call did not return a JSON object")


def test_default_profile_is_core_after_h3_cutover(monkeypatch):
    monkeypatch.delenv(PROFILE_ENV_VAR, raising=False)
    server = create_server("default-core-profile-test")

    assert DEFAULT_PROFILE == "core"
    assert len(_tool_names(server)) == 47
    names = set(_tool_names(server))
    assert {"solver_status", "job_cancel", "model_load", "study_solve"} <= names
    assert "spectral_characterize" in names
    assert "spectral_model_compare" in names
    assert "convergence_evaluate" in names
    assert "branch_continuation_plan" in names
    assert {"evidence_integrity_status", "evidence_integrity_verify"} <= names
    assert {
        "wave_optics_preflight",
        "wave_optics_point_audit",
        "mim_patch_build",
        "mim_evaluate_spectral",
        "study_solve_async",
        "clientapi_property_set",
    }.isdisjoint(names)


def test_invalid_profile_falls_back_to_core_with_explicit_provenance():
    selection = resolve_profile("not-real", environ={})
    server = create_server("invalid-profile-test", profile=selection)

    assert selection.name == "core"
    assert selection.fallback_used is True
    assert selection.requested_name == "not-real"
    assert selection.source == "explicit_argument_invalid_profile_fallback"
    assert len(_tool_names(server)) == 47


def test_environment_profile_is_normalized(monkeypatch):
    monkeypatch.setenv(PROFILE_ENV_VAR, " WAVE_OPTICS ")
    selection = resolve_profile()

    assert selection.name == "wave_optics"
    assert selection.source == "environment"
    assert selection.default_used is False
    assert selection.environment_variable == PROFILE_ENV_VAR

    server = create_server("environment-wave-profile-test")
    assert len(_tool_names(server)) == 76
    assert "wave_optics_field_datasets" in _tool_names(server)
    assert "wave_optics_field_extract" in _tool_names(server)
    assert "wave_optics_material_expression_preview" in _tool_names(server)
    assert "wave_optics_incidence_preview" in _tool_names(server)
    assert "wave_optics_incidence_apply" in _tool_names(server)
    assert {
        "visual_review_capability_normalize",
        "visual_review_request_create",
        "visual_review_receipt_create",
        "visual_review_dual_evaluate",
    } <= set(_tool_names(server))


def test_profile_name_and_schema_snapshots_are_exact():
    expected_names = json.loads(
        (SNAPSHOT_DIR / "profile_tool_names.json").read_text(encoding="utf-8")
    )
    full_schemas = json.loads((SNAPSHOT_DIR / "full_tool_schemas.json").read_text(encoding="utf-8"))

    assert tuple(expected_names) == PROFILE_NAMES
    for profile in PROFILE_NAMES:
        server = create_server(f"{profile}-snapshot-test", profile=profile)
        actual_schemas = asyncio.run(snapshot_tool_schemas(server))
        assert sorted(actual_schemas) == expected_names[profile]
        assert actual_schemas == {name: full_schemas[name] for name in expected_names[profile]}


def test_feature_tool_name_snapshot_is_exact() -> None:
    expected = json.loads(
        (SNAPSHOT_DIR / "feature_tool_names.json").read_text(encoding="utf-8")
    )

    assert tuple(expected) == FEATURE_NAMES
    assert expected == {
        feature: sorted(
            name
            for name, metadata in TOOL_METADATA.items()
            if metadata.feature_gate == feature
        )
        for feature in FEATURE_NAMES
    }


def test_profile_registration_has_no_cross_server_leakage():
    core = create_server("isolated-core", profile="core")
    full = create_server("isolated-full", profile="full")
    semantic_selection = resolve_profile(
        "core",
        environ={"COMSOL_MCP_ENABLE_SEMANTIC_DOCS": "true"},
    )
    semantic = create_server("isolated-semantic", profile=semantic_selection)
    experimental = create_server("isolated-experimental", profile="experimental")

    assert len(_tool_names(core)) == 47
    assert len(_tool_names(full)) == 150
    assert len(_tool_names(semantic)) == 50
    assert len(_tool_names(experimental)) == 97
    assert _tool_names(core) != _tool_names(experimental)
    assert {"semantic_search", "semantic_status", "semantic_worker_reset"} <= set(
        _tool_names(semantic)
    )
    assert {"semantic_search", "semantic_status", "semantic_worker_reset"}.isdisjoint(
        _tool_names(core)
    )
    assert "wave_optics_incidence_preview" not in _tool_names(core)
    assert "wave_optics_incidence_apply" not in _tool_names(core)


def test_independent_feature_overlays_compose_with_any_base_profile() -> None:
    selection = resolve_profile(
        "wave_optics",
        environ={
            "COMSOL_MCP_ENABLE_SEMANTIC_DOCS": "true",
            SHARED_SERVER_FEATURE_ENV: "true",
        },
    )
    server = create_server("composed-feature-overlays", profile=selection)
    names = set(_tool_names(server))

    assert tuple(selection.enabled_features) == ("semantic_docs", "shared_server")
    assert selection.name == "wave_optics"
    assert {"semantic_search", "semantic_status", "semantic_worker_reset"} <= names
    assert {
        "shared_server_preflight",
        "shared_server_attach",
        "shared_model_adopt",
        "shared_model_lock",
    } <= names
    assert "wave_optics_preflight" in names
    assert len(names) == len(_tool_names(server))


def test_synthetic_feature_profiles_are_not_current_profile_names() -> None:
    assert "semantic_docs" not in PROFILE_NAMES
    assert "desktop_shared" not in PROFILE_NAMES

    semantic = resolve_profile("semantic_docs", environ={})
    shared = resolve_profile("desktop_shared", environ={})
    legacy_semantic = resolve_profile(environ={PROFILE_ENV_VAR: "semantic_docs"})
    legacy_shared = resolve_profile(environ={PROFILE_ENV_VAR: "desktop_shared"})

    assert (semantic.name, semantic.enabled_features, semantic.fallback_used) == (
        "core",
        (),
        True,
    )
    assert (shared.name, shared.enabled_features, shared.fallback_used) == ("core", (), True)
    assert legacy_semantic.enabled_features == ("semantic_docs",)
    assert legacy_shared.enabled_features == ("shared_server",)


def test_shared_server_feature_is_default_off_and_adds_only_its_delta():
    base = set(_tool_names(create_server("shared-off", profile="core")))
    selection = resolve_profile(
        "core",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )
    names = set(_tool_names(create_server("shared-on", profile=selection)))
    shared = {
        "shared_server_preflight",
        "shared_server_attach",
        "shared_server_detach",
        "shared_server_status",
        "shared_server_models",
        "shared_model_lock",
        "shared_model_verify",
        "shared_model_unlock",
        "shared_model_snapshot",
        "shared_model_adopt",
    }

    assert shared.isdisjoint(base)
    assert names == base | shared


def test_validated_shared_startup_selection_is_not_reresolved(monkeypatch):
    selection = resolve_profile(
        "core",
        environ={SHARED_SERVER_FEATURE_ENV: "true"},
    )
    monkeypatch.delenv(SHARED_SERVER_FEATURE_ENV, raising=False)

    server = create_server("validated-shared-selection", profile=selection)

    assert _call_tool(server, "capabilities", {})["active_profile"] == selection.name
    assert set(_tool_names(server)) - set(
        _tool_names(create_server("validated-shared-base", profile="core"))
    ) == {
        "shared_server_preflight",
        "shared_server_attach",
        "shared_server_detach",
        "shared_server_status",
        "shared_server_models",
        "shared_model_lock",
        "shared_model_verify",
        "shared_model_unlock",
        "shared_model_snapshot",
        "shared_model_adopt",
    }


def test_directly_constructed_profile_selection_cannot_register_tools():
    forged = ProfileSelection(
        name="core",
        environment_variable=PROFILE_ENV_VAR,
        default_used=False,
        source="forged",
    )

    with pytest.raises(ValueError, match="not produced by resolve_profile"):
        create_server("forged-shared-selection", profile=forged)


def test_profile_provenance_distinguishes_argument_environment_settings_and_default(
    tmp_path,
):
    explicit = resolve_profile("core", environ={})
    environment = resolve_profile(environ={PROFILE_ENV_VAR: "core"})
    fallback = resolve_profile(environ={RUNTIME_ENV: "D:/runtime"})
    settings_path = tmp_path / "settings.json"
    settings_path.write_text((ROOT / "settings.json").read_text(encoding="utf-8"), encoding="utf-8")
    settings = resolve_profile(environ={SETTINGS_PATH_ENV: str(settings_path)})

    assert (explicit.source, explicit.environment_variable, explicit.default_used) == (
        "explicit_argument",
        None,
        False,
    )
    assert (environment.source, environment.environment_variable, environment.default_used) == (
        "environment",
        PROFILE_ENV_VAR,
        False,
    )
    assert (
        fallback.name,
        fallback.source,
        fallback.environment_variable,
        fallback.default_used,
    ) == (
        "core",
        "settings",
        None,
        True,
    )
    assert (settings.source, settings.environment_variable, settings.default_used) == (
        "settings",
        SETTINGS_PATH_ENV,
        False,
    )


def test_existing_profiles_expose_no_shared_session_tools():
    shared = {
        "shared_server_preflight",
        "shared_server_attach",
        "shared_server_detach",
        "shared_server_status",
        "shared_server_models",
        "shared_model_lock",
        "shared_model_verify",
        "shared_model_unlock",
        "shared_model_snapshot",
        "shared_model_adopt",
    }
    for profile in PROFILE_NAMES:
        assert shared.isdisjoint(
            _tool_names(create_server(f"no-shared-{profile}", profile=profile))
        )


def test_registered_server_profile_is_immutable():
    server = create_server("immutable-profile", profile="core")

    register_all_tools(server, "core")
    with pytest.raises(ValueError, match="different startup selection"):
        register_all_tools(server, "full")


def test_capabilities_are_bound_to_each_server_profile(monkeypatch):
    monkeypatch.setenv(PROFILE_ENV_VAR, "full")
    core = create_server("core-capabilities", profile="core")
    wave = create_server("wave-capabilities", profile="wave_optics")

    core_result = _call_tool(core, "capabilities", {})
    wave_result = _call_tool(wave, "capabilities", {})

    assert core_result["active_profile"] == "core"
    assert core_result["tool_count"] == 47
    assert core_result["profile_source"]["source"] == "explicit_argument"
    assert wave_result["active_profile"] == "wave_optics"
    assert wave_result["tool_count"] == 76


@pytest.mark.parametrize("profile", ["core", "basic_fem", "wave_optics"])
def test_recommended_profiles_exclude_synthetic_async_solver(profile):
    names = set(_tool_names(create_server(f"no-synthetic-async-{profile}", profile=profile)))
    assert {
        "study_solve_async",
        "study_get_progress",
        "study_cancel",
        "study_wait",
    }.isdisjoint(names)


def test_compatibility_profile_and_durable_async_guidance_are_explicit():
    full = create_server("legacy-async-compatibility", profile="full")
    names = set(_tool_names(full))
    capabilities = _call_tool(full, "capabilities", {})

    assert {
        "study_solve_async",
        "study_get_progress",
        "study_cancel",
        "study_wait",
    } <= names
    assert capabilities["profile"] == "full"
    assert capabilities["server_safety"]["compatibility_profile_weaker_guarantees"] is True
    assert capabilities["experimental"]["async_solver"]["recommended_profile_exposure"] is False
    assert capabilities["experimental"]["async_solver"]["durable_alternative"] == (
        "job_submit/job_status/job_cancel/job_resume"
    )
