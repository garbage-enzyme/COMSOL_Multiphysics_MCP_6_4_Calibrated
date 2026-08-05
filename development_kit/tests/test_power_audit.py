"""reference-power solver-free gates for declared physical-power evidence."""

from __future__ import annotations

from copy import deepcopy

import pytest
from src.evidence.power_audit import (
    normalize_declared_plane_flux,
    normalize_internal_absorption_consistency,
)


def _plane(*, raw_power_w, sign, coordinate, normal, selection_id):
    return {
        "expression": f"intop{selection_id}(real(ewfd.Poavz))",
        "selection_ids": [selection_id],
        "plane_coordinate_m": coordinate,
        "normal": normal,
        "medium_id": "lossless_air",
        "raw_power_w": raw_power_w,
        "positive_power_sign": sign,
    }


def _flux_spec():
    return {
        "incident": _plane(
            raw_power_w=-2.0,
            sign=-1,
            coordinate=1.0e-6,
            normal=[0.0, 0.0, -1.0],
            selection_id=1,
        ),
        "reflected": _plane(
            raw_power_w=0.4,
            sign=1,
            coordinate=1.0e-6,
            normal=[0.0, 0.0, 1.0],
            selection_id=2,
        ),
        "transmitted": _plane(
            raw_power_w=-1.2,
            sign=-1,
            coordinate=-1.0e-6,
            normal=[0.0, 0.0, -1.0],
            selection_id=3,
        ),
    }


def test_declared_signs_produce_bounded_flux_rta_without_volume_loss():
    result = normalize_declared_plane_flux(_flux_spec())

    assert result["state"] == "derived_from_declared_convention"
    assert result["R"] == pytest.approx(0.2)
    assert result["T"] == pytest.approx(0.6)
    assert result["A"] == pytest.approx(0.2)
    assert result["closure_abs"] == pytest.approx(0.0)
    assert result["net_absorbed_power_w"] == pytest.approx(0.4)
    assert result["planes"]["incident"]["directed_power_w"] == pytest.approx(2.0)


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda value: value["incident"].update({"positive_power_sign": 1}), "strictly positive"),
        (lambda value: value["incident"].update({"normal": [0.0, 0.0, 2.0]}), "unit vector"),
        (lambda value: value["incident"].update({"medium_id": ""}), "non-empty string"),
        (lambda value: value["incident"].update({"selection_ids": [1, 1]}), "duplicate"),
        (lambda value: value["incident"].update({"guessed_sign": -1}), "unknown fields"),
    ],
)
def test_declared_plane_flux_fails_closed_on_ambiguous_inputs(mutation, match):
    value = _flux_spec()
    mutation(value)

    with pytest.raises(ValueError, match=match):
        normalize_declared_plane_flux(value)


@pytest.mark.parametrize("sign", [1.0, [1], {"sign": 1}])
def test_declared_plane_flux_requires_strict_integer_signs(sign):
    value = _flux_spec()
    value["reflected"]["positive_power_sign"] = sign

    with pytest.raises(ValueError, match="exactly -1 or 1"):
        normalize_declared_plane_flux(value)


def test_power_audit_entry_points_reject_pair_iterables_instead_of_coercing_them():
    with pytest.raises(ValueError, match="JSON object"):
        normalize_declared_plane_flux(list(_flux_spec().items()))
    with pytest.raises(ValueError, match="JSON object"):
        normalize_internal_absorption_consistency(list(_cross_section().items()), None)


def test_declared_plane_flux_rejects_nonfinite_derived_results_and_huge_integers():
    overflow = _flux_spec()
    overflow["incident"]["raw_power_w"] = -1.0e-308
    overflow["reflected"]["raw_power_w"] = 1.0e308
    huge = _flux_spec()
    huge["incident"]["raw_power_w"] = 10**10_000

    with pytest.raises(ValueError, match="finite derived values"):
        normalize_declared_plane_flux(overflow)
    with pytest.raises(ValueError, match="finite number"):
        normalize_declared_plane_flux(huge)


@pytest.mark.parametrize(
    "plane,key,value",
    [
        ("incident", "raw_power_w", float("nan")),
        ("reflected", "raw_power_w", float("inf")),
        ("transmitted", "plane_coordinate_m", -float("inf")),
        ("incident", "normal", [0.0, float("nan"), 1.0]),
        ("reflected", "normal", [float("inf"), 0.0, 0.0]),
    ],
)
def test_declared_plane_flux_rejects_nonfinite_solver_outputs(plane, key, value):
    spec = _flux_spec()
    spec[plane][key] = value

    with pytest.raises(ValueError, match="finite number"):
        normalize_declared_plane_flux(spec)


def _cross_section():
    return {
        "expression": "ewfd.sigmaAbs",
        "value_m2": 2.0e-13,
        "unit": "m^2",
        "unit_cell_area_expression": "px*py",
        "unit_cell_area_m2": 1.0e-12,
        "source_feature": "ewfd/csc1",
    }


def _volume_loss():
    return {
        "expression": "intLoss(ewfd.Qh)",
        "selection_ids": [4, 5],
        "value_w": 0.4,
        "incident_power_w": 2.0,
        "unit": "W",
    }


def test_cross_section_volume_agreement_remains_internal_consistency_only():
    result = normalize_internal_absorption_consistency(_cross_section(), _volume_loss())

    assert result["state"] == "measured"
    assert result["classification"] == "internal_normalization_consistency"
    assert result["cross_section"]["normalized_absorption"] == pytest.approx(0.2)
    assert result["volume_loss"]["normalized_absorption"] == pytest.approx(0.2)
    assert result["relative_residual"] == pytest.approx(0.0)
    assert result["physical_flux_closure_eligible"] is False


def test_missing_cross_section_is_not_requested_and_missing_volume_is_unknown():
    assert normalize_internal_absorption_consistency(None, _volume_loss()) == {
        "schema_version": "1.0.0",
        "state": "not_requested",
        "physical_flux_closure_eligible": False,
    }

    result = normalize_internal_absorption_consistency(_cross_section(), None)
    assert result["state"] == "unknown"
    assert result["physical_flux_closure_eligible"] is False


@pytest.mark.parametrize(
    "target,key,value,match",
    [
        ("cross", "unit", "W", "exactly 'm\\^2'"),
        ("cross", "unit_cell_area_m2", 0.0, "strictly positive"),
        ("volume", "unit", "J", "exactly 'W'"),
        ("volume", "incident_power_w", 0.0, "strictly positive"),
    ],
)
def test_internal_consistency_rejects_incompatible_or_unusable_normalizations(
    target, key, value, match
):
    cross = _cross_section()
    volume = _volume_loss()
    chosen = cross if target == "cross" else volume
    chosen[key] = value

    with pytest.raises(ValueError, match=match):
        normalize_internal_absorption_consistency(deepcopy(cross), deepcopy(volume))


@pytest.mark.parametrize(
    "target,key,value",
    [
        ("cross", "value_m2", float("nan")),
        ("cross", "unit_cell_area_m2", float("inf")),
        ("volume", "value_w", -float("inf")),
        ("volume", "incident_power_w", float("nan")),
    ],
)
def test_internal_consistency_rejects_nonfinite_solver_outputs(target, key, value):
    cross = _cross_section()
    volume = _volume_loss()
    chosen = cross if target == "cross" else volume
    chosen[key] = value

    with pytest.raises(ValueError, match="finite number"):
        normalize_internal_absorption_consistency(cross, volume)


@pytest.mark.parametrize(
    "target,key,value,match",
    [
        ("cross", "value_m2", 1.0e308, "cross-section normalization"),
        ("volume", "value_w", 1.0e308, "volume-loss normalization"),
    ],
)
def test_internal_consistency_rejects_nonfinite_derived_normalizations(target, key, value, match):
    cross = _cross_section()
    volume = _volume_loss()
    chosen = cross if target == "cross" else volume
    chosen[key] = value
    if target == "cross":
        cross["unit_cell_area_m2"] = 1.0e-308
    else:
        volume["incident_power_w"] = 1.0e-308

    with pytest.raises(ValueError, match=match):
        normalize_internal_absorption_consistency(cross, volume)


def test_internal_consistency_rejects_nonfinite_derived_residuals():
    cross = _cross_section()
    cross["value_m2"] = 1.0e308
    cross["unit_cell_area_m2"] = 1.0
    volume = _volume_loss()
    volume["value_w"] = -1.0e308
    volume["incident_power_w"] = 1.0

    with pytest.raises(ValueError, match="strictly positive"):
        normalize_internal_absorption_consistency(cross, volume)
