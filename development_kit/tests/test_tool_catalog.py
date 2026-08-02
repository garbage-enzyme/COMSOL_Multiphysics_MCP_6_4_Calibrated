"""Compatibility gates for the pre-field evidence MCP discovery surface."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest
import src.tools as tools_module
from mcp.server.fastmcp import FastMCP
from src.contracts.structural import bounded_public_schema
from src.knowledge import embedded as embedded_module
from src.knowledge.embedded import register_knowledge_tools
from src.knowledge.lexical_manual import register_lexical_manual_tools
from src.server import create_server
from src.tools.catalog import (
    PROFILE_NAMES,
    TOOL_METADATA,
    TOOL_SPECS,
    get_tool_metadata,
    registrars_for_profile,
    snapshot_tool_schemas,
    validate_tool_specs,
)

SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "full_tool_schemas.json"
BASELINE_SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "baseline_tool_schemas.json"


def test_full_tool_schema_snapshot_is_stable():
    server = create_server("full-schema-snapshot-test", profile="full")
    actual = asyncio.run(snapshot_tool_schemas(server))
    expected = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))

    assert len(actual) == 155
    assert actual == expected


def test_pre_h3_compatibility_snapshot_is_preserved():
    legacy = json.loads(BASELINE_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    current = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))

    assert len(legacy) == 96
    assert set(legacy) <= set(current)
    for name, legacy_schema in legacy.items():
        expected = bounded_public_schema(legacy_schema)
        observed = deepcopy(current[name])
        allowed_additions = set()
        if TOOL_METADATA[name].requires_model_revision:
            allowed_additions.add("expected_model_revision")
        if name == "mesh_convergence_study":
            allowed_additions.update({"config_id", "manifest_path", "source_model_path"})
        if name == "geometry_add_feature":
            assert expected["properties"].pop("kwargs")["type"] == "string"
            expected["required"].remove("kwargs")
            allowed_additions.add("properties")
        if name == "job_submit":
            migrated_spec = observed["properties"]["spec"]
            assert migrated_spec["discriminator"]["propertyName"] == "job_type"
            assert migrated_spec["oneOf"]
            assert observed.pop("$defs")
            observed["properties"]["spec"] = expected["properties"]["spec"]

        actual_additions = set(observed["properties"]) - set(expected["properties"])
        assert actual_additions == allowed_additions, name
        for field in allowed_additions:
            observed["properties"].pop(field)
        assert observed == expected, name


def test_registered_tool_names_are_unique():
    server = create_server("unique-tool-name-test", profile="full")
    tools = asyncio.run(server.list_tools())
    names = [tool.name for tool in tools]

    assert len(names) == len(set(names))


def test_every_registered_tool_has_complete_canonical_metadata():
    expected_names = set(json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8")))

    assert set(TOOL_METADATA) == expected_names
    assert len(TOOL_METADATA) == 155
    for name, metadata in TOOL_METADATA.items():
        assert metadata.name == name
        assert metadata.registrar.startswith("comsol_mcp.")
        assert metadata.group
        assert metadata.maturity in {"verified", "experimental", "deprecated"}
        assert metadata.side_effect_class
        assert metadata.concurrency_class in {
            "control_plane",
            "solver_free",
            "comsol_bound",
        }
        assert isinstance(metadata.requires_model_revision, bool)
        assert isinstance(metadata.advances_model_revision, bool)
        assert not metadata.advances_model_revision or metadata.requires_model_revision
        assert isinstance(metadata.starts_solver, bool)
        assert metadata.intended_profiles
        assert set(metadata.intended_profiles) <= set(PROFILE_NAMES)
        assert "full" in metadata.intended_profiles


def test_tool_specs_are_the_validated_canonical_registry():
    assert TOOL_SPECS is TOOL_METADATA
    assert validate_tool_specs() == {
        "valid": True,
        "tool_count": 155,
        "profile_count": len(PROFILE_NAMES),
    }
    for spec in TOOL_SPECS.values():
        assert spec.input_contract.startswith("tool-input/")
        assert spec.output_contract.startswith("tool-output/")
        assert dict(spec.structural_limits)["request_bytes"] > 0
        assert dict(spec.structural_limits)["response_bytes"] > 0


def test_licensed_acoustics_and_pde_tools_are_verified_in_basic_fem():
    mutation_tools = {
        "geometry_create_box_selection",
        "geometry_create_side_selections",
        "physics_add_pressure_acoustics",
        "physics_add_coefficient_form_pde",
        "physics_add_general_form_pde",
        "physics_add_weak_form_pde",
        "physics_configure_acoustic_boundary",
        "physics_setup_acoustic_boundaries",
        "physics_configure_pde_boundary",
        "physics_setup_pde_boundaries",
    }

    for name in mutation_tools:
        metadata = TOOL_METADATA[name]
        assert metadata.maturity == "verified"
        assert metadata.side_effect_class == "model_mutation"
        assert metadata.concurrency_class == "comsol_bound"
        assert metadata.requires_model_revision is True
        assert {"basic_fem", "experimental", "full"} <= set(metadata.intended_profiles)


def test_new_tool_without_explicit_side_effect_class_fails_closed(monkeypatch):
    from src.tools import catalog

    registrars = dict(catalog._TOOLS_BY_REGISTRAR)
    registrar = next(iter(registrars))
    monkeypatch.setattr(
        catalog,
        "_TOOLS_BY_REGISTRAR",
        {**registrars, registrar: (*registrars[registrar], "unclassified_tool")},
    )

    with pytest.raises(ValueError, match="no explicit side-effect classification"):
        catalog._build_registry()


def test_profile_registrar_selection_is_derived_from_tool_specs():
    core = registrars_for_profile("core")
    full = registrars_for_profile("full")
    assert core
    assert len(core) < len(full)
    assert "comsol_mcp.tools.wave_optics_audit.register_wave_optics_audit_tools" not in core
    assert "comsol_mcp.tools.wave_optics_audit.register_wave_optics_audit_tools" in full


def test_deprecated_foreground_sweep_has_a_durable_replacement():
    spec = TOOL_SPECS["study_staged_parametric_sweep"]
    assert spec.maturity == "deprecated"
    assert spec.deprecation_state == "deprecated"
    assert spec.replacement_tool == "job_submit"
    assert spec.sunset_release == "next_major"
    assert "wave_optics" not in spec.intended_profiles
    assert {"experimental", "full"} <= set(spec.intended_profiles)


def test_tool_spec_validation_rejects_conflicting_declarations_import_free():
    specs = tuple(TOOL_SPECS.values())
    with pytest.raises(ValueError, match="duplicate tool names"):
        validate_tool_specs((*specs, specs[0]))

    read_only = TOOL_SPECS["capabilities"]
    with pytest.raises(ValueError, match="read-only ToolSpec"):
        validate_tool_specs(
            {
                **TOOL_SPECS,
                read_only.name: replace(read_only, requires_model_revision=True),
            }
        )

    experimental = TOOL_SPECS["semantic_search"]
    with pytest.raises(ValueError, match="stable profile contains experimental"):
        validate_tool_specs(
            {
                **TOOL_SPECS,
                experimental.name: replace(
                    experimental,
                    intended_profiles=("core", "full"),
                ),
            }
        )

    duplicated_profile = replace(
        read_only,
        intended_profiles=(*read_only.intended_profiles, "full"),
    )
    with pytest.raises(ValueError, match="profiles are invalid"):
        validate_tool_specs({**TOOL_SPECS, read_only.name: duplicated_profile})

    with pytest.raises(ValueError, match="deprecation state is invalid"):
        validate_tool_specs(
            {**TOOL_SPECS, read_only.name: replace(read_only, deprecation_state="retired")}
        )

    with pytest.raises(ValueError, match="maturity/deprecation state is inconsistent"):
        validate_tool_specs(
            {**TOOL_SPECS, read_only.name: replace(read_only, maturity="deprecated")}
        )


@pytest.mark.parametrize(
    ("mutated", "message"),
    [
        (
            lambda spec: replace(spec, starts_solver=True),
            "solver-starting ToolSpec has impossible effects",
        ),
        (
            lambda spec: replace(
                spec,
                side_effect_class="model_mutation",
                advances_model_revision=True,
                requires_model_revision=False,
            ),
            "advancing ToolSpec lacks revision requirement",
        ),
        (
            lambda spec: replace(
                spec,
                maturity="deprecated",
                deprecation_state="deprecated",
                replacement_tool=None,
            ),
            "deprecated ToolSpec lacks replacement",
        ),
        (
            lambda spec: replace(spec, replacement_tool="missing_replacement"),
            "ToolSpec replacement is unknown",
        ),
        (
            lambda spec: replace(spec, intended_profiles=("core",)),
            "ToolSpec compatibility profile is missing",
        ),
    ],
)
def test_tool_spec_validation_rejects_high_impact_relationship_breaks(mutated, message):
    read_only = TOOL_SPECS["capabilities"]

    with pytest.raises(ValueError, match=message):
        validate_tool_specs({**TOOL_SPECS, read_only.name: mutated(read_only)})


def test_schema_snapshot_rejects_duplicate_registered_names():
    duplicate = SimpleNamespace(name="duplicate", inputSchema={"type": "object"})

    class DuplicateServer:
        async def list_tools(self):
            return [duplicate, duplicate]

    with pytest.raises(ValueError, match="duplicate registered tool names"):
        asyncio.run(snapshot_tool_schemas(DuplicateServer()))


def test_tools_package_exports_only_profile_guarded_registration():
    assert tools_module.__all__ == ["register_tool_modules"]
    assert not hasattr(tools_module, "TOOL_REGISTRARS")


def test_unknown_tool_metadata_fails_closed():
    try:
        get_tool_metadata("not_a_registered_tool")
    except KeyError as exc:
        assert "No canonical metadata" in str(exc)
    else:
        raise AssertionError("unknown tools must not receive implicit metadata")


def test_embedded_knowledge_uses_one_bounded_regular_file_read(tmp_path, monkeypatch):
    prompt = tmp_path / "mph_api.md"
    prompt.write_text("bounded guidance", encoding="utf-8")
    monkeypatch.setattr(embedded_module, "KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setattr(
        Path,
        "exists",
        lambda _path: pytest.fail("embedded knowledge must not pre-check existence"),
    )

    assert embedded_module._load_knowledge_file("mph_api") == "bounded guidance"


def test_embedded_knowledge_responses_do_not_expose_module_state():
    docs = embedded_module.list_docs()
    docs["topics"][0]["keywords"].append("injected")
    guide = embedded_module.get_physics_guide("electrostatics")
    guide["guide"]["tips"].clear()
    troubleshoot = embedded_module.get_troubleshoot("mesh_failed")
    troubleshoot["solutions"].clear()
    practices = embedded_module.get_best_practices("mesh")
    practices["best_practices"]["tips"].clear()

    assert "injected" not in embedded_module.list_docs()["topics"][0]["keywords"]
    assert embedded_module.get_physics_guide("electrostatics")["guide"]["tips"]
    assert embedded_module.get_troubleshoot("mesh_failed")["solutions"]
    assert embedded_module.get_best_practices("mesh")["best_practices"]["tips"]


def test_embedded_guides_publish_exact_acoustics_and_pde_boundaries():
    acoustic = embedded_module.get_physics_guide("pressure_acoustics")
    pde = embedded_module.get_physics_guide("mathematical_pde")

    assert acoustic["guide"]["tool_to_add"] == "physics_add_pressure_acoustics"
    assert acoustic["guide"]["common_boundary_conditions"] == [
        "SoundHard",
        "SoundSoft",
        "Pressure",
        "Impedance",
        "NormalAcceleration",
        "NormalVelocity",
        "PlaneWaveRadiation",
    ]
    assert pde["guide"]["common_boundary_conditions"] == [
        "DirichletBoundary",
        "FluxBoundary",
        "ZeroFluxBoundary",
        "PeriodicCondition",
    ]


def test_metadata_registrars_match_actual_registration():
    registrars = []
    for registrar_path in registrars_for_profile("full"):
        module_name, symbol_name = registrar_path.rsplit(".", 1)
        registrars.append(getattr(import_module(module_name), symbol_name))
    registrars.extend((register_knowledge_tools, register_lexical_manual_tools))

    for registrar in registrars:
        server = FastMCP(f"metadata-{registrar.__name__}")
        registrar(server)
        registrar_name = f"{registrar.__module__}.{registrar.__name__}"
        expected = {
            name for name, metadata in TOOL_METADATA.items() if metadata.registrar == registrar_name
        }
        assert set(server._tool_manager._tools) == expected


def test_catalog_import_cannot_start_comsol():
    code = """
import mph
mph.Client = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('Client called'))
from src.tools.catalog import TOOL_METADATA
assert len(TOOL_METADATA) == 155
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
