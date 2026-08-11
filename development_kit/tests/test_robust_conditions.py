"""Solver-free immutable robust optimization condition-table tests."""

from __future__ import annotations

import copy
import itertools

import pytest

from comsol_mcp.research.robust_conditions import normalize_optimization_condition_table


def _state(state_id: str, marker: str) -> dict:
    return {
        "schema_name": "comsol_mcp.optimization_material_state",
        "schema_version": "1.0.0",
        "state_id": state_id,
        "material_ledger_sha256": marker * 64,
        "optical_property_source_sha256": marker.upper() * 64,
        "temperature_k": 300.0,
        "provenance_disposition": "private_input_hash_bound",
    }


def _table() -> dict:
    rows = []
    coordinates = itertools.product(
        [8.0e-6, 9.0e-6, 10.0e-6],
        [0.0, 30.0],
        ["declared_s", "declared_p"],
        ["OX", "MR"],
    )
    for index, (wavelength, elevation, polarization, state_id) in enumerate(coordinates):
        rows.append(
            {
                "condition_id": f"condition-{index:02d}",
                "order": index,
                "wavelength_m": wavelength,
                "incidence_elevation_deg": elevation,
                "incidence_azimuth_deg": 0.0,
                "polarization_basis_id": polarization,
                "excitation_sha256": "e" * 64,
                "material_state_id": state_id,
                "objective_role": "objective",
                "observable_id": "transmission_order_0_0",
                "weight": 1.0,
                "target": None,
                "scale": 1.0,
                "active": True,
            }
        )
    return {
        "schema_name": "comsol_mcp.optimization_condition_table",
        "schema_version": "1.0.0",
        "table_id": "pedot-acceptance-24",
        "material_states": [_state("OX", "a"), _state("MR", "b")],
        "conditions": rows,
        "completeness": {"mode": "cartesian_complete", "sparse_justification": None},
    }


def test_configurable_24_condition_ox_mr_table_is_canonical_and_immutable():
    first = normalize_optimization_condition_table(_table())
    assert len(first["conditions"]) == 24
    assert first["completeness"]["dimension_cardinalities"] == {
        "wavelength": 3,
        "incidence_elevation": 2,
        "incidence_azimuth": 1,
        "polarization_basis": 2,
        "material_state": 2,
    }
    assert [item["state_id"] for item in first["material_states"]] == ["OX", "MR"]
    assert normalize_optimization_condition_table(first) == first


def test_condition_table_rejects_missing_cartesian_row_unless_sparse_is_explicit():
    value = _table()
    value["conditions"].pop()
    with pytest.raises(ValueError, match="missing Cartesian combinations"):
        normalize_optimization_condition_table(value)
    value["completeness"] = {
        "mode": "explicit_sparse",
        "sparse_justification": "Caller intentionally omits one unavailable condition.",
    }
    assert normalize_optimization_condition_table(value)["completeness"]["mode"] == (
        "explicit_sparse"
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["conditions"][0].update(order=1), "canonical zero-based"),
        (
            lambda value: value["conditions"][0].update(material_state_id="PR"),
            "not declared",
        ),
        (lambda value: value["conditions"][0].update(weight=0.0), "allowed range"),
        (lambda value: value["conditions"][0].update(active=1), "must be boolean"),
        (
            lambda value: value["conditions"][1].update(
                **{
                    key: value["conditions"][0][key]
                    for key in (
                        "wavelength_m",
                        "incidence_elevation_deg",
                        "incidence_azimuth_deg",
                        "polarization_basis_id",
                        "material_state_id",
                    )
                }
            ),
            "coordinates must be unique",
        ),
    ],
)
def test_condition_table_rejects_order_state_weight_boolean_or_coordinate_drift(
    mutation, message
):
    value = _table()
    mutation(value)
    with pytest.raises(ValueError, match=message):
        normalize_optimization_condition_table(value)


def test_condition_table_rejects_tampered_nested_or_table_fingerprints():
    normalized = normalize_optimization_condition_table(_table())
    tampered_state = copy.deepcopy(normalized)
    tampered_state["material_states"][0]["temperature_k"] = 301.0
    with pytest.raises(ValueError, match="material state fingerprint"):
        normalize_optimization_condition_table(tampered_state)
    tampered_table = copy.deepcopy(normalized)
    tampered_table["conditions"][0]["scale"] = 2.0
    with pytest.raises(ValueError, match="condition table fingerprint"):
        normalize_optimization_condition_table(tampered_table)
