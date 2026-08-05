"""Canonical solver-free simulation configuration validation and comparison."""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

from comsol_mcp.contracts.simulation_configuration import (
    ConfigurationDiffPolicy,
    SimulationConfigurationInput,
)
from comsol_mcp.durable import canonical_json_v1, domain_sha256_v2

_C = 299_792_458.0
_H = 6.626_070_15e-34
_UNIT_ALIASES = {
    "μm": "um",
    "µm": "um",
    "°": "deg",
}
_SCALES: dict[str, dict[str, tuple[float, float]]] = {
    "length": {
        "m": (1.0, 0.0),
        "mm": (1.0e-3, 0.0),
        "um": (1.0e-6, 0.0),
        "nm": (1.0e-9, 0.0),
    },
    "angle": {"rad": (1.0, 0.0), "deg": (math.pi / 180.0, 0.0)},
    "temperature": {"K": (1.0, 0.0), "degC": (1.0, 273.15)},
    "dimensionless": {"1": (1.0, 0.0)},
    "frequency": {
        "Hz": (1.0, 0.0),
        "kHz": (1.0e3, 0.0),
        "MHz": (1.0e6, 0.0),
        "GHz": (1.0e9, 0.0),
        "THz": (1.0e12, 0.0),
    },
    "energy": {"J": (1.0, 0.0), "eV": (1.602_176_634e-19, 0.0)},
}
_CANONICAL_UNITS = {
    "length": "m",
    "angle": "rad",
    "temperature": "K",
    "dimensionless": "1",
    "frequency": "Hz",
    "energy": "J",
}
_TOLERANCE_KEYS = {
    "m": "length_m",
    "rad": "angle_rad",
    "K": "temperature_K",
    "Hz": "frequency_Hz",
    "J": "energy_J",
    "1": "dimensionless",
}


def _normalize_quantity(value: dict[str, Any]) -> dict[str, Any]:
    if value["status"] == "unknown":
        return {"status": "unknown", "dimension": value["dimension"]}
    dimension = value["dimension"]
    unit = _UNIT_ALIASES.get(value["unit"], value["unit"])
    try:
        scale, offset = _SCALES[dimension][unit]
    except KeyError as exc:
        raise ValueError(f"unsupported {dimension} unit: {value['unit']}") from exc
    number = float(f"{float(value['value']) * scale + offset:.15g}")
    if not math.isfinite(number):
        raise ValueError("normalized quantities must be finite")
    return {
        "status": "known",
        "dimension": dimension,
        "value": number,
        "unit": _CANONICAL_UNITS[dimension],
    }


def _walk_quantities(value: Any) -> Any:
    if isinstance(value, list):
        return [_walk_quantities(item) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) >= {"status", "dimension", "value", "unit"}:
        return _normalize_quantity(value)
    return {key: _walk_quantities(item) for key, item in value.items()}


def _unique(items: list[dict[str, Any]], key: str, label: str) -> None:
    identities = [item[key] for item in items]
    if len(identities) != len(set(identities)):
        raise ValueError(f"{label} identities must be unique")


def _canonical_body(value: SimulationConfigurationInput | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, SimulationConfigurationInput):
        raw = value.model_dump(mode="python")
    else:
        raw = dict(value)
    supplied_hash = raw.pop("configuration_sha256", None)
    parsed = SimulationConfigurationInput.model_validate(raw)
    normalized = _walk_quantities(
        parsed.model_dump(mode="python", exclude={"configuration_sha256"})
    )
    if not isinstance(normalized, dict):
        raise ValueError("simulation configuration must normalize to an object")
    body: dict[str, Any] = normalized
    _unique(body["geometry"], "dimension_id", "geometry dimension")
    _unique(body["materials"], "material_id", "material")
    _unique(body["layers"], "layer_id", "layer")
    material_ids = {item["material_id"] for item in body["materials"]}
    if any(layer["material_id"] not in material_ids for layer in body["layers"]):
        raise ValueError("every layer must reference a declared material_id")
    layer_orders = [item["order"] for item in body["layers"]]
    if len(layer_orders) != len(set(layer_orders)):
        raise ValueError("layer order values must be unique")
    body["geometry"] = sorted(body["geometry"], key=lambda item: item["dimension_id"])
    body["materials"] = sorted(body["materials"], key=lambda item: item["material_id"])
    body["layers"] = sorted(body["layers"], key=lambda item: item["order"])
    body["mesh"]["dependency_keys"] = sorted(set(body["mesh"]["dependency_keys"]))
    body["mesh"]["characteristic_lengths"] = sorted(
        body["mesh"]["characteristic_lengths"], key=lambda item: item["dimension_id"]
    )
    for key in ("physics", "studies", "solvers", "datasets", "selections"):
        body["model_tree"][key] = sorted(set(body["model_tree"][key]))
    body["unit_contracts"] = sorted(body["unit_contracts"], key=lambda item: item["quantity"])
    body["artifact_chains"] = sorted(
        body["artifact_chains"], key=lambda item: (item["role"], item["chain_sha256"])
    )
    canonical_json_v1(body)
    calculated = domain_sha256_v2("simulation_configuration/1.0.0", body)
    if supplied_hash is not None and supplied_hash != calculated:
        raise ValueError("configuration_sha256 does not match the normalized configuration")
    return body


def normalize_simulation_configuration(
    value: SimulationConfigurationInput | dict[str, Any],
) -> dict[str, Any]:
    """Normalize explicit units and return one content-bound configuration."""
    body = _canonical_body(value)
    return deepcopy(
        {
            **body,
            "configuration_sha256": domain_sha256_v2("simulation_configuration/1.0.0", body),
        }
    )


def _path(path: tuple[str, ...]) -> str:
    return ".".join(path)


def _compare(
    left: Any,
    right: Any,
    path: tuple[str, ...],
    tolerances: dict[str, float],
    changes: list[dict[str, Any]],
    counts: dict[str, int],
    unit_hint: str | None = None,
) -> None:
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                classification = "unavailable" if key in {"status", "value", "unit"} else "semantic"
                counts[classification] += 1
                changes.append({"path": _path((*path, key)), "classification": classification})
            else:
                _compare(
                    left[key],
                    right[key],
                    (*path, key),
                    tolerances,
                    changes,
                    counts,
                    left.get("unit")
                    if key == "value" and left.get("unit") == right.get("unit")
                    else None,
                )
        return
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            counts["semantic"] += 1
            changes.append(
                {
                    "path": _path(path),
                    "classification": "semantic",
                    "left_count": len(left),
                    "right_count": len(right),
                }
            )
            return
        for index, (left_item, right_item) in enumerate(zip(left, right, strict=True)):
            _compare(left_item, right_item, (*path, str(index)), tolerances, changes, counts)
        return
    if left == right:
        counts["exact"] += 1
        return
    if path and path[-1] == "label":
        counts["label_only"] += 1
        changes.append({"path": _path(path), "classification": "label_only"})
        return
    if path and path[-1] == "status" and {left, right} == {"known", "unknown"}:
        counts["unavailable"] += 1
        changes.append({"path": _path(path), "classification": "unavailable"})
        return
    if (
        isinstance(left, (int, float))
        and not isinstance(left, bool)
        and isinstance(right, (int, float))
        and not isinstance(right, bool)
        and path
        and path[-1] == "value"
    ):
        tolerance_key = _TOLERANCE_KEYS.get(unit_hint or "")
        if (
            tolerance_key is not None
            and abs(float(left) - float(right)) <= tolerances[tolerance_key]
        ):
            counts["tolerance"] += 1
            changes.append(
                {
                    "path": _path(path),
                    "classification": "tolerance",
                    "absolute_delta": abs(float(left) - float(right)),
                    "unit": unit_hint,
                }
            )
            return
    counts["semantic"] += 1
    changes.append({"path": _path(path), "classification": "semantic"})


def compare_simulation_configurations(
    left: SimulationConfigurationInput | dict[str, Any],
    right: SimulationConfigurationInput | dict[str, Any],
    policy: ConfigurationDiffPolicy | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare normalized configurations without inferring missing physical facts."""
    left_normalized = normalize_simulation_configuration(left)
    right_normalized = normalize_simulation_configuration(right)
    parsed_policy = ConfigurationDiffPolicy.model_validate(policy or {})
    tolerances = parsed_policy.tolerances.model_dump(mode="python")
    left_body = {
        key: value for key, value in left_normalized.items() if key != "configuration_sha256"
    }
    right_body = {
        key: value for key, value in right_normalized.items() if key != "configuration_sha256"
    }
    counts = {name: 0 for name in ("exact", "tolerance", "semantic", "label_only", "unavailable")}
    changes: list[dict[str, Any]] = []
    _compare(left_body, right_body, (), tolerances, changes, counts)
    disposition = "different" if counts["semantic"] or counts["unavailable"] else "equivalent"
    physical_prefixes = (
        "geometry.",
        "materials.",
        "layers.",
        "incidence.",
        "wavelength_control.",
        "solver.boundary_termination",
    )
    physical_containers = {"geometry", "materials", "layers", "incidence", "wavelength_control"}
    physical_changes = [
        item
        for item in changes
        if item["classification"] in {"semantic", "unavailable"}
        and (item["path"] in physical_containers or item["path"].startswith(physical_prefixes))
    ]
    body = {
        "schema_name": "comsol_mcp.simulation_configuration_diff",
        "schema_version": "1.0.0",
        "left_configuration_sha256": left_normalized["configuration_sha256"],
        "right_configuration_sha256": right_normalized["configuration_sha256"],
        "disposition": disposition,
        "physical_disposition": "different" if physical_changes else "equivalent",
        "classification_counts": counts,
        "changes": changes,
        "policy": parsed_policy.model_dump(mode="python"),
        "physical_identity_inferred_from_labels": False,
    }
    return {**body, "diff_sha256": domain_sha256_v2("simulation_configuration_diff/1.0.0", body)}


__all__ = [
    "compare_simulation_configurations",
    "normalize_simulation_configuration",
]
