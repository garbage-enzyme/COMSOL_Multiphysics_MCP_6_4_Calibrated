from __future__ import annotations

import pytest

from comsol_mcp.research.lin2025_pedot_cylinder import (
    SCHEMA_NAME,
    compile_lin2025_pedot_cylinder_binding,
    normalize_lin2025_pedot_cylinder_fixture,
    validate_lin2025_pedot_cylinder_tree,
)


def _fixture() -> dict:
    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": "1.0.0",
        "adapter_id": "lin2025_pedot_cylinder_v1",
        "source_identity": {
            "source_sha256": "a" * 64,
            "tree_sha256": "b" * 64,
            "comsol_build": "6.4.0.293",
        },
        "geometry": {
            "lattice": "hexagonal_primitive",
            "period_um": 1.7,
            "pedot_height_um": 0.2,
            "air_above": True,
            "substrate_n": 1.5,
        },
        "domains": {"substrate": 1, "pedot_cylinder": 5, "air": 4},
        "material_states": [
            {"state_id": "OX", "mapping_id": "ox"},
            {"state_id": "MR", "mapping_id": "mr"},
        ],
        "mutable_variables": [
            {"variable_id": "pedot_cylinder_radius_x"},
            {"variable_id": "pedot_cylinder_radius_y"},
        ],
        "temperature_k": 300.0,
    }


def test_fixture_freezes_topology_states_and_caller_temperature():
    result = normalize_lin2025_pedot_cylinder_fixture(_fixture())
    assert result["domains"] == {"air": 4, "pedot_cylinder": 5, "substrate": 1}
    assert [state["state_id"] for state in result["material_states"]] == ["OX", "MR"]
    assert result["temperature_k"] == 300.0
    assert len(result["fixture_fingerprint"]) == 64


@pytest.mark.parametrize(
    "field",
    ["source_identity", "geometry", "domains", "material_states", "mutable_variables"],
)
def test_fixture_rejects_contract_drift(field):
    value = _fixture()
    if field == "source_identity":
        value[field]["source_sha256"] = "0" * 63
    elif field == "geometry":
        value[field]["air_above"] = False
    elif field == "domains":
        value[field]["pedot_cylinder"] = 0
    elif field == "material_states":
        value[field].reverse()
    else:
        value[field].reverse()
    with pytest.raises(ValueError):
        normalize_lin2025_pedot_cylinder_fixture(value)


def test_fixture_normalization_is_defensive():
    value = _fixture()
    result = normalize_lin2025_pedot_cylinder_fixture(value)
    result["domains"]["air"] = 99
    assert normalize_lin2025_pedot_cylinder_fixture(value)["domains"]["air"] == 4


def test_binding_freezes_source_domain_states_and_variables():
    fixture = normalize_lin2025_pedot_cylinder_fixture(_fixture())
    support = {
        "adapter_id": "lin2025_pedot_cylinder_v1",
        "source_identity": fixture["source_identity"]["source_sha256"],
        "support_fingerprint": "c" * 64,
        "variables": [
            {"variable_id": "pedot_cylinder_radius_x"},
            {"variable_id": "pedot_cylinder_radius_y"},
        ],
    }
    policy = {"adapter_id": "lin2025_pedot_cylinder_v1", "policy_fingerprint": "d" * 64}
    binding = compile_lin2025_pedot_cylinder_binding(fixture, support, policy)
    assert binding["pedot_domain"] == 5
    assert binding["material_state_ids"] == ["OX", "MR"]
    assert len(binding["binding_fingerprint"]) == 64


def test_binding_rejects_wrong_adapter_or_variable_order():
    fixture = _fixture()
    support = {
        "adapter_id": "wrong",
        "source_identity": "a" * 64,
        "support_fingerprint": "c" * 64,
        "variables": [],
    }
    with pytest.raises(ValueError, match="adapter identity"):
        compile_lin2025_pedot_cylinder_binding(fixture, support, {"adapter_id": "wrong"})


def test_tree_readback_freezes_domains_z_bounds_state_and_no_gold():
    fixture = _fixture()
    readback = {
        "source_sha256": "a" * 64,
        "domain_map": {"substrate": 1, "pedot_cylinder": 5, "air": 4},
        "domain_z_bounds_um": {"5": [0.0, 0.2]},
        "material_tags": {"pedot_cylinder": "OX"},
        "feature_tags": ["ewfd", "mesh1"],
    }
    result = validate_lin2025_pedot_cylinder_tree(fixture, readback)
    assert result["pedot_state"] == "OX"
    assert "au" not in " ".join(result["feature_tags"]).casefold()


@pytest.mark.parametrize("mutate", [
    lambda value: value["domain_map"].update(pedot_cylinder=3),
    lambda value: value["domain_z_bounds_um"].update({"5": [0.1, 0.2]}),
    lambda value: value["material_tags"].update(pedot_cylinder="air"),
    lambda value: value["feature_tags"].append("mat_au"),
])
def test_tree_readback_rejects_physical_identity_drift(mutate):
    fixture = _fixture()
    value = {
        "source_sha256": "a" * 64,
        "domain_map": {"substrate": 1, "pedot_cylinder": 5, "air": 4},
        "domain_z_bounds_um": {"5": [0.0, 0.2]},
        "material_tags": {"pedot_cylinder": "OX"},
        "feature_tags": ["ewfd", "mesh1"],
    }
    mutate(value)
    with pytest.raises(ValueError):
        validate_lin2025_pedot_cylinder_tree(fixture, value)
