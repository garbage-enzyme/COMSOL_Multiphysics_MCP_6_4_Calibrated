"""Solver-free thermal material ledger validation and evaluation tests."""

from __future__ import annotations

import asyncio
import math
from copy import deepcopy

import pytest
from pydantic import ValidationError
from src.server import create_server

from comsol_mcp.contracts.thermal_material import (
    ExtrapolationPolicy,
    NkTableModel,
    PermittivityTableModel,
    ThermalMaterialLedger,
    UncertaintyModel,
)
from comsol_mcp.evidence.thermal_material import (
    evaluate_thermal_material,
    normalize_thermal_material_ledger,
)
from development_kit.tests.mcp_test_support import decode_tool_result

_C = 299_792_458.0


def test_table_contract_declares_temperature_major_flattening():
    for model, fields in (
        (NkTableModel, ("n_flat", "k_flat")),
        (PermittivityTableModel, ("epsilon_real_flat", "epsilon_imag_flat")),
    ):
        properties = model.model_json_schema()["properties"]
        for field in fields:
            assert "Temperature-major" in properties[field]["description"]


def _source():
    return {
        "source_kind": "citation",
        "citation": "Synthetic analytic fixture",
        "source_state_description": "annealed reference sample",
    }


def _validity(t_min=300.0, t_max=600.0):
    return {
        "wavelength_min_m": 1.0e-6,
        "wavelength_max_m": 10.0e-6,
        "temperature_min_K": t_min,
        "temperature_max_K": t_max,
    }


def _state(state_id="state_a", model=None, *, t_min=300.0, t_max=600.0):
    return {
        "state_id": state_id,
        "phase_id": "alpha",
        "fabrication_state": "annealed",
        "classification": "fitted",
        "source": _source(),
        "validity": _validity(t_min, t_max),
        "uncertainty": {"kind": "relative", "relative_fraction": 0.01},
        "measurement_conditions": {"method": "ellipsometry", "ambient": "vacuum"},
        "carrier_state": {
            "density_per_cubic_metre": 1.0e24,
            "mobility_square_metre_per_V_s": 0.02,
            "effective_mass_electron": 0.3,
        },
        "phase_fraction": 1.0,
        "optical_model": model
        or {
            "model_kind": "drude",
            "epsilon_infinity": 4.0,
            "plasma_angular_frequency_rad_s": 2.0e15,
            "damping_angular_frequency_rad_s": 1.0e14,
        },
    }


def _ledger(states=None, boundaries=None):
    return {
        "material_identity_sha256": "a" * 64,
        "sample_identity_sha256": "b" * 64,
        "states": states or [_state()],
        "phase_boundaries": boundaries or [],
        "comsol_target": {
            "component_tag": "comp1",
            "material_tag": "mat1",
            "property_group_tag": "def",
            "relative_permittivity_property_key": "relpermittivity",
            "function_tag_prefix": "tm",
        },
    }


def _request(ledger, state_id="state_a", wavelength=2.0e-6, temperature=400.0):
    return {
        "ledger": ledger,
        "state_id": state_id,
        "wavelength_m": wavelength,
        "temperature_K": temperature,
    }


@pytest.mark.parametrize(
    "value",
    [
        {"kind": "nk_absolute"},
        {"kind": "epsilon_absolute"},
        {"kind": "relative"},
        {"kind": "relative", "relative_fraction": 0.1, "n_abs": 0.1},
    ],
)
def test_uncertainty_kind_requires_only_its_meaningful_fields(value):
    with pytest.raises(ValidationError):
        UncertaintyModel.model_validate(value)


@pytest.mark.parametrize(
    "value",
    [
        {"mode": "none", "policy_source_sha256": "a" * 64},
        {"mode": "none", "uncertainty_growth_per_fraction": 1.0},
        {"mode": "source_backed_linear", "policy_source_sha256": "a" * 64},
    ],
)
def test_extrapolation_policy_rejects_contradictory_fields(value):
    with pytest.raises(ValidationError):
        ExtrapolationPolicy.model_validate(value)


def _decode(result):
    return decode_tool_result(result)


def test_ledger_normalization_is_idempotent_and_previews_exact_readback():
    first = normalize_thermal_material_ledger(_ledger())
    second = normalize_thermal_material_ledger(first)
    assert second == first
    preview = first["comsol_conversion_preview"][0]
    assert preview["argument_units"] == ["m", "K"]
    assert preview["target_property"]["property_key"] == "relpermittivity"
    assert preview["post_apply_readback"]["exact_match_required"] is True
    assert preview["rollback"]["remove_created_functions_on_failure"] is True
    assert first["application_contract"]["mutation_performed"] is False

    state_a = _state("state_a")
    state_b = _state("state_b")
    assert normalize_thermal_material_ledger(_ledger([state_a, state_b])) == (
        normalize_thermal_material_ledger(_ledger([state_b, state_a]))
    )


def test_drude_fixture_matches_independent_formula():
    wavelength = 2.0e-6
    evaluation = evaluate_thermal_material(_request(_ledger(), wavelength=wavelength))
    omega = 2.0 * math.pi * _C / wavelength
    expected = 4.0 - (2.0e15) ** 2 / (omega * complex(omega, 1.0e14))
    assert evaluation["epsilon_internal"]["real"] == pytest.approx(expected.real)
    assert evaluation["epsilon_internal"]["imag"] == pytest.approx(expected.imag)
    assert evaluation["passive"] is True
    assert "-i*" in evaluation["comsol_conversion_preview"]["property_expression"]


def test_thermo_optic_fixture_uses_kelvin_offset_explicitly():
    model = {
        "model_kind": "thermo_optic",
        "reference_temperature_K": 300.0,
        "refractive_index_at_reference": 2.0,
        "extinction_coefficient_at_reference": 0.1,
        "dn_dT_per_K": 1.0e-4,
        "dk_dT_per_K": 2.0e-5,
    }
    evaluation = evaluate_thermal_material(
        _request(_ledger([_state(model=model)]), temperature=400.0)
    )
    assert evaluation["refractive_index_internal"] == pytest.approx({"n": 2.01, "k": 0.102})
    bad = deepcopy(model)
    bad["reference_temperature_C"] = bad.pop("reference_temperature_K")
    with pytest.raises(ValidationError):
        normalize_thermal_material_ledger(_ledger([_state(model=bad)]))


def test_nk_and_permittivity_tables_round_trip_with_passive_convention():
    nk_model = {
        "model_kind": "nk_table",
        "wavelengths_m": [1.0e-6, 2.0e-6],
        "temperatures_K": [300.0],
        "n_flat": [2.0, 3.0],
        "k_flat": [0.1, 0.2],
        "interpolation": {
            "wavelength_method": "linear",
            "temperature_method": "linear",
            "wavelength_discontinuities_m": [],
            "temperature_discontinuities_K": [],
        },
        "table_sha256": "c" * 64,
    }
    nk_evaluation = evaluate_thermal_material(
        _request(_ledger([_state(model=nk_model)]), wavelength=1.5e-6, temperature=300.0)
    )
    assert nk_evaluation["refractive_index_internal"] == pytest.approx({"n": 2.5, "k": 0.15})
    epsilon = complex(2.5, 0.15) ** 2
    eps_model = {
        "model_kind": "permittivity_table",
        "wavelengths_m": [1.0e-6, 2.0e-6],
        "temperatures_K": [300.0],
        "epsilon_real_flat": [epsilon.real, epsilon.real],
        "epsilon_imag_flat": [epsilon.imag, epsilon.imag],
        "interpolation": {
            "wavelength_method": "linear",
            "temperature_method": "linear",
            "wavelength_discontinuities_m": [],
            "temperature_discontinuities_K": [],
        },
        "table_sha256": "d" * 64,
    }
    eps_evaluation = evaluate_thermal_material(
        _request(_ledger([_state(model=eps_model)]), wavelength=1.5e-6, temperature=300.0)
    )
    assert eps_evaluation["refractive_index_internal"] == pytest.approx(
        nk_evaluation["refractive_index_internal"]
    )
    assert eps_evaluation["epsilon_internal"]["imag"] >= 0.0


def test_carrier_states_remain_distinct():
    first = _state("state_a")
    second = _state("state_b")
    second["carrier_state"] = {
        "density_per_cubic_metre": 2.0e24,
        "mobility_square_metre_per_V_s": 0.01,
        "effective_mass_electron": 0.5,
    }
    second["state_variables"] = [
        {"name": "oxygen_fraction", "value": 0.2, "unit": "1", "source_sha256": "6" * 64}
    ]
    ledger = _ledger([first, second])
    one = evaluate_thermal_material(_request(ledger, "state_a"))
    two = evaluate_thermal_material(_request(ledger, "state_b"))
    assert one["state_identity_sha256"] != two["state_identity_sha256"]
    assert one["carrier_state"] != two["carrier_state"]
    assert two["state_identity_sha256"] != one["state_identity_sha256"]
    assert two["state_variables"][0]["name"] == "oxygen_fraction"


def test_phase_boundary_and_table_discontinuity_cannot_be_smoothed_through():
    lower = _state("solid", t_min=300.0, t_max=500.0)
    upper = _state("liquid", t_min=500.0, t_max=700.0)
    upper["phase_id"] = "liquid"
    boundary = {
        "lower_state_id": "solid",
        "upper_state_id": "liquid",
        "boundary_temperature_K": 500.0,
        "smoothing_allowed": False,
        "source_sha256": "e" * 64,
    }
    normalized = normalize_thermal_material_ledger(_ledger([lower, upper], [boundary]))
    assert normalized["phase_boundaries"][0]["smoothing_allowed"] is False

    table = {
        "model_kind": "nk_table",
        "wavelengths_m": [1.0e-6, 2.0e-6],
        "temperatures_K": [400.0, 600.0],
        "n_flat": [2.0, 2.0, 3.0, 3.0],
        "k_flat": [0.1, 0.1, 0.2, 0.2],
        "interpolation": {
            "wavelength_method": "linear",
            "temperature_method": "linear",
            "wavelength_discontinuities_m": [],
            "temperature_discontinuities_K": [500.0],
        },
        "table_sha256": "f" * 64,
    }
    result = evaluate_thermal_material(
        _request(_ledger([_state(model=table)]), wavelength=1.5e-6, temperature=500.0)
    )
    assert result["available"] is False
    assert result["reason_code"] == "declared_discontinuity_requires_explicit_state"


def test_out_of_domain_is_stably_unavailable_and_source_backed_extrapolation_expands_uncertainty():
    unavailable = evaluate_thermal_material(_request(_ledger(), wavelength=20.0e-6))
    assert unavailable["available"] is False
    assert unavailable["reason_code"] == "outside_declared_validity_domain"

    model = deepcopy(_state()["optical_model"])
    model["extrapolation"] = {
        "mode": "source_backed_linear",
        "policy_source_sha256": "7" * 64,
        "maximum_fraction_outside_domain": 0.2,
        "uncertainty_growth_per_fraction": 2.0,
    }
    extrapolated = evaluate_thermal_material(
        _request(_ledger([_state(model=model)]), wavelength=11.0e-6)
    )
    assert extrapolated["available"] is True
    assert extrapolated["extrapolated"] is True
    assert extrapolated["uncertainty"]["expansion_factor"] > 1.0
    assert len(extrapolated["extrapolation_policy_sha256"]) == 64


def test_source_backed_extrapolation_cannot_cross_far_side_discontinuity():
    model = {
        "model_kind": "nk_table",
        "wavelengths_m": [1.0e-6, 2.0e-6],
        "temperatures_K": [400.0],
        "n_flat": [2.0, 3.0],
        "k_flat": [0.1, 0.2],
        "interpolation": {
            "wavelength_method": "linear",
            "temperature_method": "linear",
            "wavelength_discontinuities_m": [2.1e-6],
            "temperature_discontinuities_K": [],
            "extrapolation": {
                "mode": "source_backed_linear",
                "policy_source_sha256": "7" * 64,
                "maximum_fraction_outside_domain": 0.2,
                "uncertainty_growth_per_fraction": 2.0,
            },
        },
        "table_sha256": "f" * 64,
    }
    result = evaluate_thermal_material(_request(_ledger([_state(model=model)]), wavelength=2.2e-6))
    assert result["available"] is False
    assert result["reason_code"] == "declared_discontinuity_requires_explicit_state"


def test_invalid_table_shape_and_negative_passive_loss_fail_closed():
    bad_shape = {
        "model_kind": "nk_table",
        "wavelengths_m": [1.0e-6, 2.0e-6],
        "temperatures_K": [300.0],
        "n_flat": [2.0],
        "k_flat": [0.1],
        "interpolation": {
            "wavelength_method": "linear",
            "temperature_method": "linear",
            "wavelength_discontinuities_m": [],
            "temperature_discontinuities_K": [],
        },
        "table_sha256": "8" * 64,
    }
    with pytest.raises((ValidationError, ValueError)):
        normalize_thermal_material_ledger(_ledger([_state(model=bad_shape)]))
    negative_loss = {
        "model_kind": "permittivity_table",
        "wavelengths_m": [1.0e-6, 2.0e-6],
        "temperatures_K": [300.0],
        "epsilon_real_flat": [2.0, 2.0],
        "epsilon_imag_flat": [-0.1, -0.1],
        "interpolation": {
            "wavelength_method": "linear",
            "temperature_method": "linear",
            "wavelength_discontinuities_m": [],
            "temperature_discontinuities_K": [],
        },
        "table_sha256": "9" * 64,
    }
    with pytest.raises(ValidationError):
        normalize_thermal_material_ledger(_ledger([_state(model=negative_loss)]))


@pytest.mark.parametrize("model_kind", ["nk_table", "permittivity_table"])
def test_table_shape_fails_at_the_typed_boundary(model_kind):
    model = {
        "model_kind": model_kind,
        "wavelengths_m": [1.0e-6, 2.0e-6],
        "temperatures_K": [300.0, 400.0],
        "interpolation": {
            "wavelength_method": "linear",
            "temperature_method": "linear",
            "wavelength_discontinuities_m": [],
            "temperature_discontinuities_K": [],
        },
        "table_sha256": "8" * 64,
    }
    if model_kind == "nk_table":
        model.update(n_flat=[2.0, 2.0], k_flat=[0.1, 0.1])
    else:
        model.update(epsilon_real_flat=[2.0, 2.0], epsilon_imag_flat=[0.1, 0.1])
    with pytest.raises(ValidationError, match="declared grid shape"):
        ThermalMaterialLedger.model_validate(_ledger([_state(model=model)]))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda ledger: ledger["states"].append(deepcopy(ledger["states"][0])), "unique"),
        (
            lambda ledger: ledger["phase_boundaries"][0].update(upper_state_id="missing"),
            "declared states",
        ),
        (
            lambda ledger: ledger["phase_boundaries"][0].update(upper_state_id="solid"),
            "distinct",
        ),
        (
            lambda ledger: ledger["phase_boundaries"][0].update(boundary_temperature_K=650.0),
            "covered",
        ),
    ],
)
def test_phase_boundary_references_fail_at_the_typed_boundary(mutate, message):
    lower = _state("solid", t_min=300.0, t_max=500.0)
    upper = _state("liquid", t_min=500.0, t_max=700.0)
    upper["phase_id"] = "liquid"
    boundary = {
        "lower_state_id": "solid",
        "upper_state_id": "liquid",
        "boundary_temperature_K": 500.0,
        "smoothing_allowed": False,
        "source_sha256": "e" * 64,
    }
    ledger = _ledger([lower, upper], [boundary])
    mutate(ledger)
    with pytest.raises(ValidationError, match=message):
        ThermalMaterialLedger.model_validate(ledger)


def test_public_m2_dispatch_is_solver_free():
    server = create_server("m2-public", profile="basic_fem")
    validated = _decode(
        asyncio.run(server.call_tool("thermal_material_validate", {"ledger": _ledger()}))
    )
    evaluated = _decode(
        asyncio.run(
            server.call_tool(
                "thermal_material_evaluate",
                {"request": _request(_ledger())},
            )
        )
    )
    assert validated["success"] is True
    assert validated["solver_started"] is False
    assert evaluated["success"] is True
    assert evaluated["evaluation"]["available"] is True
    assert evaluated["solver_started"] is False
