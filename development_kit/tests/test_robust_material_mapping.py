"""Optical-property tensor mapping tests."""

from __future__ import annotations

import copy

import pytest

from comsol_mcp.research.robust_material_mapping import normalize_optical_property_mapping


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
