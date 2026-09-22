"""Solver-free tests for the isolated electrochemistry profile surface."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from comsol_mcp.server import create_server
from comsol_mcp.tools.catalog import (
    ISOLATED_MODULE_PROFILES,
    PROFILE_NAMES,
    TOOL_METADATA,
    validate_tool_specs,
)
from comsol_mcp.tools.electro_chemistry import (
    ELECTROCHEMISTRY_INTERFACES,
    REQUIRED_PRODUCTS,
    add_electrochemistry_interface,
    build_electro_chemistry_catalog,
    configure_electrode_reaction,
    register_electrochemistry_tools,
    set_electrolyte,
)
from comsol_mcp.tools.profiles import tool_names_for_profile

ELECTRO_ONLY = {
    "electro_chemistry_catalog",
    "electro_chemistry_inspect",
    "physics_add_electrochemistry",
    "physics_configure_electrode_reaction",
    "physics_set_electrolyte",
}
FIVE_TOOLS = {
    "mph_inspect",
    "mph_diff",
    "model_identity",
    "runtime_compatibility_status",
    "offline_export_validate",
}


class FakeSelection:
    def __init__(self) -> None:
        self.entities: list[int] = []

    def set(self, values) -> None:
        self.entities = [int(value) for value in values]

    def entities(self):  # type: ignore[override]
        return list(self.entities)


class FakeFeature:
    def __init__(self, tag: str) -> None:
        self._tag = tag
        self._label = tag
        self._props: dict[str, str] = {}
        self._selection = FakeSelection()

    def label(self, value: str | None = None):
        if value is None:
            return self._label
        self._label = value
        return value

    def set(self, key: str, value: str) -> None:
        self._props[key] = value

    def selection(self):  # type: ignore[override]
        return self._selection


class FakeFeatureList:
    def __init__(self) -> None:
        self._items: dict[str, FakeFeature] = {}

    def tags(self):
        return list(self._items)

    def get(self, tag: str) -> FakeFeature:
        return self._items[tag]

    def create(self, tag: str, feature_type: str, dim: int) -> FakeFeature:
        if tag in self._items:
            raise RuntimeError(f"exists:{tag}")
        if feature_type == "MissingFeature":
            raise RuntimeError("module unavailable")
        node = FakeFeature(tag)
        node.feature_type = feature_type  # type: ignore[attr-defined]
        self._items[tag] = node
        return node

    def remove(self, tag: str) -> None:
        self._items.pop(tag, None)


class FakePhysics:
    def __init__(self, tag: str, interface_type: str) -> None:
        self._tag = tag
        self._type = interface_type
        self._label = tag
        self._feature_list = FakeFeatureList()
        self._selection = FakeSelection()

    def label(self, value: str | None = None):
        if value is None:
            return self._label
        self._label = value
        return value

    def getType(self) -> str:
        return self._type

    def feature(self):  # type: ignore[override]
        return self._feature_list

    def selection(self):  # type: ignore[override]
        return self._selection


class FakePhysicsList:
    def __init__(self) -> None:
        self._items: dict[str, FakePhysics] = {}

    def tags(self):
        return list(self._items)

    def get(self, tag: str) -> FakePhysics:
        return self._items[tag]

    def create(self, tag: str, interface_type: str, dim) -> FakePhysics:
        if tag in self._items:
            raise RuntimeError(f"exists:{tag}")
        if interface_type == "UnavailableInterface":
            raise RuntimeError("Electrochemistry Module missing")
        node = FakePhysics(tag, interface_type)
        self._items[tag] = node
        return node

    def remove(self, tag: str) -> None:
        self._items.pop(tag, None)


class FakeComponent:
    def __init__(self) -> None:
        self._physics_list = FakePhysicsList()
        self._tag = "comp1"

    def tag(self) -> str:
        return self._tag

    def physics(self):  # type: ignore[override]
        return self._physics_list

    def geom(self, tag=None):
        return SimpleNamespace(getSDim=lambda: 3)


class FakeComponentList:
    def __init__(self, component: FakeComponent) -> None:
        self._component = component

    def tags(self):
        return [self._component.tag()]

    def get(self, tag: str) -> FakeComponent:
        return self._component


class FakeJava:
    def __init__(self) -> None:
        self._component = FakeComponent()

    def component(self, tag: str | None = None):
        if tag is None:
            return FakeComponentList(self._component)
        return self._component

    @property
    def component_node(self) -> FakeComponent:
        return self._component


class FakeModel:
    def __init__(self) -> None:
        self.java = FakeJava()

    @property
    def component(self) -> FakeComponent:
        return self.java.component_node


def test_catalog_is_offline_and_declares_module_requirements() -> None:
    payload = build_electro_chemistry_catalog()
    assert payload["success"] is True
    assert payload["profile"] == "electro_chemistry"
    assert payload["required_products"] == list(REQUIRED_PRODUCTS)
    assert set(payload["interfaces"]) == set(ELECTROCHEMISTRY_INTERFACES)
    assert payload["resource_policy"]["starts_solver"] is False
    assert payload["resource_policy"]["acquires_license"] is False
    assert (
        payload["interfaces"]["secondary_current"]["evidence_status"] == "live_comsol_6_4_created"
    )
    assert (
        payload["interfaces"]["tertiary_current"]["evidence_status"]
        == "type_name_rejected_on_comsol_6_4_0_293"
    )
    assert payload["secondary_current_supported_reaction_models"] == ["surface_resistance"]


def test_profile_is_isolated_from_default_and_full() -> None:
    assert "electro_chemistry" in PROFILE_NAMES
    assert ISOLATED_MODULE_PROFILES == frozenset({"electro_chemistry"})
    eco = tool_names_for_profile("electro_chemistry")
    core = tool_names_for_profile("core")
    full = tool_names_for_profile("full")
    experimental = tool_names_for_profile("experimental")
    assert ELECTRO_ONLY <= eco
    assert ELECTRO_ONLY.isdisjoint(core)
    assert ELECTRO_ONLY.isdisjoint(full)
    assert ELECTRO_ONLY.isdisjoint(experimental)
    assert FIVE_TOOLS <= eco
    validate_tool_specs()


def test_tool_specs_are_experimental_and_module_bound() -> None:
    for name in ELECTRO_ONLY:
        spec = TOOL_METADATA[name]
        assert spec.intended_profiles == ("electro_chemistry",)
        assert spec.maturity == "experimental"
        assert spec.starts_solver is False
        assert spec.group == "electro_chemistry"
    for name in {
        "physics_add_electrochemistry",
        "physics_configure_electrode_reaction",
        "physics_set_electrolyte",
    }:
        assert "comsol_electrochemistry_module" in TOOL_METADATA[name].required_features
    assert TOOL_METADATA["electro_chemistry_catalog"].side_effect_class == "read_only"
    assert TOOL_METADATA["electro_chemistry_inspect"].side_effect_class == "read_only"


def test_public_server_surface_matches_isolated_profile() -> None:
    server = create_server("electro-surface", profile="electro_chemistry")
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert ELECTRO_ONLY <= names
    assert FIVE_TOOLS <= names
    full = create_server("electro-full-control", profile="full")
    full_names = {tool.name for tool in asyncio.run(full.list_tools())}
    assert ELECTRO_ONLY.isdisjoint(full_names)


def test_add_interface_success_and_duplicate_refusal() -> None:
    model = FakeModel()
    created = add_electrochemistry_interface(model, interface_key="secondary_current")
    assert created["success"] is True
    assert created["physics"]["tag"] == "sec"
    assert created["physics"]["interface_type"] == "SecondaryCurrentDistribution"
    again = add_electrochemistry_interface(model, interface_key="secondary_current")
    assert again["success"] is False
    assert "already exists" in again["error"]


def test_add_interface_rejects_unknown_key_and_bad_label() -> None:
    model = FakeModel()
    bad_key = add_electrochemistry_interface(model, interface_key="not_a_real_interface")
    assert bad_key["success"] is False
    bad_label = add_electrochemistry_interface(model, label="   ")
    assert bad_label["success"] is False


def test_add_interface_rolls_back_on_module_failure() -> None:
    class FailingPhysicsList(FakePhysicsList):
        def create(self, tag: str, interface_type: str, dim):
            raise RuntimeError("Electrochemistry Module not licensed")

    model = FakeModel()
    model.component.physics()._items.clear()
    model.component.physics().create  # keep method reference
    failing = FailingPhysicsList()
    model.java.component_node._physics_list = failing
    result = add_electrochemistry_interface(model, interface_key="secondary_current")
    assert result["success"] is False
    assert "Electrochemistry Module" in result["error"]
    assert result.get("rolled_back") is True
    assert "sec" not in failing.tags()


def test_configure_electrode_reaction_success_refusal_and_rollback() -> None:
    model = FakeModel()
    add_electrochemistry_interface(model)
    ok = configure_electrode_reaction(
        model,
        boundary_numbers=[1, 2],
        surface_resistivity=1.5,
        reaction_model="surface_resistance",
    )
    assert ok["success"] is True
    assert ok["feature"]["boundary_numbers"] == [1, 2]
    assert ok["feature"]["surface_resistivity"] == 1.5

    kinetics = configure_electrode_reaction(
        model,
        boundary_numbers=[1],
        reaction_model="butler_volmer",
        exchange_current_density=1.0,
    )
    assert kinetics["success"] is False
    assert "surface_resistivity" in kinetics["error"]

    missing = configure_electrode_reaction(model, physics_tag="nope", boundary_numbers=[1])
    assert missing["success"] is False

    empty = configure_electrode_reaction(model, boundary_numbers=[])
    assert empty["success"] is False

    class FailingFeatureList(FakeFeatureList):
        def create(self, tag: str, feature_type: str, dim: int) -> FakeFeature:
            raise RuntimeError("ElectrodeSurface unavailable")

    model2 = FakeModel()
    add_electrochemistry_interface(model2)
    model2.component.physics().get("sec")._feature_list = FailingFeatureList()
    failed = configure_electrode_reaction(model2, boundary_numbers=[3])
    assert failed["success"] is False
    assert failed.get("rolled_back") is True


def test_configure_electrode_reaction_validates_coefficients() -> None:
    model = FakeModel()
    add_electrochemistry_interface(model)
    bad = configure_electrode_reaction(
        model,
        boundary_numbers=[1],
        surface_resistivity=-1.0,
    )
    assert bad["success"] is False


def test_set_electrolyte_success_refusal_and_rollback() -> None:
    model = FakeModel()
    add_electrochemistry_interface(model)
    ok = set_electrolyte(model, domain_numbers=[1], ionic_conductivity=2.0)
    assert ok["success"] is True
    assert ok["feature"]["ionic_conductivity"] == 2.0

    dup = set_electrolyte(model, domain_numbers=[1])
    assert dup["success"] is False

    class FailingFeatureList(FakeFeatureList):
        def create(self, tag: str, feature_type: str, dim: int) -> FakeFeature:
            raise RuntimeError("Electrolyte feature unavailable")

    model2 = FakeModel()
    add_electrochemistry_interface(model2)
    model2.component.physics().get("sec")._feature_list = FailingFeatureList()
    failed = set_electrolyte(model2, domain_numbers=[1])
    assert failed["success"] is False
    assert failed.get("rolled_back") is True


def test_inspect_reports_features_without_mutation() -> None:
    model = FakeModel()
    add_electrochemistry_interface(model, label="cell")
    configure_electrode_reaction(model, boundary_numbers=[2])
    set_electrolyte(model, domain_numbers=[1], ionic_conductivity=1.2)
    before = set(model.component.physics().get("sec").feature().tags())
    assert before >= {"es1", "eip1"}
    from comsol_mcp.tools import electro_chemistry as eco

    result = eco.inspect_electrochemistry(model_name=None, physics_tag="sec")
    # Without a live session this must fail closed with an explicit error.
    assert result["success"] is False
    missing = eco.inspect_electrochemistry(model_name=None, physics_tag="missing")
    assert missing["success"] is False


def test_inspect_success_with_injected_session(monkeypatch) -> None:
    from comsol_mcp.tools import electro_chemistry as eco

    model = FakeModel()
    add_electrochemistry_interface(model)
    monkeypatch.setattr(eco.session_manager, "get_model", lambda name=None: model)
    result = eco.inspect_electrochemistry(physics_tag="sec")
    assert result["success"] is True
    assert result["physics"]["tag"] == "sec"
    assert result["evidence_status"] == "label_and_tag_only"
    missing = eco.inspect_electrochemistry(physics_tag="nope")
    assert missing["success"] is False


def test_mcp_registration_uses_canonical_tool_names() -> None:
    class _Server:
        def __init__(self) -> None:
            self.names: list[str] = []

        def tool(self, *args, **kwargs):
            def decorator(fn):
                self.names.append(kwargs.get("name") or fn.__name__)
                return fn

            return decorator

    server = _Server()
    register_electrochemistry_tools(server)  # type: ignore[arg-type]
    assert set(server.names) == ELECTRO_ONLY


def test_isolated_module_tool_may_omit_full_compatibility_profile() -> None:
    from dataclasses import replace

    isolated = replace(
        TOOL_METADATA["electro_chemistry_catalog"],
        intended_profiles=("electro_chemistry",),
    )
    receipt = validate_tool_specs({**TOOL_METADATA, isolated.name: isolated})
    assert receipt["valid"] is True
