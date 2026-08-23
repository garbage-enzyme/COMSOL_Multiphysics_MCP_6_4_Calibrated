"""Optical-property tensor mapping tests."""

from __future__ import annotations

import copy
import itertools

import pytest

from comsol_mcp.research.robust_conditions import normalize_optimization_condition_table
from comsol_mcp.research.robust_material_mapping import normalize_optical_property_mapping
from comsol_mcp.research.robust_material_tensor_rows import bind_robust_material_tensor_rows


def _mapping(marker: str = "a", state: str = "OX") -> dict:
    return {
        "schema_name": "comsol_mcp.optimization_optical_property_mapping",
        "schema_version": "1.0.0",
        "mapping_id": f"pedot-{state.lower()}-tensor",
        "active_domain_id": "active_material_domain",
        "source_sha256": marker * 64,
        "independent_variable": {
            "kind": "vacuum_wavelength",
            "source_column": "wavelength_nm",
            "source_unit": "nm",
            "valid_min": 500.0,
            "valid_max": 1500.0,
            "interpolation_method": "linear",
            "extrapolation": "forbidden",
        },
        "tensor": {
            "basis": "model_cartesian",
            "form": "diagonal",
            "off_diagonal_zero": True,
            "components": [
                {
                    "component": "xx",
                    "source_axis_id": "epsilon1",
                    "real_column": "epsilon1_real",
                    "imaginary_column": "comsol_epsilon1_imag",
                },
                {
                    "component": "yy",
                    "source_axis_id": "epsilon1",
                    "real_column": "epsilon1_real",
                    "imaginary_column": "comsol_epsilon1_imag",
                },
                {
                    "component": "zz",
                    "source_axis_id": "epsilon2",
                    "real_column": "epsilon2_real",
                    "imaginary_column": "comsol_epsilon2_imag",
                },
            ],
        },
        "time_harmonic_convention": "exp_positive_i_omega_t",
    }


def test_pedot_tensor_mapping_freezes_axes_signed_imaginary_columns_and_no_extrapolation():
    mapping = normalize_optical_property_mapping(_mapping())
    assert [item["source_axis_id"] for item in mapping["tensor"]["components"]] == [
        "epsilon1",
        "epsilon1",
        "epsilon2",
    ]
    assert [item["imaginary_column"] for item in mapping["tensor"]["components"]] == [
        "comsol_epsilon1_imag",
        "comsol_epsilon1_imag",
        "comsol_epsilon2_imag",
    ]
    assert mapping["independent_variable"]["extrapolation"] == "forbidden"
    assert normalize_optical_property_mapping(mapping) == mapping


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value["tensor"].update(off_diagonal_zero=False),
            "diagonal model-Cartesian",
        ),
        (
            lambda value: value["tensor"]["components"].reverse(),
            "component order",
        ),
        (
            lambda value: value["independent_variable"].update(extrapolation="linear"),
            "must be forbidden",
        ),
        (
            lambda value: value.update(time_harmonic_convention="unspecified"),
            "time-harmonic",
        ),
    ],
)
def test_mapping_rejects_ambiguous_tensor_or_wave_conventions(mutation, message):
    value = _mapping()
    mutation(value)
    with pytest.raises(ValueError, match=message):
        normalize_optical_property_mapping(value)


def test_mapping_fingerprint_rejects_column_or_range_tampering():
    normalized = normalize_optical_property_mapping(_mapping())
    tampered = copy.deepcopy(normalized)
    tampered["tensor"]["components"][2]["real_column"] = "epsilon1_real"
    with pytest.raises(ValueError, match="fingerprint"):
        normalize_optical_property_mapping(tampered)


def test_mapping_rejects_explicit_null_or_malformed_fingerprint():
    # An explicit JSON null must not masquerade as an absent field and skip
    # fingerprint verification.
    value = _mapping()
    value["mapping_fingerprint"] = None
    with pytest.raises(ValueError, match="SHA-256 hex digest"):
        normalize_optical_property_mapping(value)
    value = _mapping()
    value["mapping_fingerprint"] = "not-a-fingerprint"
    with pytest.raises(ValueError, match="SHA-256 hex digest"):
        normalize_optical_property_mapping(value)
    value = _mapping()
    value["mapping_fingerprint"] = "b" * 64
    with pytest.raises(ValueError, match="fingerprint is invalid"):
        normalize_optical_property_mapping(value)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["independent_variable"].update(interpolation_method={"a": 1}),
        lambda value: value["independent_variable"].update(interpolation_method=["linear"]),
        lambda value: value.update(time_harmonic_convention=["exp_positive_i_omega_t"]),
    ],
)
def test_mapping_membership_leaves_raise_value_errors_not_type_errors(mutation):
    value = _mapping()
    mutation(value)
    with pytest.raises(ValueError):
        normalize_optical_property_mapping(value)


def _tensor_rows() -> dict:
    return {
        "schema_name": "comsol_mcp.robust_material_tensor_rows",
        "schema_version": "1.0.0",
        "source_sha256": "c" * 64,
        "states": [
            {
                "state_id": state_id,
                "source_sha256": marker.upper() * 64,
                "rows": [
                    {
                        "wavelength_m": wavelength,
                        "xx_real": 2.0,
                        "xx_imag": -0.1,
                        "yy_real": 2.0,
                        "yy_imag": -0.1,
                        "zz_real": 3.0,
                        "zz_imag": -0.2,
                    }
                    for wavelength in (8e-6, 9e-6, 10e-6)
                ],
            }
            for state_id, marker in (("OX", "a"), ("MR", "b"))
        ],
    }


def _condition_table() -> dict:
    states = []
    for state_id, marker in (("OX", "a"), ("MR", "b")):
        states.append(
            {
                "schema_name": "comsol_mcp.optimization_material_state",
                "schema_version": "1.0.0",
                "state_id": state_id,
                "material_ledger_sha256": marker * 64,
                "optical_property_source_sha256": marker.upper() * 64,
                "optical_property_mapping": _mapping(marker.upper(), state_id),
                "temperature_k": 300.0,
                "provenance_disposition": "private_input_hash_bound",
            }
        )
    rows = []
    for index, (wavelength, state_id) in enumerate(
        itertools.product((8e-6, 9e-6, 10e-6), ("OX", "MR"))
    ):
        rows.append(
            {
                "condition_id": f"condition-{index}",
                "order": index,
                "wavelength_m": wavelength,
                "incidence_elevation_deg": 0.0,
                "incidence_azimuth_deg": 0.0,
                "polarization_basis_id": "x_linear",
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
        "table_id": "tensor-binding",
        "material_states": states,
        "conditions": rows,
        "completeness": {"mode": "cartesian_complete", "sparse_justification": None},
    }


def test_tensor_rows_bind_state_sources_temperature_and_active_wavelengths():
    table = normalize_optimization_condition_table(_condition_table())
    binding = bind_robust_material_tensor_rows(_tensor_rows(), table, expected_temperature_k=300.0)
    assert binding["state_ids"] == ["OX", "MR"]
    assert len(binding["binding_fingerprint"]) == 64


def test_tensor_rows_reject_source_temperature_or_wavelength_drift():
    table = normalize_optimization_condition_table(_condition_table())
    value = _tensor_rows()
    value["states"][0]["source_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="source identity"):
        bind_robust_material_tensor_rows(value, table, expected_temperature_k=300.0)
    value = _tensor_rows()
    with pytest.raises(ValueError, match="temperature"):
        bind_robust_material_tensor_rows(value, table, expected_temperature_k=301.0)
    value = _tensor_rows()
    value["states"][1]["rows"].pop()
    with pytest.raises(ValueError, match="active wavelengths"):
        bind_robust_material_tensor_rows(value, table, expected_temperature_k=300.0)
