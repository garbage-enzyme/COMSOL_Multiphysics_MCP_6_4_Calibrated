"""Solver-free normalization for reference-power physical-power evidence.

The helpers in this module only validate caller declarations and perform
arithmetic.  They do not infer plane orientation, medium identity, expressions,
or whether an internal absorption normalization is physical flux closure.
"""

from __future__ import annotations

from copy import deepcopy
import math
from typing import Any, Mapping


_PLANE_FIELDS = {
    "expression",
    "selection_ids",
    "plane_coordinate_m",
    "normal",
    "medium_id",
    "raw_power_w",
    "positive_power_sign",
}
_FLUX_FIELDS = {"incident", "reflected", "transmitted"}
_CROSS_SECTION_FIELDS = {
    "expression",
    "value_m2",
    "unit",
    "unit_cell_area_expression",
    "unit_cell_area_m2",
    "source_feature",
}
_VOLUME_LOSS_FIELDS = {
    "expression",
    "selection_ids",
    "value_w",
    "incident_power_w",
    "unit",
}
_MAX_TEXT = 4096
_MAX_SELECTION_IDS = 256


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{label} contains unknown fields: {unknown}")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    if len(value) > _MAX_TEXT:
        raise ValueError(f"{label} exceeds {_MAX_TEXT} characters")
    return value


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _positive(value: Any, label: str) -> float:
    result = _finite(value, label)
    if result <= 0.0:
        raise ValueError(f"{label} must be strictly positive")
    return result


def _selection_ids(value: Any, label: str) -> list[int]:
    if not isinstance(value, list) or not value or len(value) > _MAX_SELECTION_IDS:
        raise ValueError(f"{label} must contain 1..{_MAX_SELECTION_IDS} boundary IDs")
    result: list[int] = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise ValueError(f"{label}[{index}] must be a positive integer")
        result.append(item)
    if len(result) != len(set(result)):
        raise ValueError(f"{label} must not contain duplicate boundary IDs")
    return result


def _unit_normal(value: Any, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{label} must contain exactly three components")
    result = [_finite(item, f"{label}[{index}]") for index, item in enumerate(value)]
    norm = math.sqrt(sum(item * item for item in result))
    if not math.isclose(norm, 1.0, rel_tol=1e-9, abs_tol=1e-9):
        raise ValueError(f"{label} must be a unit vector")
    return result


def _plane(value: Any, label: str) -> dict[str, Any]:
    item = _mapping(value, label)
    _reject_unknown(item, _PLANE_FIELDS, label)
    sign = item.get("positive_power_sign")
    if isinstance(sign, bool) or sign not in {-1, 1}:
        raise ValueError(f"{label}.positive_power_sign must be exactly -1 or 1")
    raw_power = _finite(item.get("raw_power_w"), f"{label}.raw_power_w")
    normalized = {
        "expression": _text(item.get("expression"), f"{label}.expression"),
        "selection_ids": _selection_ids(item.get("selection_ids"), f"{label}.selection_ids"),
        "plane_coordinate_m": _finite(item.get("plane_coordinate_m"), f"{label}.plane_coordinate_m"),
        "normal": _unit_normal(item.get("normal"), f"{label}.normal"),
        "medium_id": _text(item.get("medium_id"), f"{label}.medium_id"),
        "raw_power_w": raw_power,
        "positive_power_sign": int(sign),
        "directed_power_w": raw_power * int(sign),
    }
    return normalized


def normalize_declared_plane_flux(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate three declared planes and derive signed, normalized R/T/A.

    ``positive_power_sign`` maps each raw expression result onto the caller's
    declared positive physical direction.  The function never guesses a sign or
    plane medium.  ``A`` is the normalized net power missing from the two
    declared outgoing channels; it is not derived from volume ``Qh``.
    """

    spec = _mapping(dict(value), "declared_plane_flux")
    _reject_unknown(spec, _FLUX_FIELDS, "declared_plane_flux")
    missing = sorted(_FLUX_FIELDS - set(spec))
    if missing:
        raise ValueError(f"declared_plane_flux is missing fields: {missing}")
    planes = {name: _plane(spec[name], f"declared_plane_flux.{name}") for name in sorted(_FLUX_FIELDS)}
    incident = planes["incident"]["directed_power_w"]
    if incident <= 0.0:
        raise ValueError("declared_plane_flux.incident directed power must be strictly positive")
    reflected = planes["reflected"]["directed_power_w"]
    transmitted = planes["transmitted"]["directed_power_w"]
    r_value = reflected / incident
    t_value = transmitted / incident
    a_value = (incident - reflected - transmitted) / incident
    closure = abs(1.0 - r_value - t_value - a_value)
    return {
        "schema_version": "1.0.0",
        "state": "derived_from_declared_convention",
        "planes": planes,
        "R": r_value,
        "T": t_value,
        "A": a_value,
        "closure_abs": closure,
        "net_absorbed_power_w": incident - reflected - transmitted,
        "limitations": [
            "Plane orientation, medium identity, expressions, and signs are caller declarations.",
            "Volume-loss and cross-section normalizations are not substitutes for this declared-plane evidence.",
        ],
    }


def normalize_internal_absorption_consistency(
    cross_section: Mapping[str, Any] | None,
    volume_loss: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Compare two internal absorption normalizations without promoting closure.

    Missing declarations are represented as ``not_requested`` or ``unknown`` so
    that an absent CrossSectionCalculation is not a solve failure.
    """

    if cross_section is None:
        return {
            "schema_version": "1.0.0",
            "state": "not_requested",
            "physical_flux_closure_eligible": False,
        }

    cross = _mapping(dict(cross_section), "cross_section_absorption")
    _reject_unknown(cross, _CROSS_SECTION_FIELDS, "cross_section_absorption")
    normalized_cross = {
        "expression": _text(cross.get("expression"), "cross_section_absorption.expression"),
        "value_m2": _finite(cross.get("value_m2"), "cross_section_absorption.value_m2"),
        "unit": _text(cross.get("unit"), "cross_section_absorption.unit"),
        "unit_cell_area_expression": _text(
            cross.get("unit_cell_area_expression"),
            "cross_section_absorption.unit_cell_area_expression",
        ),
        "unit_cell_area_m2": _positive(
            cross.get("unit_cell_area_m2"),
            "cross_section_absorption.unit_cell_area_m2",
        ),
        "source_feature": _text(cross.get("source_feature"), "cross_section_absorption.source_feature"),
    }
    if normalized_cross["unit"] != "m^2":
        raise ValueError("cross_section_absorption.unit must be exactly 'm^2'")
    normalized_cross["normalized_absorption"] = (
        normalized_cross["value_m2"] / normalized_cross["unit_cell_area_m2"]
    )

    if volume_loss is None:
        return {
            "schema_version": "1.0.0",
            "state": "unknown",
            "cross_section": normalized_cross,
            "physical_flux_closure_eligible": False,
            "limitations": ["Volume-loss normalization was not declared."],
        }

    volume = _mapping(dict(volume_loss), "volume_loss_absorption")
    _reject_unknown(volume, _VOLUME_LOSS_FIELDS, "volume_loss_absorption")
    normalized_volume = {
        "expression": _text(volume.get("expression"), "volume_loss_absorption.expression"),
        "selection_ids": _selection_ids(
            volume.get("selection_ids"),
            "volume_loss_absorption.selection_ids",
        ),
        "value_w": _finite(volume.get("value_w"), "volume_loss_absorption.value_w"),
        "incident_power_w": _positive(
            volume.get("incident_power_w"),
            "volume_loss_absorption.incident_power_w",
        ),
        "unit": _text(volume.get("unit"), "volume_loss_absorption.unit"),
    }
    if normalized_volume["unit"] != "W":
        raise ValueError("volume_loss_absorption.unit must be exactly 'W'")
    normalized_volume["normalized_absorption"] = (
        normalized_volume["value_w"] / normalized_volume["incident_power_w"]
    )

    cross_value = normalized_cross["normalized_absorption"]
    volume_value = normalized_volume["normalized_absorption"]
    absolute_residual = abs(cross_value - volume_value)
    denominator = max(abs(cross_value), abs(volume_value))
    relative_residual = 0.0 if denominator == 0.0 else absolute_residual / denominator
    return {
        "schema_version": "1.0.0",
        "state": "measured",
        "classification": "internal_normalization_consistency",
        "cross_section": deepcopy(normalized_cross),
        "volume_loss": deepcopy(normalized_volume),
        "absolute_residual": absolute_residual,
        "relative_residual": relative_residual,
        "physical_flux_closure_eligible": False,
        "limitations": [
            "Agreement between internal normalizations does not establish independent declared-plane flux closure."
        ],
    }


__all__ = [
    "normalize_declared_plane_flux",
    "normalize_internal_absorption_consistency",
]
