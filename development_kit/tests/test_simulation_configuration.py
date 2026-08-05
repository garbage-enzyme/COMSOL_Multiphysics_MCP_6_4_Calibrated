"""Solver-free simulation configuration and job-preview contracts."""

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy

import pytest
from pydantic import ValidationError
from src.server import create_server

from comsol_mcp.evidence.simulation_configuration import (
    compare_simulation_configurations,
    normalize_simulation_configuration,
)
from comsol_mcp.tools.jobs import _preview_job_spec, _submit_job
from development_kit.tests.mcp_test_support import decode_tool_result


def _quantity(value: float, unit: str, dimension: str = "length") -> dict[str, object]:
    return {"status": "known", "dimension": dimension, "value": value, "unit": unit}


def _configuration() -> dict[str, object]:
    return {
        "method": "fem",
        "source": {
            "relative_identity": "fixtures/unit_cell.mph",
            "content_sha256": "a" * 64,
            "format": "mph",
        },
        "producer": {"tool": "fixture", "version": "1.0", "contract_sha256": "b" * 64},
        "geometry": [
            {
                "dimension_id": "period_x",
                "semantic": "period_x",
                "quantity": _quantity(500.0, "nm"),
                "label": "Px",
            },
            {
                "dimension_id": "width",
                "semantic": "full_width",
                "quantity": _quantity(200.0, "nm"),
                "label": "Width",
            },
        ],
        "materials": [
            {
                "material_id": "gold",
                "region_id": "resonator",
                "model_identity_sha256": "c" * 64,
                "temperature": _quantity(300.0, "K", "temperature"),
                "loss_sign_convention": "positive_imaginary_loss",
                "label": "Au",
            }
        ],
        "layers": [
            {
                "layer_id": "resonator",
                "material_id": "gold",
                "order": 0,
                "thickness": _quantity(50.0, "nm"),
            }
        ],
        "incidence": {
            "theta": _quantity(0.0, "deg", "angle"),
            "phi": _quantity(0.0, "deg", "angle"),
            "propagation_direction": "negative_z",
            "polarization_basis": "sp",
            "polarization_state": "s",
            "handedness_convention": "not_applicable",
        },
        "wavelength_control": {
            "driver": "parameter",
            "parameter_name": "lambda0",
            "requested": _quantity(1550.0, "nm"),
            "evaluated": _quantity(1550.0, "nm"),
        },
        "mesh": {"dependency_keys": ["width", "period_x"], "characteristic_lengths": []},
        "model_tree": {
            "physics": ["ewfd"],
            "studies": ["std1"],
            "solvers": ["sol1"],
            "datasets": ["dset1"],
            "selections": ["resonator"],
        },
        "solver": {
            "formulation": "frequency_domain",
            "termination_condition": "relative_tolerance",
            "boundary_termination": "port",
        },
        "unit_contracts": [
            {"quantity": "wavelength", "requested_unit": "nm", "evaluated_unit": "m"}
        ],
        "artifact_chains": [{"chain_sha256": "d" * 64, "role": "geometry_source"}],
    }


def _decode_public(result):
    return decode_tool_result(result)


def test_unit_normalization_is_idempotent_and_semantically_equal():
    left = _configuration()
    right = deepcopy(left)
    right["geometry"][0]["quantity"] = _quantity(0.5, "um")
    right["materials"][0]["temperature"] = _quantity(26.85, "degC", "temperature")

    normalized = normalize_simulation_configuration(left)
    assert normalize_simulation_configuration(normalized) == normalized
    comparison = compare_simulation_configurations(
        left, right, {"tolerances": {"temperature_K": 1e-12}}
    )
    assert comparison["disposition"] == "equivalent"
    assert comparison["classification_counts"]["semantic"] == 0


def test_celsius_symbol_alias_normalizes_to_kelvin():
    value = _configuration()
    value["materials"][0]["temperature"] = _quantity(26.85, "°C", "temperature")

    normalized = normalize_simulation_configuration(value)

    assert normalized["materials"][0]["temperature"] == {
        "status": "known",
        "dimension": "temperature",
        "value": 300.0,
        "unit": "K",
    }


@pytest.mark.parametrize(
    ("mutation", "path_fragment"),
    [
        (lambda value: value["geometry"][1].update(semantic="half_width"), "semantic"),
        (
            lambda value: value["materials"][0].update(
                loss_sign_convention="negative_imaginary_loss"
            ),
            "loss_sign_convention",
        ),
        (lambda value: value["incidence"].update(polarization_state="p"), "polarization_state"),
        (lambda value: value["solver"].update(boundary_termination="pml"), "boundary_termination"),
    ],
)
def test_physical_configuration_changes_are_semantic(mutation, path_fragment):
    right = deepcopy(_configuration())
    mutation(right)
    comparison = compare_simulation_configurations(_configuration(), right)
    assert comparison["disposition"] == "different"
    assert any(path_fragment in item["path"] for item in comparison["changes"])


def test_labels_are_not_promoted_to_physical_identity():
    right = deepcopy(_configuration())
    right["materials"][0]["label"] = "metal"
    comparison = compare_simulation_configurations(_configuration(), right)
    assert comparison["disposition"] == "equivalent"
    assert comparison["classification_counts"]["label_only"] == 1
    assert comparison["physical_identity_inferred_from_labels"] is False


def test_cross_method_bridge_separates_full_and_physical_dispositions():
    right = deepcopy(_configuration())
    right["method"] = "rcwa"
    right["source"]["relative_identity"] = "fixtures/unit_cell.json"
    right["source"]["content_sha256"] = "f" * 64
    right["source"]["format"] = "json"
    comparison = compare_simulation_configurations(_configuration(), right)
    assert comparison["disposition"] == "different"
    assert comparison["physical_disposition"] == "equivalent"


def test_layer_order_and_wavelength_control_changes_are_physical():
    left = _configuration()
    left["materials"].append(
        {
            "material_id": "glass",
            "region_id": "substrate",
            "model_identity_sha256": "9" * 64,
            "temperature": _quantity(300.0, "K", "temperature"),
            "loss_sign_convention": "positive_imaginary_loss",
        }
    )
    left["layers"].append(
        {
            "layer_id": "substrate",
            "material_id": "glass",
            "order": 1,
            "thickness": _quantity(500.0, "nm"),
        }
    )
    right = deepcopy(left)
    right["layers"][0]["order"], right["layers"][1]["order"] = 1, 0
    right["wavelength_control"]["evaluated"] = _quantity(1540.0, "nm")
    comparison = compare_simulation_configurations(left, right)
    assert comparison["physical_disposition"] == "different"
    assert any("layers" in item["path"] for item in comparison["changes"])
    assert any("wavelength_control" in item["path"] for item in comparison["changes"])


def test_unknown_quantity_remains_explicit():
    value = _configuration()
    value["geometry"][1]["quantity"] = {"status": "unknown", "dimension": "length"}
    normalized = normalize_simulation_configuration(value)
    assert normalized["geometry"][1]["quantity"] == {"status": "unknown", "dimension": "length"}


def test_duplicate_layer_order_and_unrecognized_units_fail_closed():
    duplicate = _configuration()
    duplicate["layers"].append(
        {"layer_id": "second", "material_id": "gold", "order": 0, "thickness": _quantity(1.0, "nm")}
    )
    with pytest.raises(ValueError, match="layer order"):
        normalize_simulation_configuration(duplicate)
    bad_unit = _configuration()
    bad_unit["geometry"][0]["quantity"] = _quantity(1.0, "furlong")
    with pytest.raises(ValueError, match="unsupported length unit"):
        normalize_simulation_configuration(bad_unit)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(geometry=[]),
        lambda value: value.update(materials=[], layers=[]),
        lambda value: value.update(layers=[]),
    ],
)
def test_container_level_physical_changes_are_classified(mutation):
    right = deepcopy(_configuration())
    mutation(right)
    comparison = compare_simulation_configurations(_configuration(), right)
    assert comparison["physical_disposition"] == "different"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["incidence"]["theta"].update(dimension="length", unit="m"),
        lambda value: value["incidence"]["phi"].update(dimension="temperature", unit="K"),
        lambda value: value["materials"][0]["temperature"].update(dimension="length", unit="m"),
        lambda value: value["layers"][0]["thickness"].update(dimension="angle", unit="rad"),
        lambda value: value["layers"][0]["thickness"].update(value=-1.0),
    ],
)
def test_physical_role_quantities_fail_at_the_typed_boundary(mutation):
    value = _configuration()
    mutation(value)
    with pytest.raises(ValidationError):
        normalize_simulation_configuration(value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_configuration_rejects_nonfinite_declared_quantities(value):
    configuration = _configuration()
    configuration["layers"][0]["thickness"]["value"] = value

    with pytest.raises(ValidationError):
        normalize_simulation_configuration(configuration)


def test_configuration_rejects_geometry_dimension_and_missing_material_reference():
    wrong_dimension = _configuration()
    wrong_dimension["geometry"][0]["quantity"] = _quantity(1.0, "eV", "energy")
    with pytest.raises(ValidationError, match="length quantities"):
        normalize_simulation_configuration(wrong_dimension)

    missing_material = _configuration()
    missing_material["layers"][0]["material_id"] = "missing"
    with pytest.raises(ValidationError, match="declared material_id"):
        normalize_simulation_configuration(missing_material)


def test_non_parameter_wavelength_driver_rejects_residual_parameter_name():
    configuration = _configuration()
    configuration["wavelength_control"]["driver"] = "frequency"

    with pytest.raises(ValidationError, match="cannot declare parameter_name"):
        normalize_simulation_configuration(configuration)


@pytest.mark.parametrize(
    "spec",
    [
        {
            "job_type": "staged_sweep",
            "source_model_path": "D:/fixtures/model.mph",
            "parameter_name": "p",
            "parameter_values": [1.0, 2.0],
            "expressions": ["ewfd.Rorder_0"],
        },
        {
            "job_type": "validation_matrix",
            "source_model_path": "D:/fixtures/model.mph",
            "points": [{"point_id": "p1"}],
            "point_limit": 1,
            "resource_policy": {},
            "cores": 1,
        },
        {
            "job_type": "spectral_characterization",
            "source_model_path": "D:/fixtures/model.mph",
            "source_model_relative_identity": "fixtures/model.mph",
            "configuration_sha256": "e" * 64,
            "parameter_state": {},
            "wavelength_parameter": "lambda0",
            "initial_grid": {},
            "refinement_policy": {},
            "expansion_policy": {},
            "maximum_points": 9,
            "collector": {},
            "analysis_policy": {},
            "measurement_configuration": {},
            "resource_policy": {},
            "cores": 1,
        },
        {
            "job_type": "convergence_campaign",
            "campaign_id": "c",
            "levels": [{"level": 1}],
            "convergence_policy": {},
            "stop_policy": {},
            "maximum_total_points": 5,
            "wall_time_budget_seconds": 60,
        },
        {
            "job_type": "branch_continuation_campaign",
            "campaign_id": "b",
            "states": [{"state": 1}],
            "continuation_policy": {},
            "maximum_total_points": 5,
            "wall_time_budget_seconds": 60,
        },
    ],
)
def test_job_preview_is_side_effect_free_and_content_bound(spec):
    original = deepcopy(spec)
    first = _preview_job_spec(spec)
    second = _preview_job_spec(deepcopy(spec))
    assert spec == original
    assert first == second
    assert first["preview_guarantees"] == {
        "submitted": False,
        "admission_checked": False,
        "solver_ownership_checked": False,
        "solve_success_implied": False,
        "solver_started": False,
        "filesystem_modified": False,
    }


def test_job_preview_and_submit_share_discriminated_input_rejections():
    oversized = {
        "job_type": "convergence_campaign",
        "campaign_id": "too-large",
        "levels": [{"level": index} for index in range(2049)],
        "convergence_policy": {},
        "stop_policy": {},
        "maximum_total_points": 2048,
        "wall_time_budget_seconds": 60,
    }
    submitted = []

    class Manager:
        def submit(self, spec):
            submitted.append(spec)
            raise AssertionError("invalid job spec reached submission")

    for invalid in ({"job_type": "unknown"}, oversized):
        with pytest.raises(ValueError):
            _preview_job_spec(deepcopy(invalid))
        with pytest.raises(ValueError):
            _submit_job(
                deepcopy(invalid),
                profile_name="core",
                shared_enabled=False,
                manager=Manager(),
            )
    assert submitted == []


def test_public_f0_dispatch_is_solver_free():
    server = create_server("f0-public", profile="core")
    validate_result = _decode_public(
        asyncio.run(
            server.call_tool(
                "simulation_configuration_validate", {"configuration": _configuration()}
            )
        )
    )
    preview_result = _decode_public(
        asyncio.run(
            server.call_tool(
                "job_spec_preview",
                {
                    "spec": {
                        "job_type": "convergence_campaign",
                        "campaign_id": "c",
                        "levels": [{"level": 1}],
                        "convergence_policy": {},
                        "stop_policy": {},
                        "maximum_total_points": 5,
                        "wall_time_budget_seconds": 60,
                    }
                },
            )
        )
    )
    assert validate_result["success"] is True
    assert validate_result["solver_started"] is False
    assert preview_result["success"] is True
    assert preview_result["preview_guarantees"]["submitted"] is False


def test_public_configuration_failures_are_logged_and_redacted(caplog):
    bad = _configuration()
    bad["geometry"][0]["quantity"] = _quantity(1.0, "private-furlong")
    server = create_server("f0-redaction", profile="core")

    with caplog.at_level(logging.ERROR, logger="comsol_mcp.tools.configuration"):
        result = _decode_public(
            asyncio.run(
                server.call_tool("simulation_configuration_validate", {"configuration": bad})
            )
        )

    assert result["success"] is False
    assert result["error"] == "Simulation configuration validation failed."
    assert "private-furlong" not in result["error"]
    assert "Simulation configuration validation failed" in caplog.text
    assert "private-furlong" in caplog.text


@pytest.mark.parametrize("failure", [OverflowError("huge"), RecursionError("deep")])
def test_public_configuration_contains_numeric_and_depth_failures(monkeypatch, failure):
    import src.evidence.simulation_configuration as evidence_module

    monkeypatch.setattr(
        evidence_module,
        "normalize_simulation_configuration",
        lambda _configuration: (_ for _ in ()).throw(failure),
    )
    server = create_server("f0-contained-input-failure", profile="core")
    result = _decode_public(
        asyncio.run(
            server.call_tool(
                "simulation_configuration_validate", {"configuration": _configuration()}
            )
        )
    )

    assert result["success"] is False
    assert result["error"] == "Simulation configuration validation failed."
