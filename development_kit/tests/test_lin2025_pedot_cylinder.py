from __future__ import annotations

import copy

import pytest

from comsol_mcp.research.lin2025_pedot_cylinder import (
    SCHEMA_NAME,
    normalize_lin2025_pedot_cylinder_fixture,
)


def _fixture() -> dict:
    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": "1.0.0",
        "adapter_id": "lin2025_pedot_cylinder_v1",
        "source_identity": {"source_sha256": "a" * 64, "tree_sha256": "b" * 64, "comsol_build": "6.4.0.293"},
        "geometry": {"lattice": "hexagonal_primitive", "period_um": 1.7, "pedot_height_um": 0.2, "air_above": True, "substrate_n": 1.5},
        "domains": {"substrate": 1, "pedot_cylinder": 5, "air": 4},
        "material_states": [{"state_id": "OX", "mapping_id": "ox"}, {"state_id": "MR", "mapping_id": "mr"}],
        "mutable_variables": [{"variable_id": "pedot_cylinder_radius_x"}, {"variable_id": "pedot_cylinder_radius_y"}],
        "temperature_k": 300.0,
    }


def test_fixture_freezes_topology_states_and_caller_temperature():
    result = normalize_lin2025_pedot_cylinder_fixture(_fixture())
    assert result["domains"] == {"air": 4, "pedot_cylinder": 5, "substrate": 1}
    assert [state["state_id"] for state in result["material_states"]] == ["OX", "MR"]
    assert result["temperature_k"] == 300.0
    assert len(result["fixture_fingerprint"]) == 64


@pytest.mark.parametrize("field", ["source_identity", "geometry", "domains", "material_states", "mutable_variables"])
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

